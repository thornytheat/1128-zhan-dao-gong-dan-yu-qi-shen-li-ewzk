"""Publish a merge batch transactionally with a recoverable original record.

A publication is all-or-nothing.  The original classification (the pre-merge
zone of every visible permit) is captured before anything is recomputed.  If
any recomputation step fails, the original classification is restored and stays
queryable through :class:`PublicationLog`; nothing partial is left behind.
Re-running the same batch is idempotent: the publication id is derived from the
normalised batch, the policy, the capacity and the baseline run, so identical
work never adds a district or a reservation twice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from json import dumps

from .adjudication import Adjudication
from .contracts import MergeGroup, MergePolicy, batch_digest


class PublicationError(RuntimeError):
    """Raised when a merge publication cannot be completed and was rolled back."""


@dataclass(frozen=True, slots=True)
class Publication:
    batch_id: str
    run_id: str
    state: str
    original_zones: dict[str, str]
    published_zones: dict[str, str]
    held: dict[str, str]
    capacity: int
    baseline_run_id: str | None
    groups: tuple[dict, ...]
    digest: str

    def as_record(self) -> dict:
        return {
            "record": "归并发布",
            "batch_id": self.batch_id,
            "state": self.state,
            "run_id": self.run_id,
            "baseline_run_id": self.baseline_run_id,
            "capacity": self.capacity,
            "zones_before": _counts(self.original_zones.values()),
            "zones_after": _counts(self.published_zones.values()),
            "held": dict(sorted(self.held.items())),
            "groups": list(self.groups),
            "digest": self.digest,
        }


@dataclass
class PublicationLog:
    """Append-only memory of published and rolled-back batches.

    The log never rewrites an entry.  A failed batch is stored with the
    original classification it restored, so callers can always look back at
    what was in force before the attempt.
    """

    entries: list[Publication] = field(default_factory=list)

    def find(self, batch_id: str, baseline_run_id: str | None, capacity: int) -> Publication | None:
        for entry in reversed(self.entries):
            if (entry.batch_id == batch_id and entry.baseline_run_id == baseline_run_id
                    and entry.capacity == capacity and entry.state == "已发布"):
                return entry
        return None

    def latest_original(self) -> Publication | None:
        for entry in reversed(self.entries):
            if entry.state in {"已发布", "已回滚"}:
                return entry
        return None

    def records(self) -> list[dict]:
        return [entry.as_record() for entry in reversed(self.entries)]

    def append(self, entry: Publication) -> Publication:
        self.entries.append(entry)
        return entry


def publication_id(capacity: int, groups: tuple[MergeGroup, ...], policy: MergePolicy,
                   baseline_run_id: str | None) -> str:
    material = dumps({
        "batch": batch_digest(groups),
        "policy": policy.as_record(),
        "capacity": capacity,
        "baseline": baseline_run_id,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(material.encode()).hexdigest()[:16]


def publish(log: PublicationLog, *, groups: tuple[MergeGroup, ...], adjudication: Adjudication,
            policy: MergePolicy, capacity: int, baseline_run_id: str | None,
            recompute) -> tuple[Publication, object]:
    """Run ``recompute`` and publish only if it succeeds.

    ``recompute`` receives the adjudicated zone map and the reviewer scope and
    must return ``(run_id, result)``.  Any exception is re-raised as
    :class:`PublicationError` after recording a rolled-back entry that still
    exposes the original classification.
    """
    batch_id = publication_id(capacity, groups, policy, baseline_run_id)
    original = dict(adjudication.origin_of)
    groups_record = tuple(group.as_record() for group in groups)
    existing = log.find(batch_id, baseline_run_id, capacity)
    if existing is not None:
        return existing, None
    try:
        run_id, result = recompute(adjudication.zone_of, adjudication.reviewer_zones)
    except Exception as error:
        digest = sha256(dumps([batch_id, "已回滚"], sort_keys=True).encode()).hexdigest()[:16]
        log.append(Publication(
            batch_id=batch_id, run_id="", state="已回滚", original_zones=original,
            published_zones=dict(original), held={},
            capacity=capacity, baseline_run_id=baseline_run_id, groups=groups_record, digest=digest,
        ))
        raise PublicationError("归并发布失败，已保留原版归类") from error
    held = {permit: ticket.district for permit, ticket in adjudication.held.items()}
    digest = sha256(dumps([batch_id, run_id, adjudication.zones_after], ensure_ascii=False,
                          sort_keys=True).encode()).hexdigest()[:16]
    entry = Publication(
        batch_id=batch_id, run_id=run_id, state="已发布", original_zones=original,
        published_zones=dict(adjudication.zone_of), held=held, capacity=capacity,
        baseline_run_id=baseline_run_id, groups=groups_record, digest=digest,
    )
    log.append(entry)
    return entry, result


def _counts(values) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        result[value] = result.get(value, 0) + 1
    return dict(sorted(result.items()))
