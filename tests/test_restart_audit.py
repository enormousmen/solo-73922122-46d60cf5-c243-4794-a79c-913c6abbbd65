"""重启核查 API 的关键流程自测。

覆盖：变化检测与起点前基线、窗口边界、同采样时刻后入库为准、
许可相交边界、每许可最多覆盖一次（最大匹配/最小无法覆盖）、
快照读取、参数校验与真实许可端到端流程。
"""
from __future__ import annotations

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import create_app  # noqa: E402
from app.models import PermitRecord  # noqa: E402

MANIFEST = {
    "shards": {
        "shard-a": ["n1", "n2", "n3"],
        "shard-b": ["n2", "n3", "n4"],
        "shard-c": ["n3", "n4", "n5"],
    }
}
UTC = timezone.utc


@pytest.fixture()
def client(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(MANIFEST))
    app = create_app(db_path=str(tmp_path / "permits.db"), manifest_path=str(manifest_path))
    with TestClient(app) as c:
        yield c


def report(client, node, boot, sampled_at, healthy=True):
    return client.post(
        "/liveness",
        json={
            "reports": [
                {
                    "node_id": node,
                    "sampled_at": sampled_at.isoformat(),
                    "healthy": healthy,
                    "boot_id": boot,
                }
            ]
        },
    )


def insert_permit(client, rid, nodes, approved_at, finished_at=None):
    """直接落一条许可，便于精确控制获批/结束时间。"""
    client.app.state.store.insert_permit(
        PermitRecord(
            request_id=rid,
            nodes=tuple(sorted(nodes)),
            status="finished" if finished_at else "open",
            approved_at=approved_at,
            finished_at=finished_at,
            boot_ids_at_approval={n: "boot-x" for n in nodes},
        )
    )


def audit(client, node, start, end):
    return client.get(
        f"/nodes/{node}/restart-audit",
        params={"start": start.isoformat(), "end": end.isoformat()},
    )


def t(hour, minute=0):
    return datetime(2026, 11, 19, hour, minute, tzinfo=UTC)


# ---------- 变化检测与基线 ----------


def test_detects_changes_in_window_with_baseline_before_start(client):
    report(client, "n1", "A", t(9))
    report(client, "n1", "B", t(10))
    report(client, "n1", "C", t(11))

    r = audit(client, "n1", t(10), t(12))
    assert r.status_code == 200, r.text
    body = r.json()

    # 起点前最后一份探活作为对照基线
    assert body["baseline"] == {
        "sampled_at": t(9).isoformat(),
        "healthy": True,
        "boot_id": "A",
    }
    changes = body["changes"]
    assert len(changes) == 2
    assert changes[0]["previous"]["boot_id"] == "A"
    assert changes[0]["current"]["boot_id"] == "B"
    assert changes[0]["possible_restart_window"] == {
        "start": t(9).isoformat(),
        "end": t(10).isoformat(),
        "boundary": "(prev_sample, current_sample]",
    }
    assert changes[1]["previous"]["boot_id"] == "B"
    assert changes[1]["current"]["boot_id"] == "C"
    assert body["summary"] == {
        "total_changes": 2,
        "max_coverable_by_permits": 0,
        "min_uncovered_changes": 2,
    }


def test_window_boundaries_start_inclusive_end_exclusive(client):
    for hour, boot in [(10, "A"), (11, "B"), (12, "C"), (13, "D")]:
        report(client, "n2", boot, t(hour))

    # 后一份探活恰在起点 -> 列出；恰在终点 -> 不列出
    body = audit(client, "n2", t(11), t(13)).json()
    currents = [c["current"]["sampled_at"] for c in body["changes"]]
    assert currents == [t(11).isoformat(), t(12).isoformat()]
    # 基线是起点前最后一份
    assert body["baseline"]["boot_id"] == "A"


def test_changes_fully_before_window_are_not_listed(client):
    report(client, "n1", "A", t(8))
    report(client, "n1", "B", t(9))
    body = audit(client, "n1", t(10), t(12)).json()
    assert body["changes"] == []
    assert body["baseline"]["boot_id"] == "B"  # 基线仍取起点前最后一份
    assert body["summary"]["min_uncovered_changes"] == 0


