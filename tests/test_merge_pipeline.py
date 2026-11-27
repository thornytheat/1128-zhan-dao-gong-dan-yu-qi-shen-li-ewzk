import pytest

from permitdesk import pipeline
from permitdesk.pipeline import publish_merges, run
from permitdesk.district_merge.contracts import MergePlanError
from permitdesk.district_merge.publication import PublicationLog


MERGE = [["政西", "河口"]]


def test_baseline_is_unchanged_without_a_batch():
    assert run() == run(None, None)
    packet, report = publish_merges(8, [])
    assert report["state"] == "未启用"
    assert packet == run(8)
    assert "merge" not in packet


def test_merge_recomputes_queue_and_capacity_without_adding_stages():
    baseline = run(8)
    merged, report = publish_merges(8, MERGE)
    assert report["state"] == "已归并"
    assert len(merged["stages"]) == len(baseline["stages"]) == 6
    assert merged["merge"]["groups"][0]["state"] == "生效"
    assert {row["district"] for row in merged["rows"]} == {"政西·河口", "新坊"}
    assert report["plan"]["queue_after"]["政西·河口"] == sum(
        report["plan"]["queue_before"][district] for district in ("政西", "河口"))
    assert merged["metrics"]["已安排复核"] == baseline["metrics"]["已安排复核"] == 8
    recomputed = report["recomputed"]
    assert recomputed["scheduled_after"] == merged["metrics"]["已安排复核"]
    assert recomputed["by_zone_after"].keys() <= {"政西·河口", "新坊"}


def test_scores_actually_move_after_merging():
    baseline = {row["permit"]: row["risk"] for row in run(8)["rows"]}
    merged = {row["permit"]: row["risk"] for row in publish_merges(8, MERGE)[0]["rows"]}
    assert baseline != merged


def test_merge_on_sample_data_is_deterministic_and_idempotent():
    first, report = publish_merges(8, MERGE)
    second, report_again = publish_merges(8, MERGE)
    assert first == second
    assert report["publication"]["digest"] == report_again["publication"]["digest"]


def test_merge_batch_order_does_not_change_the_result():
    reversed_batch = [["河口", "政西"]]
    assert publish_merges(8, MERGE)[0] == publish_merges(8, reversed_batch)[0]


def test_deferred_group_keeps_baseline_zones_and_run_id():
    baseline = run(8)
    packet, report = publish_merges(8, [["政西", "不存在区"]])
    assert report["groups"][0]["state"] == "暂缓"
    assert {row["district"] for row in packet["rows"]} == {row["district"] for row in baseline["rows"]}
    assert packet["merge"]["state"] == "未归并"


def test_publication_after_shift_opening_is_refused_before_recompute():
    with pytest.raises(ValueError, match="开班"):
        publish_merges(8, MERGE, {"at": 9999})


def test_invalid_batch_is_refused():
    with pytest.raises(MergePlanError):
        publish_merges(8, [["政西", "河口"], ["河口", "新坊"]])


def test_midway_failure_keeps_original_classification_recoverable(monkeypatch):
    baseline = run(8)
    log = PublicationLog()
    compose = pipeline._compose
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("特征重算崩溃")
        return compose(*args, **kwargs)

    monkeypatch.setattr(pipeline, "_compose", flaky)
    packet, report = pipeline.publish_merges(8, MERGE, journal=log)
    assert report["state"] == "已回滚"
    assert report["run_id"] is None
    assert packet["merge"]["state"] == "已回滚"
    assert {row["district"] for row in packet["rows"]} == {row["district"] for row in baseline["rows"]}
    assert log.entries[-1].state == "已回滚"
    original = log.latest_original()
    assert original is not None and original.published_zones == original.original_zones
