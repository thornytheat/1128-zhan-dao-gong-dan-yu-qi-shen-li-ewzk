"""Adjudicate district merges from independent evidence.

A merge may take effect only when the combined jurisdiction can carry the
scoring sample it needs, has a rostered reviewer and an office calendar, and
its boundary tickets do not contradict each other.  Tickets whose sources
disagree stay in their original district for manual verification so that
phantom queue pressure never reaches the model.

Nothing here mutates shared state: :func:`adjudicate` returns a new mapping
and the caller decides whether to publish it.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Iterable

from .contracts import MergeGroup, MergePolicy


CHANNEL_LEDGER = "占道台账"
CHANNEL_CLOSURE = "结案回执"

EFFECTIVE = "生效"
DEFERRED = "暂缓"


@dataclass(frozen=True, slots=True)
class Channel:
    name: str
    district: str
    detail: str

    def as_record(self) -> dict:
        return {"channel": self.name, "district": self.district, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class Evidence:
    """One immutable snapshot of everything used to judge a merge batch."""

    permit_districts: dict[str, str]
    channels: dict[str, tuple[Channel, ...]]
    sample_counts: dict[str, int]
    calendar_districts: frozenset[str]
    reviewer_districts: dict[str, str]
    known_districts: frozenset[str]

    def boundary(self, permit: str) -> tuple[Channel, ...]:
        ledger = self.permit_districts[permit]
        conflicting = [item for item in self.channels.get(permit, ()) if item.district != ledger]
        return tuple(sorted(conflicting, key=lambda item: (item.name, item.district, item.detail)))

    def local_claims(self, permit: str, group: MergeGroup) -> tuple[Channel, ...]:
        """Conflicting channels that point at a district *outside* the group.

        A disagreement between two districts inside the same group is resolved
        by the merge itself; a claim that points anywhere else means the
        group's boundary evidence is contradictory and must not be merged.
        """
        sources = set(group.sources)
        conflicting = [item for item in self.channels.get(permit, ())
                       if item.district not in sources and item.district != self.permit_districts[permit]]
        return tuple(sorted(conflicting, key=lambda item: (item.name, item.district, item.detail)))


@dataclass(frozen=True, slots=True)
class HeldTicket:
    permit: str
    district: str
    group: str
    channels: tuple[Channel, ...]
    reason: str

    def as_record(self) -> dict:
        ledger = next((item for item in self.channels if item.name == CHANNEL_LEDGER), None)
        return {
            "permit": self.permit,
            "origin_district": self.district,
            "group": self.group,
            "status": "留原辖区待核",
            "ledger_district": ledger.district if ledger else self.district,
            "conflicts": [item.as_record() for item in self.channels if item.name != CHANNEL_LEDGER],
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class GroupDecision:
    group: MergeGroup
    state: str
    reason: str
    sample_before: dict[str, int]
    combined_samples: int
    permit_count: int
    reviewer_count: int
    boundary_count: int
    conflict_count: int
    boundary_share: float
    held: tuple[HeldTicket, ...]
    supplemented: tuple[str, ...]

    def as_record(self) -> dict:
        return {
            "record": "归并裁决",
            "group": self.group.name,
            "districts": list(self.group.sources),
            "state": self.state,
            "reason": self.reason,
            "permits": self.permit_count,
            "samples_before": dict(self.sample_before),
            "combined_samples": self.combined_samples,
            "supplemented_districts": list(self.supplemented),
            "reviewers": self.reviewer_count,
            "boundary_conflicts": self.boundary_count,
            "unresolved_boundary": self.conflict_count,
            "boundary_share": round(self.boundary_share, 4),
            "held_tickets": len(self.held),
        }


@dataclass(frozen=True, slots=True)
class Adjudication:
    decisions: tuple[GroupDecision, ...]
    zone_of: dict[str, str]
    origin_of: dict[str, str]
    held: dict[str, HeldTicket]
    reviewer_zones: dict[str, tuple[str, ...]]
    district_zones: dict[str, str]
    zones_before: tuple[str, ...]
    zones_after: tuple[str, ...]
    queue_before: dict[str, int]
    queue_after: dict[str, int]
    samples_before: dict[str, int]
    samples_after: dict[str, int]

    @property
    def effective(self) -> tuple[GroupDecision, ...]:
        return tuple(item for item in self.decisions if item.state == EFFECTIVE)

    @property
    def deferred(self) -> tuple[GroupDecision, ...]:
        return tuple(item for item in self.decisions if item.state != EFFECTIVE)

    def records(self) -> list[dict]:
        return [item.as_record() for item in self.decisions]

    def held_records(self) -> list[dict]:
        return [self.held[key].as_record() for key in sorted(self.held)]


def adjudicate(groups: Iterable[MergeGroup], evidence: Evidence, policy: MergePolicy) -> Adjudication:
    decisions: list[GroupDecision] = []
    held: dict[str, HeldTicket] = {}
    zone_of = dict(evidence.permit_districts)
    origin_of = dict(evidence.permit_districts)
    reviewer_zones = {handle: (district,) for handle, district in evidence.reviewer_districts.items()}
    for group in groups:
        sources = set(group.sources)
        sample_before = {district: evidence.sample_counts.get(district, 0) for district in group.sources}
        combined = sum(sample_before.values())
        permit_keys = sorted(permit for permit, district in evidence.permit_districts.items() if district in sources)
        boundary = [permit for permit in permit_keys if evidence.boundary(permit)]
        # A claim pointing outside the group cannot be settled by the merge.
        # Keeping such a ticket in its original district prevents phantom queue
        # pressure from leaking into the merged sample.
        conflicts = [permit for permit in permit_keys if evidence.local_claims(permit, group)]
        reviewer_count = sum(1 for district in evidence.reviewer_districts.values() if district in sources)
        unknown = sorted(district for district in group.sources if district not in evidence.known_districts)
        missing_calendar = sorted(district for district in group.sources
                                  if district not in evidence.calendar_districts)
        share = len(conflicts) / max(1, len(permit_keys))
        supplemented = tuple(district for district, count in sorted(sample_before.items())
                             if count < policy.min_samples)
        state, reason = _verdict(unknown, combined, reviewer_count, missing_calendar,
                                 permit_keys, len(conflicts), share, policy)
        tickets: tuple[HeldTicket, ...] = ()
        if state == EFFECTIVE:
            tickets = tuple(
                HeldTicket(permit, evidence.permit_districts[permit], group.name,
                           evidence.local_claims(permit, group),
                           "边界工单证据指向组外辖区，留原辖区待核")
                for permit in conflicts
            )
            for ticket in tickets:
                zone_of[ticket.permit] = ticket.district
                held[ticket.permit] = ticket
            for permit in permit_keys:
                if permit not in held:
                    zone_of[permit] = group.name
            for handle, district in evidence.reviewer_districts.items():
                if district in sources:
                    reviewer_zones[handle] = tuple(sorted({group.name, district}))
        decisions.append(GroupDecision(
            group=group, state=state, reason=reason, sample_before=sample_before,
            combined_samples=combined, permit_count=len(permit_keys), reviewer_count=reviewer_count,
            boundary_count=len(boundary), conflict_count=len(conflicts), boundary_share=share,
            held=tickets, supplemented=supplemented,
        ))
    origin_to_zone: dict[str, str] = {}
    for group_decision in decisions:
        if group_decision.state != EFFECTIVE:
            continue
        for district in group_decision.group.sources:
            origin_to_zone[district] = group_decision.group.name
    district_zones = dict(sorted(origin_to_zone.items()))
    return Adjudication(
        decisions=tuple(decisions), zone_of=zone_of, origin_of=origin_of, held=held,
        reviewer_zones=reviewer_zones, district_zones=district_zones,
        zones_before=tuple(sorted(set(origin_of.values()))),
        zones_after=tuple(sorted(set(zone_of.values()))),
        queue_before=dict(sorted(Counter(origin_of.values()).items())),
        queue_after=dict(sorted(Counter(zone_of.values()).items())),
        samples_before=dict(sorted(evidence.sample_counts.items())),
        samples_after=dict(sorted(
            (zone, total) for zone, total in (
                (zone, sum(count for district, count in evidence.sample_counts.items()
                           if district_zones.get(district, district) == zone))
                for zone in set(district_zones.values()) | set(evidence.sample_counts)
            ) if total
        )),
    )


def _verdict(unknown, combined, reviewer_count, missing_calendar, permit_keys,
             boundary_count, boundary_share, policy) -> tuple[str, str]:
    if unknown:
        return DEFERRED, "辖区不在台账、日历或班表证据内：" + "、".join(unknown)
    if combined < policy.min_samples:
        return DEFERRED, f"合并后评分样本仍不足（{combined} < {policy.min_samples}）"
    if reviewer_count == 0:
        return DEFERRED, "归并后没有当值审理员"
    if missing_calendar:
        return DEFERRED, "归并后日历覆盖不全：" + "、".join(missing_calendar)
    if not permit_keys:
        return DEFERRED, "边界没有可见工单，暂不发布"
    if boundary_count > policy.max_held_per_group or boundary_share > policy.max_conflict_share:
        return DEFERRED, (f"边界矛盾过多（{boundary_count} 宗，占比 {boundary_share:.0%}），"
                          "整组留原辖区待核，避免把不存在的队列压力带进模型")
    return EFFECTIVE, "证据一致，可先行归并；矛盾边界工单留原辖区待核"
