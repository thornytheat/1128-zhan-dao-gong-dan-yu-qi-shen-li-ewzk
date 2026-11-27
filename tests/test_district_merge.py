import pytest

from permitdesk.district_merge.adjudication import (CHANNEL_CLOSURE, CHANNEL_LEDGER, Channel,
                                                    Evidence, adjudicate)
from permitdesk.district_merge.contracts import (MergeGroup, MergePlanError, MergePolicy,
                                                 batch_digest, parse_batch)
from permitdesk.district_merge.publication import (PublicationError, PublicationLog, publish,
                                                   publication_id)


def policy(**changes):
    return MergePolicy(**({'at': 100, 'max_held_per_group': 1, 'max_conflict_share': .34,
                           'min_samples': 10} | changes))


def evidence(permits, samples=None, calendar=('甲', '乙', '丙'), reviewers=None, channels=None):
    channels = channels or {}
    return Evidence(
        permit_districts=dict(permits),
        channels={permit: (Channel(CHANNEL_LEDGER, district, '有效修订'), *channels.get(permit, ()))
                  for permit, district in permits.items()},
        sample_counts=samples or {district: 40 for district in set(permits.values())},
        calendar_districts=frozenset(calendar),
        reviewer_districts=reviewers or {'甲一': '甲', '乙一': '乙', '丙一': '丙'},
        known_districts=frozenset(set(permits.values()) | set(calendar) | {'甲', '乙', '丙'}),
    )


def group(name='甲乙', districts=('甲', '乙')):
    return MergeGroup(name, tuple(districts))


def test_batch_order_and_duplicate_do_not_change_digest():
    forward = parse_batch([['甲', '乙'], {'districts': ['丙', '丁'], 'name': '丙丁'}])
    reversed_input = parse_batch([{'name': '丙丁', 'districts': ['丁', '丙']}, ['乙', '甲']])
    duplicated = parse_batch([['甲', '乙'], ['甲', '乙'], {'districts': ['丙', '丁'], 'name': '丙丁'}])
    assert forward == reversed_input == duplicated
    assert batch_digest(forward) == batch_digest(duplicated)


def test_batch_rejects_overlapping_or_malformed_groups():
    with pytest.raises(MergePlanError, match='多个归并组'):
        parse_batch([['甲', '乙'], ['乙', '丙']])
    with pytest.raises(MergePlanError, match='2 至 16 个辖区'):
        parse_batch([['甲']])
    with pytest.raises(MergePlanError, match='重复'):
        parse_batch([['甲', '甲']])
    with pytest.raises(MergePlanError, match='文本'):
        parse_batch([['甲', 3]])
    with pytest.raises(MergePlanError, match='无法识别'):
        parse_batch([{'districts': ['甲', '乙'], 'unknown': 1}])


def test_merge_requires_sufficient_samples_and_reviewer():
    thin = evidence({'P1': '甲', 'P2': '乙'}, samples={'甲': 3, '乙': 3})
    verdict = adjudicate((group(),), thin, policy())[0] if False else adjudicate((group(),), thin, policy()).decisions[0]
    assert verdict.state == '暂缓' and '样本仍不足' in verdict.reason
    orphan = evidence({'P1': '甲', 'P2': '乙'}, reviewers={'丙一': '丙'})
    assert adjudicate((group(),), orphan, policy()).decisions[0].state == '暂缓'
    no_calendar = evidence({'P1': '甲', 'P2': '乙'}, calendar=('甲',))
    assert '日历覆盖不全' in adjudicate((group(),), no_calendar, policy()).decisions[0].reason


def test_conflicting_boundary_ticket_stays_in_original_district():
    permits = {f'P{i}': '甲' for i in range(6)}
    permits['P9'] = '乙'
    claims = {f'P{i}': (Channel(CHANNEL_CLOSURE, '丙', '结案回执'),) for i in range(0, 6, 2)}
    result = adjudicate((group(),), evidence(permits, channels=claims), policy())
    decision = result.decisions[0]
    assert decision.state == '暂缓'
    assert '边界矛盾过多' in decision.reason
    # Nothing merged, so every permit keeps its ledger district.
    assert result.zone_of == permits


def test_merge_takes_effect_but_holds_out_of_group_claim():
    permits = {f'P{i}': '甲' for i in range(5)}
    permits.update({f'Q{i}': '乙' for i in range(5)})
    claims = {'P2': (Channel(CHANNEL_CLOSURE, '丙', '结案回执'),)}
    result = adjudicate((group(),), evidence(permits, channels=claims), policy())
    decision = result.decisions[0]
    assert decision.state == '生效'
    assert [ticket.permit for ticket in decision.held] == ['P2']
    assert result.zone_of['P2'] == '甲'
    assert result.zone_of['P0'] == '甲乙'
    assert result.zone_of['Q0'] == '甲乙'
    assert result.held_records()[0]['status'] == '留原辖区待核'


