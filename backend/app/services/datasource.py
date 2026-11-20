"""数据源配置业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.services.sampling import sampling_service
from app.store import store

MODULE = "datasource"
REQUIRED_FIELDS = ["源端编号", "连接类型", "接入地址"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用配置": "已启用", "停用配置": "已停用"}
NEGATIVE_ACTIONS = ["停用配置"]
# 只有这两个字段会参与采集轮次的地址/周期快照，改动就要登记新的来源版本。
VERSIONED_FIELDS = ["接入地址", "采样周期"]


class DatasourceService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
        extra: dict[str, str] | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("源端编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if extra:
            for field, val in extra.items():
                rows = [row for row in rows if val in str(row.get(field, ""))]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        for field, value in values.items():
            if field not in entry and field in ("认证方式", "采样周期", "超时秒数", "重试上限", "负责人"):
                entry[field] = value
        entry["采样周期"] = entry.get("采样周期") or "10分钟"
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        sampling_service.ensure_version(entry)
        return entry, []

    def update_entry(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """修改数据源的接入地址或采样周期：登记新版本并刷新未领用的轮次。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"数据源 {entry_id} 不存在或已归档"
        versioned_changed = False
        for field, value in values.items():
            if field in ("id", "status", "pending", "abnormal", "来源版本", "action"):
                continue
            if field in VERSIONED_FIELDS and str(entry.get(field) or "") != str(value or ""):
                versioned_changed = True
            entry[field] = value
        if versioned_changed:
            version, refreshed = sampling_service.on_source_updated(entry)
            entry["来源版本"] = version.get("版本号")
            return entry, (
                f"数据源已更新并登记来源版本 v{version.get('版本号')}，"
                f"未领用的轮次已成套刷新（{len(refreshed)} 条）"
            )
        return entry, "数据源已更新"

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"数据源 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据源配置可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        if action == "启用配置":
            sampling_service.ensure_version(entry)
        if action == "停用配置":
            result = sampling_service.on_source_disabled(entry_id)
            return entry, (
                f"数据源已{action}，作废未派发轮次 {result['voided_items']} 条、"
                f"未确认派发记录 {result['voided_dispatches']} 条"
            )
        return entry, f"数据源已{action}"
