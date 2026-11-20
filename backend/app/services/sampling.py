"""数据源采样轮次、采样清单与派发记录的业务规则。

核心约束：

1. 只有「已启用」的数据源会生成采集轮次，轮次按该来源的采样周期对齐；
2. 每个轮次在生成时把「接入地址 + 采样周期」按同一个来源版本快照下来，
   截止时间也由同一版本计算，三者成套生效，不允许跨版本拼接；
3. 同一来源的同一轮次最多只有一条采样清单记录、最多只有一条有效派发记录；
4. 来源停用与领用并发时，由同一把锁串行化：停用后尚未确认的派发记录一律作废，
   待领用记录也不能再派发。
"""
from __future__ import annotations

import re
import threading
from datetime import datetime, timedelta
from typing import Any

from app.store import store

DATASOURCE_MODULE = "datasource"
VERSION_MODULE = "source_version"
SAMPLING_MODULE = "sampling_item"
DISPATCH_MODULE = "dispatch"

DEFAULT_PERIOD_MINUTES = 10
MINUTE_KEY_FORMAT = "%Y-%m-%dT%H:%M"
DISPLAY_FORMAT = "%Y-%m-%d %H:%M"

SOURCE_ENABLED = "已启用"

ITEM_PENDING = "待领用"
ITEM_CLAIMED = "已领用"
ITEM_DISPATCHED = "已派发"
ITEM_VOIDED = "已作废"

DISPATCH_UNCONFIRMED = "待确认"
DISPATCH_CONFIRMED = "已派发"
DISPATCH_VOIDED = "已作废"

ACTIVE_DISPATCH_STATUSES = {DISPATCH_UNCONFIRMED, DISPATCH_CONFIRMED}


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def parse_period_minutes(raw: Any) -> int:
    """把「10」「10分钟」「每 15 分钟」等写法统一成分钟数。"""
    match = re.search(r"\d+", str(raw or ""))
    if not match:
        return DEFAULT_PERIOD_MINUTES
    minutes = int(match.group(0))
    return minutes if minutes > 0 else DEFAULT_PERIOD_MINUTES


def parse_round_at(value: Any = None, date: Any = None) -> datetime:
    """解析轮次时间，支持完整时间、只给 HH:MM，或不传（取当前分钟）。"""
    if value is None or str(value).strip() == "":
        base = datetime.now()
        return base.replace(second=0, microsecond=0)
    text = str(value).strip().replace(" ", "T")
    if re.fullmatch(r"\d{1,2}:\d{2}", text):
        day = str(date).strip() if date else datetime.now().strftime("%Y-%m-%d")
        text = f"{day}T{text}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"无法识别轮次时间「{value}」，请使用 2026-11-20T12:20 或 12:20") from exc
    return parsed.replace(second=0, microsecond=0)


def round_key_of(moment: datetime) -> str:
    return moment.strftime(MINUTE_KEY_FORMAT)


def display_of(moment: datetime) -> str:
    return moment.strftime(DISPLAY_FORMAT)


