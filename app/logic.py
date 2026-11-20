"""许可批准 / 结束的判定逻辑（纯函数，便于单测）。"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime

from .manifest import Manifest
from .models import LatestReport, PermitRecord


@dataclass(frozen=True)
class BootChange:
    """一次启动标识变化（重启嫌疑）。

    previous/current 是相邻两次不同采样时刻的探活：
    重启可能发生在前一次采样之后、后一次采样当时或之前，
    即可能重启时段为 (previous.sampled_at, current.sampled_at]。
    """

    previous: LatestReport
    current: LatestReport

    @property
    def restart_window(self) -> tuple[datetime, datetime]:
        return self.previous.sampled_at, self.current.sampled_at


def _permit_valid_interval(permit: PermitRecord) -> tuple[datetime, datetime | None]:
    """许可有效期：获批时生效，结束时失效；未结束许可终点为 None（仍有效）。"""
    return permit.approved_at, permit.finished_at


def _intervals_overlap(
    window_start: datetime,
    window_end: datetime,
    permit_start: datetime,
    permit_end: datetime | None,
) -> bool:
    """可能重启时段 (window_start, window_end] 与许可有效期
    [permit_start, permit_end) 是否相交。

    闭开区间边界按各自语义处理：
    - 许可恰在窗口上界 window_end 才生效 -> 未覆盖窗口内任何时刻，不相交；
    - 许可恰在窗口下界 window_start 失效 -> 窗口是左开的，不相交。
    """
    # 许可生效时间晚于/等于窗口上界 -> 不相交
    if permit_start >= window_end:
        return False
    # 许可在窗口下界（不含）之前已失效 -> 不相交
    if permit_end is not None and permit_end <= window_start:
        return False
    return True


def detect_boot_changes(
    series: list[LatestReport], start: datetime, end: datetime
) -> tuple[LatestReport | None, list[BootChange]]:
    """在去重探活序列（采样时间升序）中找出窗口内的启动标识变化。

    仅列出后一份探活采样时刻 >= start 且 < end 的变化；
    起点之前的最后一份探活作为对照基线（可能不属于窗口）。
    返回 (基线或 None, 变化列表)。
    """
    baseline: LatestReport | None = None
    changes: list[BootChange] = []
    previous: LatestReport | None = None
    for rep in series:
        if rep.sampled_at < start:
            # 起点前最后一份，随时向前覆盖基线
            baseline = rep
        if previous is not None and rep.boot_id != previous.boot_id:
            if start <= rep.sampled_at < end:
                changes.append(BootChange(previous=previous, current=rep))
        previous = rep
    return baseline, changes


def candidate_permits_for_change(
    change: BootChange, permits: list[PermitRecord], node_id: str
) -> list[PermitRecord]:
    """许可有效期与该变化的可能重启时段相交、且覆盖该节点，即为候选。"""
    window_start, window_end = change.restart_window
    out: list[PermitRecord] = []
    for p in permits:
        if node_id not in p.nodes:
            continue
        p_start, p_end = _permit_valid_interval(p)
        if _intervals_overlap(window_start, window_end, p_start, p_end):
            out.append(p)
    return out


def _maximum_match(
    change_candidates: list[list[PermitRecord]],
) -> dict[int, str]:
    """二分图最大匹配：左侧变化、右侧许可（每许可最多覆盖一次变化）。

    候选已按获批时间/申请号有序，Kuhn 增广路按序尝试，结果稳定可复现。
    返回 {变化下标: 许可号}（仅含被匹配的变化）。
    """
    # 增广路递归深度最多为变化数，按输入规模抬高递归上限，避免长序列时 RecursionError
    old_limit = sys.getrecursionlimit()
    needed = len(change_candidates) + 100
    if needed > old_limit:
        sys.setrecursionlimit(needed)

    # 许可 -> 匹配到的变化下标
    matched_permit: dict[str, int] = {}

    def augment(ci: int, seen: set[str]) -> bool:
        for p in change_candidates[ci]:
            if p.request_id in seen:
                continue
            seen.add(p.request_id)
            owner = matched_permit.get(p.request_id)
            if owner is None or augment(owner, seen):
                matched_permit[p.request_id] = ci
                return True
        return False

    try:
        for ci in range(len(change_candidates)):
            augment(ci, set())
    finally:
        sys.setrecursionlimit(old_limit)

    return {ci: rid for rid, ci in matched_permit.items()}


def analyze_restarts(
    node_id: str,
    start: datetime,
    end: datetime,
    series: list[LatestReport],
    permits: list[PermitRecord],
) -> dict:
    """核查节点在 [start, end) 内的启动标识变化及许可覆盖情况。

    每份许可在本次核查中最多覆盖一次变化；用最大匹配给出"至少几次
    无法由许可覆盖"（总变化数 - 可覆盖的最大变化数）。
    """
    baseline, changes = detect_boot_changes(series, start, end)

    change_candidates: list[list[PermitRecord]] = []
    for change in changes:
        candidates = candidate_permits_for_change(change, permits, node_id)
        change_candidates.append(candidates)

    matched = _maximum_match(change_candidates)
    total = len(changes)
    covered = len(matched)

    intervals: list[dict] = []
    for idx, change in enumerate(changes):
        window_start, window_end = change.restart_window
        intervals.append(
            {
                "previous": _report_view(change.previous),
                "current": _report_view(change.current),
                "possible_restart_window": {
                    "start": window_start,
                    "end": window_end,
                    "boundary": "(prev_sample, current_sample]",
                },
                "candidate_permits": [p.request_id for p in change_candidates[idx]],
                "covered_by": matched.get(idx),
            }
        )

    return {
        "node_id": node_id,
        "window": {"start": start, "end": end},
        "baseline": _report_view(baseline) if baseline is not None else None,
        "changes": intervals,
        "summary": {
            "total_changes": total,
            "max_coverable_by_permits": covered,
            "min_uncovered_changes": total - covered,
        },
    }


def _report_view(rep: LatestReport) -> dict:
    return {
        "sampled_at": rep.sampled_at,
        "healthy": rep.healthy,
        "boot_id": rep.boot_id,
    }


def evaluate_permit_request(
    manifest: Manifest,
    batch: list[str],
    latest: dict[str, LatestReport],
    open_permits: list[PermitRecord],
) -> tuple[bool, dict]:
    """校验新申请。返回 (是否批准, 详情)。

    批准条件（同时满足）：
    1. 本批每个节点最新探活都健康；
    2. 本批节点未被任何未结束许可占用；
    3. 扣除本批节点、未结束许可占用节点、健康未知或异常节点后，
       本批涉及的每个分片剩余可用投票副本均过半。
    拒绝时详情里带上所有不满足的类别，票数不足时列出受影响分片。
    """
    batch_set = set(batch)

    # 条件 1：本批节点健康
    health_unknown = [n for n in batch if n not in latest]
    unhealthy = [n for n in batch if n in latest and not latest[n].healthy]

    # 条件 2：未结束许可占用
    node_owner: dict[str, str] = {}
    for p in open_permits:
        for n in p.nodes:
            node_owner[n] = p.request_id
    occupied = {n: node_owner[n] for n in batch if n in node_owner}

    # 条件 3：可用性扣除。excluded: node -> 扣除原因
    excluded: dict[str, str] = {}
    for n in batch:
        excluded[n] = "in_batch"
    for n, owner in sorted(node_owner.items()):
        excluded.setdefault(n, f"occupied_by:{owner}")
    for n in sorted(manifest.nodes):
        if n in excluded:
            continue
        rep = latest.get(n)
        if rep is None:
            excluded[n] = "health_unknown"
        elif not rep.healthy:
            excluded[n] = "unhealthy"

    affected_shards = []
    for shard, voters in sorted(manifest.shards.items()):
        if not batch_set.intersection(voters):
            continue  # 只检查本批涉及的分片
        available = [v for v in voters if v not in excluded]
        needed = manifest.majority_needed(shard)
        if len(available) < needed:
            affected_shards.append(
                {
                    "shard": shard,
                    "voters": len(voters),
                    "required_available": needed,
                    "available": len(available),
                    "available_nodes": available,
                    "excluded": {v: excluded[v] for v in voters if v in excluded},
                }
            )

    ok = not (health_unknown or unhealthy or occupied or affected_shards)
    detail: dict = {"ok": ok}
    if not ok:
        detail["error"] = "permit_rejected"
        if health_unknown:
            detail["health_unknown_nodes"] = health_unknown
        if unhealthy:
            detail["unhealthy_nodes"] = unhealthy
        if occupied:
            detail["occupied_nodes"] = [
                {"node": n, "permit": owner} for n, owner in sorted(occupied.items())
            ]
        if affected_shards:
            detail["affected_shards"] = affected_shards
    return ok, detail


def evaluate_finish(
    permit: PermitRecord, latest: dict[str, LatestReport]
) -> dict[str, str]:
    """结束许可的校验。返回 {节点: 不满足原因}，空 dict 表示可以结束。

    本批每个节点都须满足：最新探活在获批后采集、显示健康、
    启动标识与获批时不同。
    """
    problems: dict[str, str] = {}
    for n in permit.nodes:
        rep = latest.get(n)
        if rep is None:
            problems[n] = "no_liveness_report"
        elif rep.sampled_at <= permit.approved_at:
            problems[n] = "not_sampled_after_approval"
        elif not rep.healthy:
            problems[n] = "unhealthy"
        elif rep.boot_id == permit.boot_ids_at_approval[n]:
            problems[n] = "boot_id_unchanged"
    return problems
