from pathlib import Path
import json
from collections import Counter
from hashlib import sha256
from dataclasses import asdict
from .permit_intake.service import intake,material_workload,district_evidence
from .calendar_intervals.service import clock_records, resolve_clocks
from .review_features.service import build_features,feature_check
from .deadline_classifier.service import classify_cases,explain,walk_forward
from .review_roster.service import prepare_roster,hearing_records,shift_impact
from .permit_journal.service import review,review_closures
from .district_merge.contracts import MergePolicy,parse_batch
from .district_merge.service import (check_publication_window,gather_evidence,prepare_merges,
                                     summary as merge_summary)
from .district_merge.publication import PublicationError,PublicationLog,publish

SAMPLE=Path(__file__).resolve().parents[2]/"sample_data"

def load(name):
    return json.loads((SAMPLE/name).read_text(encoding="utf-8"))

def _capacity(capacity):
    cap=8 if capacity is None else capacity
    if type(cap) is not int or not 1<=cap<=100:
        raise ValueError("审理席位配额须在 1 至 100")
    return cap

def run(capacity: int|None=None, merges=None, policy=None)->dict:
    """Run the desk.  ``merges`` is an optional district-merge batch.

    Without a batch the result is the historical baseline unchanged.  With a
    batch the same artefacts are recomputed against the adjudicated zones and
    a ``merge`` report is attached; see :func:`publish_merges`.
    """
    snapshot,_report=publish_merges(capacity,merges,policy)
    return snapshot

def publish_merges(capacity: int|None=None, merges=None, policy=None,
                   journal: PublicationLog|None=None)->tuple[dict,dict]:
    cap=_capacity(capacity)
    groups=parse_batch(merges)
    cfg=load("rules_and_rosters.json")
    calendar=load("working_intervals.json")
    cases=intake(load("permit_intake.json"),cfg["road_aliases"],cfg["at"])
    clocks,outlets=resolve_clocks(cases.active,calendar,cfg["pauses"],cfg["at"],cfg)
    history=load("historical_decisions.json")
    receipts=load("closure_register.json")
    baseline=_compose(cap,cfg,calendar,cases,clocks,outlets,history,receipts)
    if not groups:
        return baseline,{"state":"未启用","capacity":cap,"plan":None,"publication":None,
                         "original":None,"run_id":None,"baseline_run_id":baseline["run_id"],
                         "zones_before":None,"zones_after":None,"held":[],"groups":[]}
    settings=policy if isinstance(policy,MergePolicy) else MergePolicy.parse(policy,cfg["at"])
    opening=check_publication_window(settings,cfg["reviewers"])
    evidence=gather_evidence(cases.active,cases.revisions,history,calendar,cfg["reviewers"],receipts,cfg["at"])
    adjudication=prepare_merges(groups,evidence,settings)
    plan=merge_summary(groups,adjudication,settings,cap,opening)
    log=journal if journal is not None else PublicationLog()
    def recompute(zone_of,reviewer_zones):
        packet=_compose(cap,cfg,calendar,cases,clocks,outlets,history,receipts,
                        zone_of=zone_of,
                        district_zones=adjudication.district_zones,
                        reviewer_zones=reviewer_zones)
        _attach_merge_evidence(packet,plan,adjudication,_capacity_recompute(baseline,packet,cap))
        packet["run_id"]=fingerprint({key:value for key,value in packet.items() if key!="run_id"})
        return packet["run_id"],packet
    try:
        entry,result=publish(log,groups=groups,adjudication=adjudication,policy=settings,
                             capacity=cap,baseline_run_id=baseline["run_id"],recompute=recompute)
    except PublicationError:
        # The recomputation failed; the original classification is still in
        # force and queryable.  Return the untouched baseline rather than a
        # partially merged docket, flagged so the caller knows it was rolled
        # back.
        report=merge_report(dict(plan,state="已回滚"),None,cap,adjudication,baseline,log)
        baseline["merge"]={"state":"已回滚","capacity":cap,"plan":report["plan"],
                           "recomputed":None,"groups":report["groups"],"held":report["held"],
                           "publication":None,"original":report["original"]}
        return baseline,report
    if result is None:
        # Same batch, same baseline: reuse the previously published packet,
        # rebuilding it deterministically rather than re-deriving a new id.
        result=_compose(cap,cfg,calendar,cases,clocks,outlets,history,receipts,
                        zone_of=adjudication.zone_of,
                        district_zones=adjudication.district_zones,
                        reviewer_zones=adjudication.reviewer_zones)
        _attach_merge_evidence(result,plan,adjudication,_capacity_recompute(baseline,result,cap))
        result["run_id"]=fingerprint({key:value for key,value in result.items() if key!="run_id"})
    report=merge_report(plan,entry,cap,adjudication,baseline,log)
    report["recomputed"]=result["merge"]["recomputed"]
    report["run_id"]=result["run_id"]
    # Publication metadata is derived deterministically from the batch and the
    # fingerprint, so attach it after fingerprinting to avoid a cycle.  The
    # exported packet still carries the recoverable original classification.
    result["merge"]["publication"]=report["publication"]
    result["merge"]["original"]=report["original"]
    return result,report