class SamplingService:
    """所有会改变采样清单/派发记录的操作都在这把锁内完成。"""

    def __init__(self) -> None:
        self.lock = threading.RLock()

    # ------------------------------------------------------------------ 版本
    def list_versions(self, source_id: int) -> list[dict[str, Any]]:
        versions = [row for row in store.rows(VERSION_MODULE) if int(row.get("source_id", 0)) == source_id]
        return sorted(versions, key=lambda row: int(row.get("版本号", 0)))

    def latest_version(self, source_id: int) -> dict[str, Any] | None:
        versions = self.list_versions(source_id)
        return versions[-1] if versions else None

    def record_version(self, entry: dict[str, Any]) -> dict[str, Any]:
        """按数据源当前字段登记版本；地址与周期没有变化时复用上一版。"""
        with self.lock:
            return self._record_version_locked(entry)

    def _record_version_locked(self, entry: dict[str, Any]) -> dict[str, Any]:
        source_id = int(entry.get("id", 0))
        period = parse_period_minutes(entry.get("采样周期"))
        address = str(entry.get("接入地址") or "").strip()
        latest = self.latest_version(source_id)
        if (
            latest is not None
            and str(latest.get("接入地址") or "") == address
            and int(latest.get("采样周期分钟") or 0) == period
        ):
            entry["来源版本"] = int(latest.get("版本号", 1))
            return latest

        rows = store.rows(VERSION_MODULE)
        version_no = max(
            (int(row.get("版本号", 0)) for row in rows if int(row.get("source_id", 0)) == source_id),
            default=0,
        ) + 1
        for row in rows:
            if int(row.get("source_id", 0)) == source_id:
                row["状态"] = "已归档"

        version = {
            "id": _next_id(rows),
            "source_id": source_id,
            "来源编号": entry.get("源端编号"),
            "版本号": version_no,
            "接入地址": address,
            "采样周期": entry.get("采样周期") or f"{period}分钟",
            "采样周期分钟": period,
            "生效时间": _now_text(),
            "状态": "生效中",
        }
        rows.append(version)
        entry["来源版本"] = version_no
        return version

    def ensure_version(self, entry: dict[str, Any]) -> dict[str, Any]:
        return self.record_version(entry)

    # ------------------------------------------------------------- 轮次生成
    def generate_round(self, moment: datetime) -> dict[str, list[dict[str, Any]]]:
        """为某一分钟生成采集轮次；只处理按周期对齐且已启用的数据源。"""
        with self.lock:
            created: list[dict[str, Any]] = []
            refreshed: list[dict[str, Any]] = []
            skipped: list[dict[str, Any]] = []
            key = round_key_of(moment)
            minute_of_day = moment.hour * 60 + moment.minute

            for entry in list(store.rows(DATASOURCE_MODULE)):
                if str(entry.get("status") or "") != SOURCE_ENABLED:
                    continue
                version = self._record_version_locked(entry)
                period = int(version.get("采样周期分钟") or DEFAULT_PERIOD_MINUTES)
                if period <= 0 or minute_of_day % period != 0:
                    continue

                existing = self._find_item(int(entry.get("id", 0)), key)
                if existing is None:
                    item = self._build_item(entry, version, moment)
                    store.rows(SAMPLING_MODULE).append(item)
                    created.append(item)
                elif existing.get("status") == ITEM_PENDING:
                    if self._apply_snapshot(existing, version, moment):
                        refreshed.append(existing)
                    else:
                        skipped.append(existing)
                else:
                    # 已领用、已派发或已作废的轮次保持原样，避免一轮多次派发。
                    skipped.append(existing)

            return {"created": created, "refreshed": refreshed, "skipped": skipped}

    def generate_range(
        self,
        start: datetime,
        *,
        end: datetime | None = None,
        count: int | None = None,
        step_minutes: int = 1,
    ) -> dict[str, Any]:
        """从 start 起逐分钟扫描到 end，按各来源周期落轮次。"""
        step = max(int(step_minutes or 1), 1)
        if end is None:
            slots = max(int(count or 1), 1)
            end = start + timedelta(minutes=(slots - 1) * step)
        if end < start:
            raise ValueError("轮次结束时间不能早于开始时间")

        created: list[dict[str, Any]] = []
        refreshed: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        cursor = start
        while cursor <= end:
            result = self.generate_round(cursor)
            created.extend(result["created"])
            refreshed.extend(result["refreshed"])
            skipped.extend(result["skipped"])
            cursor += timedelta(minutes=step)
        return {
            "start": display_of(start),
            "end": display_of(end),
            "created": created,
            "refreshed": refreshed,
            "skipped": skipped,
            "created_count": len(created),
            "refreshed_count": len(refreshed),
            "skipped_count": len(skipped),
        }

    def _find_item(self, source_id: int, key: str) -> dict[str, Any] | None:
        for row in store.rows(SAMPLING_MODULE):
            if int(row.get("source_id", 0)) == source_id and row.get("round_key") == key:
                return row
        return None

    def _build_item(self, entry: dict[str, Any], version: dict[str, Any], moment: datetime) -> dict[str, Any]:
        period = int(version.get("采样周期分钟") or DEFAULT_PERIOD_MINUTES)
        deadline = moment + timedelta(minutes=period)
        rows = store.rows(SAMPLING_MODULE)
        return {
            "id": _next_id(rows),
            "status": ITEM_PENDING,
            "pending": True,
            "abnormal": False,
            "source_id": int(entry.get("id", 0)),
            "来源编号": entry.get("源端编号"),
            "round_key": round_key_of(moment),
            "轮次": display_of(moment),
            "计划分钟": moment.strftime("%H:%M"),
            "来源版本": int(version.get("版本号", 1)),
            "接入地址": version.get("接入地址"),
            "采样周期分钟": period,
            "截止时间": display_of(deadline),
            "截止分钟": deadline.strftime("%H:%M"),
            "领用人": "",
            "领用时间": "",
            "确认时间": "",
            "派发编号": "",
            "备注": "",
        }

    def _apply_snapshot(self, item: dict[str, Any], version: dict[str, Any], moment: datetime) -> bool:
        """把同一版本的地址、周期（以及由周期算出的截止时间）成套写入轮次。"""
        period = int(version.get("采样周期分钟") or DEFAULT_PERIOD_MINUTES)
        deadline = moment + timedelta(minutes=period)
        snapshot = {
            "来源版本": int(version.get("版本号", 1)),
            "接入地址": version.get("接入地址"),
            "采样周期分钟": period,
            "截止时间": display_of(deadline),
            "截止分钟": deadline.strftime("%H:%M"),
        }
        changed = any(item.get(field) != value for field, value in snapshot.items())
        if changed:
            item.update(snapshot)
            item["备注"] = (
                f"已按来源版本 v{snapshot['来源版本']} 成套刷新接入地址与采样周期"
                f"（截止 {snapshot['截止分钟']}）"
            )
        return changed

    # ------------------------------------------------------------- 清单查询
    def list_items(
        self,
        *,
        status: str | None = None,
        source_id: int | None = None,
        round_key: str | None = None,
        keyword: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = list(store.rows(SAMPLING_MODULE))
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if source_id is not None:
            rows = [row for row in rows if int(row.get("source_id", 0)) == int(source_id)]
        if round_key:
            rows = [row for row in rows if row.get("round_key") == round_key]
        if keyword:
            rows = [
                row for row in rows
                if keyword in str(row.get("来源编号", "")) or keyword in str(row.get("接入地址", ""))
            ]
        rows.sort(key=lambda row: (str(row.get("round_key", "")), int(row.get("source_id", 0))))
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_item(self, item_id: int) -> dict[str, Any] | None:
        return store.find(SAMPLING_MODULE, item_id)

    def list_dispatches(
        self,
        *,
        status: str | None = None,
        source_id: int | None = None,
        round_key: str | None = None,
        valid: bool | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = list(store.rows(DISPATCH_MODULE))
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if source_id is not None:
            rows = [row for row in rows if int(row.get("source_id", 0)) == int(source_id)]
        if round_key:
            rows = [row for row in rows if row.get("round_key") == round_key]
        if valid is not None:
            rows = [row for row in rows if bool(row.get("valid")) is valid]
        rows.sort(key=lambda row: int(row.get("id", 0)))
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_dispatch(self, dispatch_id: int) -> dict[str, Any] | None:
        return store.find(DISPATCH_MODULE, dispatch_id)

    # ----------------------------------------------------------------- 领用
    def claim(self, item_id: int, operator: str | None = None) -> tuple[dict[str, Any] | None, str]:
        """领用待执行的采样项并生成唯一的派发记录（未确认状态）。"""
        operator = str(operator or "").strip() or "值班管理员"
        with self.lock:
            item = store.find(SAMPLING_MODULE, item_id)
            if item is None:
                return None, f"采样项 {item_id} 不存在或已归档"

            if item.get("status") in (ITEM_CLAIMED, ITEM_DISPATCHED):
                return None, (
                    f"轮次 {item.get('轮次')} 已由 {item.get('领用人') or '其他操作员'} 领用"
                    f"（派发编号 {item.get('派发编号')}），同一来源的同一轮次只能派发一次"
                )
            if item.get("status") == ITEM_VOIDED:
                reason = item.get("备注") or "来源已停用"
                return None, f"轮次 {item.get('轮次')} 已作废（{reason}），不能再派发"
            if item.get("status") != ITEM_PENDING:
                return None, f"轮次 {item.get('轮次')} 当前状态为 {item.get('status')}，不能领用"

            source_id = int(item.get("source_id", 0))
            entry = store.find(DATASOURCE_MODULE, source_id)
            if entry is None or str(entry.get("status") or "") != SOURCE_ENABLED:
                self._void_item(item, "来源已停用，尚未派发的轮次不能派出")
                return None, f"来源 {item.get('来源编号')} 已停用，本轮采样不再派发"

            duplicate = self._active_dispatch(source_id, str(item.get("round_key") or ""))
            if duplicate is not None:
                self._void_item(item, f"本轮已存在派发记录 {duplicate.get('派发编号')}")
                return None, (
                    f"轮次 {item.get('轮次')} 已派发（{duplicate.get('派发编号')}），"
                    "同一来源的同一轮次只能派发一次"
                )

            # 领用前再和最新来源版本对齐一次：地址、周期、截止分钟必须成套。
            version = self._record_version_locked(entry)
            moment = parse_round_at(item.get("round_key"))
            self._apply_snapshot(item, version, moment)

            dispatch_rows = store.rows(DISPATCH_MODULE)
            dispatch_id = _next_id(dispatch_rows)
            now = _now_text()
            dispatch = {
                "id": dispatch_id,
                "派发编号": f"DISP-{dispatch_id:04d}",
                "采样清单编号": item.get("id"),
                "status": DISPATCH_UNCONFIRMED,
                "pending": True,
                "abnormal": False,
                "valid": True,
                "source_id": source_id,
                "来源编号": item.get("来源编号"),
                "round_key": item.get("round_key"),
                "轮次": item.get("轮次"),
                "来源版本": item.get("来源版本"),
                "接入地址": item.get("接入地址"),
                "采样周期分钟": item.get("采样周期分钟"),
                "截止时间": item.get("截止时间"),
                "截止分钟": item.get("截止分钟"),
                "操作人": operator,
                "领用时间": now,
                "确认人": "",
                "确认时间": "",
                "作废原因": "",
            }
            dispatch_rows.append(dispatch)

            item["status"] = ITEM_CLAIMED
            item["pending"] = True
            item["领用人"] = operator
            item["领用时间"] = now
            item["派发编号"] = dispatch["派发编号"]
            item["备注"] = f"已领用，等待确认派发（{dispatch['派发编号']}）"
            return dispatch, (
                f"已领用轮次 {item.get('轮次')}：接入地址 {item.get('接入地址')}，"
                f"截止 {item.get('截止分钟')}（来源版本 v{item.get('来源版本')}）"
            )

    # ----------------------------------------------------------------- 确认
    def confirm(self, dispatch_id: int, operator: str | None = None) -> tuple[dict[str, Any] | None, str]:
        """确认派发；来源已停用时，未确认的记录一律不能转为有效派发。"""
        operator = str(operator or "").strip() or "值班管理员"
        with self.lock:
            dispatch = store.find(DISPATCH_MODULE, dispatch_id)
            if dispatch is None:
                return None, f"派发记录 {dispatch_id} 不存在"
            if dispatch.get("status") == DISPATCH_CONFIRMED:
                return None, f"派发记录 {dispatch.get('派发编号')} 已确认，不能重复确认"
            if dispatch.get("status") == DISPATCH_VOIDED:
                reason = dispatch.get("作废原因") or "记录已作废"
                return None, f"派发记录 {dispatch.get('派发编号')} 已作废（{reason}），不能再派发"

            source_id = int(dispatch.get("source_id", 0))
            entry = store.find(DATASOURCE_MODULE, source_id)
            if entry is None or str(entry.get("status") or "") != SOURCE_ENABLED:
                self._void_dispatch(dispatch, "来源已停用，尚未确认的记录不能再派发")
                return None, f"来源 {dispatch.get('来源编号')} 已停用，未确认的派发记录已作废"

            now = _now_text()
            dispatch["status"] = DISPATCH_CONFIRMED
            dispatch["pending"] = False
            dispatch["valid"] = True
            dispatch["abnormal"] = False
            dispatch["确认人"] = operator
            dispatch["确认时间"] = now

            item = store.find(SAMPLING_MODULE, int(dispatch.get("采样清单编号") or 0))
            if item is not None:
                item["status"] = ITEM_DISPATCHED
                item["pending"] = False
                item["确认时间"] = now
                item["备注"] = f"已确认派发（{dispatch['派发编号']}）"
            return dispatch, (
                f"派发已确认：{dispatch.get('派发编号')}，接入地址 {dispatch.get('接入地址')}，"
                f"截止 {dispatch.get('截止分钟')}（来源版本 v{dispatch.get('来源版本')}）"
            )

    # --------------------------------------------------------- 来源配置变更
    def on_source_updated(self, entry: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """地址/周期变更后登记新版本，并把尚未领用的轮次成套刷新到新版本。"""
        with self.lock:
            version = self._record_version_locked(entry)
            if str(entry.get("status") or "") != SOURCE_ENABLED:
                return version, []
            refreshed: list[dict[str, Any]] = []
            for item in store.rows(SAMPLING_MODULE):
                if int(item.get("source_id", 0)) != int(entry.get("id", 0)):
                    continue
                if item.get("status") != ITEM_PENDING:
                    continue
                moment = parse_round_at(item.get("round_key"))
                if self._apply_snapshot(item, version, moment):
                    refreshed.append(item)
            return version, refreshed

    def on_source_disabled(self, source_id: int) -> dict[str, int]:
        """停用来源：未派发的清单项与尚未确认的派发记录全部作废。"""
        with self.lock:
            voided_items = 0
            voided_dispatches = 0
            reason = "来源已停用，尚未确认的记录不能再派发"
            for item in store.rows(SAMPLING_MODULE):
                if int(item.get("source_id", 0)) != int(source_id):
                    continue
                if item.get("status") in (ITEM_PENDING, ITEM_CLAIMED):
                    self._void_item(item, reason, abnormal=True)
                    voided_items += 1
            for dispatch in store.rows(DISPATCH_MODULE):
                if int(dispatch.get("source_id", 0)) != int(source_id):
                    continue
                if dispatch.get("status") == DISPATCH_UNCONFIRMED:
                    self._void_dispatch(dispatch, reason)
                    voided_dispatches += 1
            return {"voided_items": voided_items, "voided_dispatches": voided_dispatches}

    # ----------------------------------------------------------------- 内部
    def _active_dispatch(self, source_id: int, key: str) -> dict[str, Any] | None:
        for row in store.rows(DISPATCH_MODULE):
            if (
                int(row.get("source_id", 0)) == source_id
                and row.get("round_key") == key
                and row.get("status") in ACTIVE_DISPATCH_STATUSES
            ):
                return row
        return None

    def _void_item(self, item: dict[str, Any], reason: str, *, abnormal: bool = True) -> None:
        item["status"] = ITEM_VOIDED
        item["pending"] = False
        item["abnormal"] = abnormal
        item["备注"] = reason

    def _void_dispatch(self, dispatch: dict[str, Any], reason: str) -> None:
        dispatch["status"] = DISPATCH_VOIDED
        dispatch["pending"] = False
        dispatch["valid"] = False
        dispatch["abnormal"] = True
        dispatch["作废原因"] = reason
        item = store.find(SAMPLING_MODULE, int(dispatch.get("采样清单编号") or 0))
        if item is not None:
            self._void_item(item, reason)


sampling_service = SamplingService()