def test_same_sample_time_later_insert_wins_no_false_change(client):
    # 同一采样时刻两份：后入库的 B 为准
    report(client, "n3", "A", t(10))
    report(client, "n3", "B", t(10))
    report(client, "n3", "B", t(11))  # 标识未变，不是重启
    body = audit(client, "n3", t(10), t(12)).json()
    assert body["changes"] == []

    # 后续真正变化时，对照的是同刻胜出的 B
    report(client, "n3", "C", t(11, 30))
    body = audit(client, "n3", t(10), t(12)).json()
    assert len(body["changes"]) == 1
    assert body["changes"][0]["previous"]["boot_id"] == "B"
    assert body["changes"][0]["current"]["boot_id"] == "C"


def test_no_reports_or_single_report(client):
    body = audit(client, "n5", t(10), t(12)).json()
    assert body["baseline"] is None and body["changes"] == []

    report(client, "n5", "A", t(11))
    body = audit(client, "n5", t(10), t(12)).json()
    assert body["baseline"] is None and body["changes"] == []


# ---------- 候选许可与覆盖 ----------


def test_candidate_permits_intersecting_restart_window(client):
    report(client, "n1", "A", t(9))
    report(client, "n1", "B", t(10))
    report(client, "n1", "C", t(11))

    # p1 在第一次变化窗口 (09:00,10:00] 内有效，10:00 整失效 -> 不覆盖第二次
    insert_permit(client, "p1", ["n1"], t(9, 30), finished_at=t(10))
    # p2 10:30 获批且未结束 -> 只覆盖第二次窗口 (10:00,11:00]
    insert_permit(client, "p2", ["n1"], t(10, 30))

    body = audit(client, "n1", t(10), t(12)).json()
    assert body["changes"][0]["candidate_permits"] == ["p1"]
    assert body["changes"][0]["covered_by"] == "p1"
    assert body["changes"][1]["candidate_permits"] == ["p2"]
    assert body["changes"][1]["covered_by"] == "p2"
    assert body["summary"] == {
        "total_changes": 2,
        "max_coverable_by_permits": 2,
        "min_uncovered_changes": 0,
    }


def test_permit_intersection_boundary_conditions(client):
    # 唯一一次变化，可能重启时段 (10:00, 11:00]
    report(client, "n4", "A", t(10))
    report(client, "n4", "B", t(11))

    insert_permit(client, "p-late", ["n4"], t(11))                 # 11:00 才生效，不相交
    insert_permit(client, "p-expired", ["n4"], t(9), t(10))        # 10:00 整失效，不相交
    insert_permit(client, "p-approve-edge", ["n4"], t(10))         # 10:00 生效，相交
    insert_permit(client, "p-finish-edge", ["n4"], t(9), t(11))    # 11:00 才失效，相交
    insert_permit(client, "p-other-node", ["n5"], t(9))            # 不含本节点

    body = audit(client, "n4", t(10), t(12)).json()
    candidates = body["changes"][0]["candidate_permits"]
    assert candidates == ["p-finish-edge", "p-approve-edge"]
    assert body["changes"][0]["covered_by"] in candidates
    assert body["summary"]["min_uncovered_changes"] == 0


def test_one_permit_covers_at_most_one_change(client):
    # 三次变化，pX 与全部窗口相交但只能覆盖一次
    for hour, boot in [(10, "A"), (11, "B"), (12, "C"), (13, "D")]:
        report(client, "n4", boot, t(hour))
    insert_permit(client, "pX", ["n4"], t(9))

    body = audit(client, "n4", t(10), t(14)).json()
    assert body["summary"] == {
        "total_changes": 3,
        "max_coverable_by_permits": 1,
        "min_uncovered_changes": 2,
    }
    covered = [c["covered_by"] for c in body["changes"]]
    assert covered == ["pX", None, None]


def test_max_matching_minimizes_uncovered_changes(client):
    # 三次变化；pX 覆盖全部，pY 只覆盖后两次 -> 最优可覆盖 2 次
    for hour, boot in [(10, "A"), (11, "B"), (12, "C"), (13, "D")]:
        report(client, "n4", boot, t(hour))
    insert_permit(client, "pX", ["n4"], t(9))
    insert_permit(client, "pY", ["n4"], t(11, 30))

    body = audit(client, "n4", t(10), t(14)).json()
    assert body["summary"]["max_coverable_by_permits"] == 2
    assert body["summary"]["min_uncovered_changes"] == 1
    assigned = {c["covered_by"] for c in body["changes"] if c["covered_by"]}
    assert assigned == {"pX", "pY"}  # 每份许可最多用一次