def _capacity_recompute(baseline,result,capacity):
    """Show how the in-shift quota was re-filled against the merged zones."""
    before=Counter(row["district"] for row in baseline["rows"] if row["state"]=="安排复核")
    after=Counter(row["district"] for row in result["rows"] if row["state"]=="安排复核")
    return {
        "record":"班内名额重算",
        "capacity":capacity,
        "scheduled_before":sum(before.values()),
        "scheduled_after":sum(after.values()),
        "slots_open_after":capacity-sum(after.values()),
        "by_zone_before":dict(sorted(before.items())),
        "by_zone_after":dict(sorted(after.items())),
        "reviewer_scope_changed":before!=after,
    }

def _attach_merge_evidence(packet,plan,adjudication,recomputed):
    """Fold merge evidence into the affected stages without adding a stage."""
    packet["merge"]={"state":plan["state"],"capacity":plan["capacity"],"plan":plan,
                     "recomputed":recomputed,"groups":adjudication.records(),
                     "held":adjudication.held_records()}
    evidence_stage=packet["stages"][2]
    evidence_stage["records"]=[*evidence_stage["records"],
                               {"record":"辖区归并","samples_before":adjudication.samples_before,
                                "samples_after":adjudication.samples_after,
                                "zones_before":list(adjudication.zones_before),
                                "zones_after":list(adjudication.zones_after)}]
    roster_stage=packet["stages"][4]
    roster_stage["records"]=[*roster_stage["records"],
                             {"record":"归并后队列","queue_before":adjudication.queue_before,
                              "queue_after":adjudication.queue_after,
                              "held_tickets":[ticket.permit for ticket in adjudication.held.values()],
                              "capacity":plan["capacity"]}]
    packet["stages"][0]["records"]=[*packet["stages"][0]["records"],*adjudication.records()]

def merge_report(plan,entry,capacity,adjudication=None,baseline=None,log=None):
    original=log.latest_original() if log else None
    return {
        "state":plan["state"] if plan else "未启用",
        "capacity":capacity,
        "plan":plan,
        "publication":entry.as_record() if entry else None,
        # The recoverable pre-merge classification of the most recent attempt.
        "original":original.as_record() if original else None,
        "run_id":entry.run_id if entry else None,
        "baseline_run_id":baseline["run_id"] if baseline else None,
        "zones_before":list(adjudication.zones_before) if adjudication else None,
        "zones_after":list(adjudication.zones_after) if adjudication else None,
        "held":adjudication.held_records() if adjudication else [],
        "groups":adjudication.records() if adjudication else [],
    }

