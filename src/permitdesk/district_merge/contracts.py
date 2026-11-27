"""Contracts and normalisation for adjudicated district merges.

A merge batch is a set of independent groups.  Every group combines two or
more existing districts under one jurisdiction label.  Batches are normalised
before use so that re-submitting the same business request -- in any input
order, and with exact duplicate groups repeated -- produces the same digest
and therefore the same publication.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from math import isfinite


DEFAULT_SEPARATOR = "·"
MAX_GROUPS = 64
MAX_DISTRICTS = 16


class MergePlanError(ValueError):
    """Raised when a merge batch cannot be interpreted safely."""


@dataclass(frozen=True, slots=True)
class MergeGroup:
    name: str
    districts: tuple[str, ...]

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name.strip():
            raise MergePlanError("归并后的辖区名称不能为空")
        if not isinstance(self.districts, tuple):
            raise MergePlanError("归并辖区须为序列")
        if not 2 <= len(self.districts) <= MAX_DISTRICTS:
            raise MergePlanError("每组归并须包含 2 至 16 个辖区")
        if len(set(self.districts)) != len(self.districts):
            raise MergePlanError("同一辖区不能在归并组内重复")
        if any(not isinstance(item, str) or not item.strip() for item in self.districts):
            raise MergePlanError("归并辖区名称不能为空")
        # Canonical order so equal business requests compare equal regardless
        # of how the caller listed the districts.
        object.__setattr__(self, "districts", tuple(sorted(self.districts)))

    @property
    def sources(self) -> tuple[str, ...]:
        return tuple(sorted(self.districts))

    def as_record(self) -> dict:
        return {"name": self.name, "districts": list(self.sources)}


@dataclass(frozen=True, slots=True)
class MergePolicy:
    at: int
    max_held_per_group: int = 2
    max_conflict_share: float = .34
    min_samples: int = 20

    def __post_init__(self):
        if type(self.at) is not int or not 0 <= self.at <= 50000:
            raise MergePlanError("归并发布时点越界")
        if type(self.max_held_per_group) is not int or not 0 <= self.max_held_per_group <= 100:
            raise MergePlanError("单组待核上限越界")
        if isinstance(self.max_conflict_share, bool) or not isinstance(self.max_conflict_share, (int, float)):
            raise MergePlanError("边界矛盾占比须为数值")
        if not isfinite(self.max_conflict_share) or not 0 <= self.max_conflict_share <= 1:
            raise MergePlanError("边界矛盾占比越界")
        if type(self.min_samples) is not int or not 0 <= self.min_samples <= 10000:
            raise MergePlanError("归并评分样本下限越界")

    @classmethod
    def parse(cls, row, default_at: int) -> "MergePolicy":
        if row is None:
            row = {}
        if not isinstance(row, dict):
            raise MergePlanError("归并策略须为对象")
        allowed = {"at", "max_held_per_group", "max_conflict_share", "min_samples"}
        unknown = set(row) - allowed
        if unknown:
            raise MergePlanError("归并策略含有无法识别的字段")
        return cls(at=row.get("at", default_at),
                   max_held_per_group=row.get("max_held_per_group", 2),
                   max_conflict_share=row.get("max_conflict_share", .34),
                   min_samples=row.get("min_samples", 20))

    def as_record(self) -> dict:
        return {"at": self.at, "max_held_per_group": self.max_held_per_group,
                "max_conflict_share": self.max_conflict_share, "min_samples": self.min_samples}


def _clean_name(value, label: str) -> str:
    if not isinstance(value, str):
        raise MergePlanError(f"{label}必须是文本")
    value = value.strip()
    if not value:
        raise MergePlanError(f"{label}不能为空")
    return value


def _group_from_districts(districts, name=None) -> MergeGroup:
    if isinstance(districts, (str, bytes)) or not isinstance(districts, (list, tuple)):
        raise MergePlanError("归并辖区须为序列")
    if len(districts) > MAX_DISTRICTS:
        raise MergePlanError("单组归并辖区过多")
    cleaned = tuple(_clean_name(item, "归并辖区") for item in districts)
    label = _clean_name(name, "归并后的辖区名称") if name is not None else DEFAULT_SEPARATOR.join(sorted(cleaned))
    return MergeGroup(label, cleaned)


def parse_batch(raw) -> tuple[MergeGroup, ...]:
    """Normalise supported batch shapes into ordered, de-duplicated groups.

    Accepted shapes::

        [["政西", "河口"], ...]
        [{"districts": ["政西", "河口"], "name": "西岸"}, ...]
        {"西岸": ["政西", "河口"], ...}
        {"merges": <any of the above>}
        [] or None
    """
    if raw is None:
        return ()
    if isinstance(raw, dict):
        if "merges" in raw:
            if len(raw) != 1:
                raise MergePlanError("归并批次对象只能包含 merges")
            return parse_batch(raw["merges"])
        if "districts" in raw:
            raise MergePlanError("归并批次须为数组")
        groups = tuple(_group_from_districts(sources, target) for target, sources in raw.items())
        return _deduplicate(groups)
    if isinstance(raw, (str, bytes)) or not isinstance(raw, (list, tuple)):
        raise MergePlanError("归并批次须为数组")
    if len(raw) > MAX_GROUPS:
        raise MergePlanError("归并组数量过多")
    groups = []
    for item in raw:
        if isinstance(item, dict):
            if "districts" not in item:
                raise MergePlanError("归并组缺少 districts")
            unknown = set(item) - {"districts", "name", "into", "target"}
            if unknown:
                raise MergePlanError("归并组含有无法识别的字段")
            labels = [item[key] for key in ("name", "into", "target") if item.get(key) is not None]
            cleaned = {_clean_name(label, "归并后的辖区名称") for label in labels}
            if len(cleaned) > 1:
                raise MergePlanError("归并组的目标名称不一致")
            groups.append(_group_from_districts(item["districts"], labels[0] if labels else None))
        elif isinstance(item, (list, tuple)):
            groups.append(_group_from_districts(item))
        else:
            raise MergePlanError("归并组须为辖区数组或对象")
    return _deduplicate(tuple(groups))


def _deduplicate(groups: tuple[MergeGroup, ...]) -> tuple[MergeGroup, ...]:
    unique: dict[tuple[str, tuple[str, ...]], MergeGroup] = {}
    for group in groups:
        unique.setdefault((group.name, group.sources), group)
    ordered = tuple(sorted(unique.values(), key=lambda group: (group.name, group.sources)))
    names: dict[str, MergeGroup] = {}
    owners: dict[str, MergeGroup] = {}
    for group in ordered:
        previous = names.get(group.name)
        if previous is not None and previous.districts != group.districts:
            raise MergePlanError("归并后的辖区名称重复")
        names[group.name] = group
        for district in group.districts:
            owner = owners.get(district)
            if owner is not None and owner is not group:
                raise MergePlanError("同一辖区不能进入多个归并组")
            owners[district] = group
    return ordered


def batch_digest(groups: tuple[MergeGroup, ...]) -> str:
    material = json.dumps([group.as_record() for group in groups], ensure_ascii=False,
                          sort_keys=True, separators=(",", ":"))
    return sha256(material.encode()).hexdigest()


def known_districts(*sources) -> set[str]:
    """Collect district names from permit, calendar, roster and history views.

    Every source is an iterable of district names.  Objects carrying a
    ``district`` attribute are also accepted so callers need not pre-map.
    """
    universe: set[str] = set()
    for source in sources:
        if not source:
            continue
        if isinstance(source, str):
            if source.strip():
                universe.add(source.strip())
            continue
        for item in source:
            if isinstance(item, str) and item.strip():
                universe.add(item.strip())
            elif hasattr(item, "district") and isinstance(item.district, str):
                universe.add(item.district.strip())
    return universe