def test_finished_and_open_permits_both_considered(client):
    report(client, "n1", "A", t(10))
    report(client, "n1", "B", t(11))
    insert_permit(client, "p-done", ["n1"], t(10, 30), finished_at=t(10, 45))
    body = audit(client, "n1", t(10), t(12)).json()
    assert body["changes"][0]["candidate_permits"] == ["p-done"]


def test_long_change_chain_does_not_hit_recursion_limit(client):
    # 1200 次变化（超过 Python 默认递归深度 1000），单许可只能覆盖 1 次
    n_changes = 1200
    for i in range(n_changes + 1):
        report(client, "n3", f"boot-{i % 2}", t(0) + timedelta(minutes=i))
    insert_permit(client, "p1", ["n3"], t(0))
    body = audit(client, "n3", t(0), t(0) + timedelta(minutes=n_changes + 1)).json()
    assert body["summary"]["total_changes"] == n_changes
    assert body["summary"]["max_coverable_by_permits"] == 1
    assert body["summary"]["min_uncovered_changes"] == n_changes - 1


# ---------- 快照一致性 ----------


def test_snapshot_reads_reports_and_permits_together(client):
    report(client, "n1", "A", t(10))
    report(client, "n1", "B", t(11))
    insert_permit(client, "p1", ["n1"], t(10, 30))

    series, permits = client.app.state.store.node_series_snapshot("n1")
    assert [r.boot_id for r in series] == ["A", "B"]
    assert [p.request_id for p in permits] == ["p1"]

    # 核查结束后新写入对下一次核查立即可见（快照不残留）
    report(client, "n1", "C", t(12))
    body = audit(client, "n1", t(10), t(13)).json()
    assert body["summary"]["total_changes"] == 2


def test_snapshot_stable_under_concurrent_writes(client):
    report(client, "n1", "A", t(9))

    def write_many(_):
        for i in range(20):
            report(client, "n1", f"B{i % 3}", t(10) + timedelta(seconds=i))

    def read_many(_):
        for _ in range(20):
            series, permits = client.app.state.store.node_series_snapshot("n1")
            # 同刻去重后序列始终按采样时间升序
            sampled = [r.sampled_at for r in series]
            assert sampled == sorted(sampled)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write_many, range(2)))
        list(pool.map(read_many, range(2)))


# ---------- 参数校验与端到端 ----------


def test_unknown_node_and_invalid_window_rejected(client):
    r = audit(client, "ghost", t(10), t(12))
    assert r.status_code == 422
    assert r.json()["detail"]["unknown_nodes"] == ["ghost"]

    r = audit(client, "n1", t(12), t(12))
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "invalid_time_window"

    r = audit(client, "n1", t(12), t(11))
    assert r.status_code == 422


def test_naive_datetime_treated_as_utc(client):
    report(client, "n1", "A", t(10))
    report(client, "n1", "B", t(11))
    r = client.get(
        "/nodes/n1/restart-audit",
        params={"start": "2026-11-19T10:00:00", "end": "2026-11-19T12:00:00"},
    )
    assert r.status_code == 200
    assert r.json()["window"] == {
        "start": "2026-11-19T10:00:00+00:00",
        "end": "2026-11-19T12:00:00+00:00",
    }
    assert len(r.json()["changes"]) == 1


def test_end_to_end_with_real_permit_flow(client):
    base = datetime.now(UTC) - timedelta(minutes=30)
    for n in ["n1", "n2", "n3", "n4", "n5"]:
        report(client, n, "boot-1", base)
    r = client.post("/permits", json={"request_id": "req-1", "nodes": ["n1"]})
    assert r.status_code == 201

    report(client, "n1", "boot-2", datetime.now(UTC) + timedelta(minutes=5))
    body = audit(
        client,
        "n1",
        datetime.now(UTC) - timedelta(hours=1),
        datetime.now(UTC) + timedelta(hours=1),
    ).json()
    assert len(body["changes"]) == 1
    assert body["changes"][0]["candidate_permits"] == ["req-1"]
    assert body["changes"][0]["covered_by"] == "req-1"
    assert body["summary"]["min_uncovered_changes"] == 0
