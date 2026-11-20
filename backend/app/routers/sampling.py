"""数据源采样清单与派发记录接口：生成轮次、领用待执行项、确认派发、查看来源版本。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.sampling import (
    DISPATCH_UNCONFIRMED,
    ITEM_CLAIMED,
    ITEM_DISPATCHED,
    ITEM_PENDING,
    ITEM_VOIDED,
    parse_round_at,
    sampling_service,
)

router = APIRouter(prefix="/api/sampling", tags=["数据源采样清单"])

ITEM_STATUSES = [ITEM_PENDING, ITEM_CLAIMED, ITEM_DISPATCHED, ITEM_VOIDED]


def _parse(value: Any, *, date: Any = None) -> datetime:
    try:
        return parse_round_at(value, date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/summary")
def summary() -> dict[str, Any]:
    """采样看板：各状态轮次与有效派发的数量。"""
    items = sampling_service.list_items(page=1, size=100000)[0]
    dispatches = sampling_service.list_dispatches(page=1, size=100000)[0]
    counts = {status: 0 for status in ITEM_STATUSES}
    for item in items:
        counts[item.get("status")] = counts.get(item.get("status"), 0) + 1
    valid = sum(1 for row in dispatches if row.get("valid"))
    return {
        "items": len(items),
        "pending": counts.get(ITEM_PENDING, 0),
        "claimed": counts.get(ITEM_CLAIMED, 0),
        "dispatched": counts.get(ITEM_DISPATCHED, 0),
        "voided": counts.get(ITEM_VOIDED, 0),
        "dispatches": len(dispatches),
        "valid_dispatches": valid,
        "unconfirmed": sum(1 for row in dispatches if row.get("status") == DISPATCH_UNCONFIRMED),
    }


@router.get("/items", response_model=PageResult[dict])
def list_items(
    status: str | None = Query(default=None, description="待领用、已领用、已派发、已作废"),
    source_id: int | None = Query(default=None, description="按来源 ID 过滤"),
    round_key: str | None = Query(default=None, description="按轮次过滤，如 2026-11-20T12:20"),
    keyword: str | None = Query(default=None, description="按来源编号或接入地址检索"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """查询采样清单（按启用状态与周期编排出的轮次）。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = sampling_service.list_items(
        status=status, source_id=source_id, round_key=round_key, keyword=keyword, page=page, size=size
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("/rounds", response_model=ActionResult)
def generate_rounds(payload: EntryPayload) -> ActionResult:
    """生成采集轮次：按 start/end（或 start + count）扫描，只落已启用来源的对齐分钟。"""
    values = payload.values
    start = _parse(values.get("start"), date=values.get("date"))
    end_raw = values.get("end")
    count = values.get("count")
    try:
        result = sampling_service.generate_range(
            start,
            end=_parse(end_raw, date=values.get("date")) if end_raw else None,
            count=int(count) if count not in (None, "") else None,
            step_minutes=int(values.get("step_minutes") or 1),
        )
    except ValueError as exc:
        return ActionResult(ok=False, message=str(exc))
    return ActionResult(
        ok=True,
        message=(
            f"{result['start']} ~ {result['end']} 已编排采集轮次："
            f"新增 {result['created_count']} 条、刷新 {result['refreshed_count']} 条、"
            f"跳过 {result['skipped_count']} 条"
        ),
        entry=result,
    )


@router.post("/items/{item_id}/claim", response_model=ActionResult)
def claim_item(item_id: int, payload: EntryPayload) -> ActionResult:
    """领用一条待执行的采样项，生成唯一派发记录（待确认）。"""
    dispatch, message = sampling_service.claim(item_id, payload.values.get("operator") or payload.remark)
    if dispatch is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=dispatch)


@router.get("/dispatches", response_model=PageResult[dict])
def list_dispatches(
    status: str | None = Query(default=None, description="待确认、已派发、已作废"),
    source_id: int | None = Query(default=None, description="按来源 ID 过滤"),
    round_key: str | None = Query(default=None, description="按轮次过滤"),
    valid: bool | None = Query(default=None, description="只看有效/无效派发"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """查询派发记录；同一来源同一轮次最多一条有效记录。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = sampling_service.list_dispatches(
        status=status, source_id=source_id, round_key=round_key, valid=valid, page=page, size=size
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("/dispatches/{dispatch_id}/confirm", response_model=ActionResult)
def confirm_dispatch(dispatch_id: int, payload: EntryPayload) -> ActionResult:
    """确认派发；来源已停用时，未确认的记录会被作废而不是派出。"""
    dispatch, message = sampling_service.confirm(
        dispatch_id, payload.values.get("operator") or payload.remark
    )
    if dispatch is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=dispatch)


@router.get("/versions", response_model=dict)
def list_versions(source_id: int = Query(..., description="数据源 ID")) -> dict[str, Any]:
    """查看某个来源的地址/周期版本历史，最新一版为生效中。"""
    versions = sampling_service.list_versions(source_id)
    return {"source_id": source_id, "total": len(versions), "items": versions}
