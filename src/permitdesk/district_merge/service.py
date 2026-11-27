"""Wire district-merge evidence to the existing permit desk artefacts.

The service is deliberately pure: :func:`prepare_merges` returns an
:class:`~permitdesk.district_merge.adjudication.Adjudication` without touching
any published result.  Publishing is handled transactionally by
:mod:`permitdesk.district_merge.publication`.
"""

from __future__ import annotations

from collections import Counter

from .adjudication import (CHANNEL_CLOSURE, CHANNEL_LEDGER, Adjudication, Channel,
                           Evidence, adjudicate)
from .contracts import MergeGroup, MergePolicy, known_districts


REVISION_CHANNEL = "台账修订"


def _receipt_district(receipt: dict) -> str | None:
    primary = receipt.get("primary")
    if isinstance(primary, str) and primary.strip():
        return primary.split()[0].strip()
    return None


def gather_evidence(cases, revisions, history, calendars, reviewers, receipts,
                    cutoff: int) -> Evidence:
    """Collect independent district claims visible at ``cutoff``.

    Only sources that existed at or before ``cutoff`` may be used: a closure
    receipt is visible when it has been recorded, and a permit revision is
    visible when it has been received.
    """
    permit_districts = {row.permit: row.district for row in cases}
    channels: dict[str, list[Channel]] = {
        permit: [Channel(CHANNEL_LEDGER, district, f"有效修订 {version}")]
        for permit, district, version in ((row.permit, row.district, row.version) for row in cases)
    }
    revision_rows = {row["permit"]: row for row in revisions}
    for permit, row in revision_rows.items():
        for district in row.get("districts", ()):
            if district != permit_districts.get(permit):
                channels.setdefault(permit, []).append(
                    Channel(REVISION_CHANNEL, district, f"历史修订见 {district}"))
    for receipt in receipts:
        if not isinstance(receipt, dict):
            continue
        recorded = receipt.get("recorded")
        if type(recorded) is not int or recorded > cutoff:
            continue
        permit = receipt.get("permit")
        district = _receipt_district(receipt)
        if permit in permit_districts and district and district != permit_districts[permit]:
            channels.setdefault(permit, []).append(
                Channel(CHANNEL_CLOSURE, district, f"结案回执 {receipt.get('ticket', '')}".strip()))
    sample_counts = Counter()
    for row in history:
        if not isinstance(row, dict):
            continue
        if row.get("closed", cutoff) >= cutoff:
            continue
        district = row.get("district")
        if isinstance(district, str) and district:
            sample_counts[district] += 1
    calendar_districts = frozenset(
        row["district"] for row in calendars
        if isinstance(row, dict) and isinstance(row.get("district"), str) and row.get("work") and not row.get("closure")
    )
    reviewer_districts = {row["handle"]: row["district"] for row in reviewers
                          if isinstance(row, dict) and isinstance(row.get("handle"), str)}
    universe = known_districts(set(permit_districts.values()),
                               calendar_districts,
                               set(reviewer_districts.values()),
                               set(sample_counts))
    return Evidence(
        permit_districts=permit_districts,
        channels={permit: tuple(values) for permit, values in channels.items()},
        sample_counts=dict(sample_counts),
        calendar_districts=calendar_districts,
        reviewer_districts=reviewer_districts,
        known_districts=frozenset(universe),
    )


def prepare_merges(groups: tuple[MergeGroup, ...], evidence: Evidence,
                   policy: MergePolicy) -> Adjudication:
    return adjudicate(groups, evidence, policy)


def shift_start(reviewers) -> int | None:
    values = [row["available"] for row in reviewers
              if isinstance(row, dict) and type(row.get("available")) is int]
    return min(values) if values else None


def check_publication_window(policy: MergePolicy, reviewers) -> int:
    """Refuse to publish after the next shift has already opened."""
    opening = shift_start(reviewers)
    if opening is None:
        raise ValueError("班表缺少可约起始分钟，无法确认开班时点")
    if policy.at > opening:
        raise ValueError(f"归并须在下一班开班前发布（{policy.at} 晚于开班 {opening}）")
    return opening


def summary(groups: tuple[MergeGroup, ...], adjudication: Adjudication,
            policy: MergePolicy, capacity: int, opening: int | None) -> dict:
    counts = Counter(row.state for row in adjudication.decisions)
    return {
        "state": "已归并" if adjudication.effective else "未归并",
        "policy": policy.as_record(),
        "next_shift": opening,
        "capacity": capacity,
        "groups": adjudication.records(),
        "held": adjudication.held_records(),
        "zones_before": list(adjudication.zones_before),
        "zones_after": list(adjudication.zones_after),
        "queue_before": adjudication.queue_before,
        "queue_after": adjudication.queue_after,
        "samples_before": adjudication.samples_before,
        "samples_after": adjudication.samples_after,
        "reviewer_scope": {handle: list(zones) for handle, zones in sorted(adjudication.reviewer_zones.items())},
        "counts": dict(sorted(counts.items())),
        "effective": len(adjudication.effective),
        "deferred": len(adjudication.deferred),
        "held_tickets": len(adjudication.held),
    }
