"""FastAPI 应用：分批重启许可 API。"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query, Response

from .logic import analyze_restarts, evaluate_finish, evaluate_permit_request
from .manifest import load_manifest
from .models import PermitRecord, now_utc, to_iso
from .schemas import LivenessBatchIn, PermitRequestIn
from .store import Store


def create_app(db_path: str, manifest_path: str) -> FastAPI:
    manifest = load_manifest(manifest_path)
    store = Store(db_path)

    app = FastAPI(title="restart-permit-api", version="1.0.0")
    app.state.store = store
    app.state.manifest = manifest

    def permit_view(p: PermitRecord) -> dict:
        return {
            "request_id": p.request_id,
            "nodes": list(p.nodes),
            "status": p.status,
            "approved_at": to_iso(p.approved_at),
            "finished_at": to_iso(p.finished_at) if p.finished_at else None,
            "boot_ids_at_approval": p.boot_ids_at_approval,
        }

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/manifest")
    def get_manifest():
        return {
            "shards": {
                name: {
                    "voters": list(voters),
                    "majority_needed": manifest.majority_needed(name),
                }
                for name, voters in sorted(manifest.shards.items())
            }
        }

    # ---- 探活报告 ----

    @app.post("/liveness", status_code=201)
    def post_liveness(batch: LivenessBatchIn):
        unknown = sorted({r.node_id for r in batch.reports} - manifest.nodes)
        if unknown:
            raise HTTPException(
                status_code=422,
                detail={"error": "unknown_nodes", "unknown_nodes": unknown},
            )
        store.add_reports(batch.reports)
        return {"accepted": len(batch.reports)}

    @app.get("/liveness")
    def get_all_liveness():
        latest = store.latest_reports()
        return {
            "nodes": {
                n: {
                    "sampled_at": to_iso(r.sampled_at),
                    "healthy": r.healthy,
                    "boot_id": r.boot_id,
                }
                for n, r in sorted(latest.items())
            },
            "nodes_without_reports": sorted(manifest.nodes - latest.keys()),
        }

    @app.get("/liveness/{node_id}")
    def get_node_liveness(node_id: str):
        rep = store.latest_reports().get(node_id)
        if rep is None:
            raise HTTPException(
                status_code=404,
                detail={"error": "no_liveness_report", "node": node_id},
            )
        return {
            "node_id": node_id,
            "sampled_at": to_iso(rep.sampled_at),
            "healthy": rep.healthy,
            "boot_id": rep.boot_id,
        }

    # ---- 许可 ----

    @app.post("/permits", status_code=201)
    def request_permit(req: PermitRequestIn, response: Response):
        nodes = sorted(set(req.nodes))  # 规范化：去重排序
        unknown = [n for n in nodes if n not in manifest.nodes]
        if unknown:
            raise HTTPException(
                status_code=422,
                detail={"error": "unknown_nodes", "unknown_nodes": unknown},
            )
        with store.transaction():  # 并发申请在此串行，守住多数票
            existing = store.get_permit(req.request_id)
            if existing is not None:
                if list(existing.nodes) == nodes:
                    # 同号同批重发 -> 返回原许可
                    response.status_code = 200
                    return permit_view(existing)
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "request_id_conflict",
                        "message": "request_id 已存在且节点集合不同",
                        "existing_nodes": list(existing.nodes),
                    },
                )
            latest = store.latest_reports()
            ok, detail = evaluate_permit_request(
                manifest, nodes, latest, store.open_permits()
            )
            if not ok:
                raise HTTPException(status_code=409, detail=detail)
            record = PermitRecord(
                request_id=req.request_id,
                nodes=tuple(nodes),
                status="open",
                approved_at=now_utc(),
                finished_at=None,
                boot_ids_at_approval={n: latest[n].boot_id for n in nodes},
            )
            store.insert_permit(record)
            return permit_view(record)

    @app.get("/permits")
    def list_permits(status: str | None = Query(default=None, pattern="^(open|finished)$")):
        return {"permits": [permit_view(p) for p in store.list_permits(status)]}

    @app.get("/permits/{request_id}")
    def get_permit(request_id: str):
        p = store.get_permit(request_id)
        if p is None:
            raise HTTPException(
                status_code=404,
                detail={"error": "permit_not_found", "request_id": request_id},
            )
        return permit_view(p)

    @app.post("/permits/{request_id}/finish")
    def finish_permit(request_id: str):
        with store.transaction():
            p = store.get_permit(request_id)
            if p is None:
                raise HTTPException(
                    status_code=404,
                    detail={"error": "permit_not_found", "request_id": request_id},
                )
            if p.status == "finished":
                raise HTTPException(
                    status_code=409,
                    detail={"error": "permit_already_finished", "request_id": request_id},
                )
            problems = evaluate_finish(p, store.latest_reports())
            if problems:
                raise HTTPException(
                    status_code=409,
                    detail={"error": "finish_conditions_not_met", "nodes": problems},
                )
            store.mark_finished(request_id, now_utc())
            return permit_view(store.get_permit(request_id))

    # ---- 重启核查（只读） ----

    @app.get("/nodes/{node_id}/restart-audit")
    def restart_audit(
        node_id: str,
        start: datetime = Query(description="核查起点 UTC（含），ISO8601"),
        end: datetime = Query(description="核查终点 UTC（不含），ISO8601"),
    ):
        if node_id not in manifest.nodes:
            raise HTTPException(
                status_code=422,
                detail={"error": "unknown_nodes", "unknown_nodes": [node_id]},
            )
        # 不带时区按 UTC 处理，统一归一到 UTC
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        start = start.astimezone(timezone.utc)
        end = end.astimezone(timezone.utc)
        if start >= end:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "invalid_time_window",
                    "message": "start 必须早于 end（窗口为 [start, end)）",
                    "start": to_iso(start),
                    "end": to_iso(end),
                },
            )

        # 探活与许可取自同一已提交快照；核查期间上报与许可操作照常受理
        series, permits = store.node_series_snapshot(node_id)
        result = analyze_restarts(node_id, start, end, series, permits)
        return _stringify_datetimes(result)

    return app


def _stringify_datetimes(obj):
    """递归把结果中的 datetime 转为 ISO8601 字符串（UTC）。"""
    if isinstance(obj, datetime):
        return to_iso(obj)
    if isinstance(obj, dict):
        return {k: _stringify_datetimes(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_stringify_datetimes(v) for v in obj]
    return obj


def create_app_from_env() -> FastAPI:
    return create_app(
        db_path=os.environ.get("DB_PATH", "./data/permits.db"),
        manifest_path=os.environ.get("MANIFEST_PATH", "./manifest.json"),
    )


app = create_app_from_env()
