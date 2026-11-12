"""查询缓存业务规则：状态流转、字段校验与筛选口径都收在这里。

跨模块准入：交互查询已完成轮次的结果身份通过 admit_query_result 进入查询缓存，
只接纳已完成轮次；超时轮次与过期动作根本不会走到这里。重复准入只认可首次
那份结果身份，不覆盖、不新增。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.store import store

MODULE = "cache"
REQUIRED_FIELDS = ["策略编号", "目标查询", "缓存键规则"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用缓存": "已启用", "停用缓存": "已停用"}
NEGATIVE_ACTIONS = ["停用缓存"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class CacheService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("策略编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
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
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"缓存策略 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于查询缓存可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"缓存策略已{action}"

    def admit_query_result(
        self,
        *,
        query_id: int,
        query_code: str,
        round_no: int,
        result_id: str,
        trajectory_seq: int,
        timeout: Any = None,
    ) -> dict[str, Any]:
        """把交互查询已完成轮次的结果身份准入查询缓存。

        准入证据（来源语句、来源轮次、结果身份、准入轨迹序号）随条目保存，
        从缓存条目可以反查交互查询的执行轨迹。重复准入只认可首次那份结果
        身份：已存在同一语句的准入条目时直接返回原条目，不覆盖、不新增。
        """
        rows = store.rows(MODULE)
        for row in rows:
            if row.get("来源模块") == "交互查询" and row.get("来源语句") == query_code:
                return row
        entry: dict[str, Any] = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "策略编号": f"QC-{query_code}",
            "目标查询": query_code,
            "缓存键规则": f"query:{query_id}:r{round_no}",
            "过期秒数": str(timeout or "300"),
            "刷新触发": "结果保存",
            "命中率阈值": "0",
            "最大条目数": "1",
            "淘汰算法": "LRU",
            "status": "已启用",
            "pending": False,
            "abnormal": False,
            "来源模块": "交互查询",
            "来源语句": query_code,
            "来源轮次": round_no,
            "结果身份": result_id,
            "准入轨迹": trajectory_seq,
            "准入时间": _now(),
        }
        rows.append(entry)
        return entry