def test_in_group_boundary_disagreement_is_resolved_by_merge():
    # Revision history that names the sibling district is not a contradiction:
    # the merge itself settles it.
    permits = {f'P{i}': '甲' for i in range(6)}
    permits['P1'] = '甲'
    claims = {'P1': (Channel('台账修订', '乙', '历史修订见 乙'),)}
    decision = adjudicate((group(),), evidence(permits, channels=claims), policy()).decisions[0]
    assert decision.state == '生效' and not decision.held
    assert decision.conflict_count == 0


def test_deferred_group_leaves_zone_map_untouched_and_counts_queue():
    permits = {'P1': '甲', 'P2': '甲', 'P3': '乙'}
    thin = evidence(permits, samples={'甲': 2, '乙': 2})
    result = adjudicate((group(),), thin, policy())
    assert result.effective == () and result.deferred
    assert result.zones_after == result.zones_before == ('乙', '甲')
    assert result.queue_after == result.queue_before


def test_publication_is_idempotent_and_rolls_back_on_failure():
    permits = {f'P{i}': '甲' for i in range(5)} | {f'Q{i}': '乙' for i in range(5)}
    plan = adjudicate((group(),), evidence(permits), policy())
    calls = []

    def recompute(zone_of, reviewer_zones):
        calls.append(dict(zone_of))
        return 'run-1', {'rows': len(zone_of)}

    def explode(zone_of, reviewer_zones):
        raise RuntimeError('模型重算失败')

    failing_log = PublicationLog()
    with pytest.raises(PublicationError, match='原版归类'):
        publish(failing_log, groups=(group(),), adjudication=plan, policy=policy(),
                capacity=8, baseline_run_id='base', recompute=explode)
    failed = failing_log.entries[-1]
    assert failed.state == '已回滚' and failed.published_zones == failed.original_zones
    assert failing_log.latest_original() is failed

    # The same batch may be retried after a rollback, and only succeeds once.
    entry, result = publish(failing_log, groups=(group(),), adjudication=plan, policy=policy(),
                            capacity=8, baseline_run_id='base', recompute=recompute)
    assert entry.state == '已发布' and len(calls) == 1
    same, again = publish(failing_log, groups=(group(),), adjudication=plan, policy=policy(),
                          capacity=8, baseline_run_id='base', recompute=recompute)
    assert same is entry and again is None and len(calls) == 1
    assert [item.state for item in failing_log.entries] == ['已回滚', '已发布']


def test_publication_rerun_with_new_baseline_is_a_new_batch():
    first = publication_id(8, (group(),), policy(), 'base-a')
    second = publication_id(8, (group(),), policy(), 'base-b')
    assert first != second


def test_evidence_gathering_holds_out_of_group_revision_claim():
    from types import SimpleNamespace as O
    from permitdesk.district_merge.contracts import MergePolicy
    from permitdesk.district_merge.service import gather_evidence

    cases = [O(permit=f'P{i}', district='甲', version=1) for i in range(20)]
    cases += [O(permit=f'Q{i}', district='乙', version=1) for i in range(20)]
    revisions = [{'permit': 'P0', 'district': '甲', 'version': 2, 'districts': ['甲', '丙']}]
    history = ([{'key': f'h{i}', 'district': '甲', 'closed': 10} for i in range(25)]
               + [{'key': f'k{i}', 'district': '乙', 'closed': 10} for i in range(25)])
    calendars = [{'district': name, 'work': True, 'closure': False} for name in ('甲', '乙', '丙')]
    reviewers = [{'handle': '甲一', 'district': '甲'}, {'handle': '乙一', 'district': '乙'}]
    snapshot = gather_evidence(cases, revisions, history, calendars, reviewers, [], 100)
    result = adjudicate((group(),), snapshot, MergePolicy(100, 2, .34, 5))
    assert result.decisions[0].state == '生效'
    assert result.zone_of['P0'] == '甲' and result.zone_of['P1'] == '甲乙'
    assert result.held_records()[0]['conflicts'][0]['channel'] == '台账修订'


def test_evidence_ignores_receipts_after_cutoff():
    from types import SimpleNamespace as O
    from permitdesk.district_merge.service import gather_evidence

    cases = [O(permit=f'P{i}', district='甲', version=1) for i in range(25)]
    history = [{'key': f'h{i}', 'district': '甲', 'closed': 10} for i in range(30)]
    calendars = [{'district': '甲', 'work': True, 'closure': False}]
    reviewers = [{'handle': '甲一', 'district': '甲'}]
    receipt = {'permit': 'P0', 'recorded': 900, 'primary': '丙 主管', 'ticket': 'T-1'}
    early = gather_evidence(cases, [], history, calendars, reviewers, [receipt], 800)
    late = gather_evidence(cases, [], history, calendars, reviewers, [receipt], 950)
    assert early.channels['P0'] and len(early.channels['P0']) == 1
    assert any(item.district == '丙' for item in late.channels['P0'])