def _compose(cap,cfg,calendar,cases,clocks,outlets,history,receipts,zone_of=None,
             district_zones=None,reviewer_zones=None):
    old,current,docs=build_features(cases.active,history,clocks,cfg,district_zones,zone_of)
    model,scored=classify_cases(old,current,cfg["model"])
    hearings=prepare_roster(cases.active,clocks,scored,docs,cfg,cap,zone_of,reviewer_zones)
    policy_replay=walk_forward(old,model.policy,cfg["at"],review_slots=cap)
    shift_review=shift_impact(cases.active,clocks,scored,docs,cfg["reviewers"],cap,cfg["at"],hearings,
                              cfg["exercise_absences"],zone_of=zone_of,reviewer_zones=reviewer_zones)
    followup=review_closures(hearings,clocks,receipts,cfg["audit_at"])
    mark=sha256(json.dumps([asdict(model),calendar,cfg["reviewers"],cfg["pauses"],shift_review["replay_id"]],sort_keys=True).encode()).hexdigest()
    check,detail=review(hearings,scored,receipts,
                        {"audit_at":cfg["audit_at"],"analysis_digest":mark,"capacity":cap})
    docket={row.permit:row for row in cases.active}
    result=[]
    for item in hearings:
        source=docket[item.permit]
        holding=clocks[item.permit]
        assessed=scored[item.permit]
        result.append({"permit":item.permit,"district":(zone_of or {}).get(item.permit,source.district),
                       "road":source.road,"work":source.work,
                       "remaining":holding.remaining,"due":holding.due,"risk":assessed.probability,
                       "primary":item.reviewer,"secondary":item.second,"state":item.state,
                       "reason":item.reason+" · "+assessed.label})
    counts=Counter(row.state for row in hearings)
    overdue=sum(clock.overdue for clock in clocks.values())
    history_rows=detail[1:]
    values=[{"label":f"{ix*20}–{(ix+1)*20}%", "value":row["n"]} for ix,row in enumerate(history_rows) if "bucket" in row]
    packet = {
        "metrics":{"有效占道申请":len(cases.active),"已过法定时限":overdue,"已安排复核":counts["安排复核"],
                   "待通知补件":counts["通知补件"]},
        "columns":{"permit":"许可号","district":"辖区","road":"道路","work":"施工类型","remaining":"剩余法定分钟",
                   "due":"截止分钟","risk":"校准逾期概率","primary":"首审","secondary":"二审","state":"处置","reason":"审理依据"},
        "rows":result,"series":values,
        "stages":[{"title":"许可流转","summary":f"{len(cases.active)} 份使用有效修订，保留冲突及撤回记录",
                   "records":[*cases.log,*material_workload(cases),*district_evidence(cases)]},
                  {"title":"日历折算","summary":f"工作时钟映射 {len(outlets)} 个街区，标注假期及补正暂停",
                   "records":clock_records(cases.active,clocks,outlets,cfg["pauses"],cfg["at"])},
                  {"title":"审理特征","summary":f"历史训练 {len(old.history)} 宗，留出 {len(old.calibration)} 宗",
                   "records":feature_check(current,docs)},
                  {"title":"超期概率","summary":f"模型 {model.policy.key}，保序校准 {len(model.buckets)} 段及按时点滚动复验",
                   "records":[*explain(model,scored),*policy_replay]},
                  {"title":"审理编组","summary":f"席位配额 {cap}，实际安排 {counts['安排复核']} 宗",
                   "records":[*hearing_records(hearings),shift_review]},
                  {"title":"许可追溯","summary":"回执及双签核对通过" if check["passed"] else "签审凭证冲突",
                   "records":[*detail,*followup]}],
    }
    packet["run_id"]=fingerprint(packet)
    return packet

def fingerprint(packet)->str:
    content=json.dumps(packet,ensure_ascii=False,allow_nan=False,sort_keys=True)
    return sha256(content.encode()).hexdigest()[:12]
