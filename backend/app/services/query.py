"""交互查询业务规则：以查询执行轨迹为中心的状态机、竞争裁决与缓存准入。

状态只能按 草稿 → 待执行 → 执行中 → 已完成/已超时 的顺序流转，不允许跨级
或回退；草稿不能取得结果；已完成与已超时一旦裁定便互斥且不可回退。每个
动作（无论是否被接受）都会写入执行轨迹并保留理由；保存结果被接受后会把
该轮次的结果身份准入查询缓存，轨迹事件与缓存条目互相引用，从查询轨迹可
以追到缓存准入证据。

竞争裁决：同一轮次的完成与终止争用时，由类级锁串行裁定，执行轨迹中首个
被接受的终裁动作胜出；后到者按过期/已裁定拒绝，拒绝理由指明胜出者。
"""
from __future__ import annotations

from datetime import datetime, timezone
from threading import Lock
from typing import Any

from app.services.cache import CacheService
from app.store import store

MODULE = "query"
REQUIRED_FIELDS = ["语句编号", "查询表达式", "目标数据集"]
OPTIONAL_FIELDS = ["返回字段", "排序条件", "分页大小", "最大行数", "超时秒数"]
STATUS_ORDER = ["草稿", "待执行", "执行中", "已完成", "已超时"]
STATUS_FLOW_TEXT = "草稿 → 待执行 → 执行中 → 已完成/已超时"
TERMINAL_STATUSES = ("已完成", "已超时")

# 动作 → (允许的源状态, 目标状态)：只允许相邻状态流转，草稿不能取得结果。
ACTION_TRANSITIONS: dict[str, tuple[tuple[str, ...], str]] = {
    "提交执行": (("草稿",), "待执行"),
    "启动查询": (("待执行",), "执行中"),
    "保存结果": (("执行中",), "已完成"),
    "终止查询": (("执行中",), "已超时"),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class QueryService:
    """交互查询状态机；类级锁保证同一轮次的完成与终止串行裁定。"""

    _lock = Lock()

    def __init__(self) -> None:
        self._cache = CacheService()
        # 启动时对存量记录做一次兼容解释，保证列表、详情与概览口径一致。
        for row in store.rows(MODULE):
            self._normalize(row)

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for row in rows:
            self._normalize(row)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("语句编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is not None:
            self._normalize(entry)
        return entry

    # ------------------------------------------------------------------
    # 登记
    # ------------------------------------------------------------------
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry: dict[str, Any] = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        for field in REQUIRED_FIELDS + OPTIONAL_FIELDS:
            value = values.get(field)
            if value is not None and str(value).strip() != "":
                entry[field] = value
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["round"] = 0
        entry["结果身份"] = None
        entry["缓存策略"] = None
        entry["trajectory"] = []
        rows.append(entry)
        self._record(
            entry,
            action="登记查询",
            accepted=True,
            note="查询语句已登记，初始状态「草稿」",
            from_status=None,
            to_status="草稿",
        )
        return entry, []

    # ------------------------------------------------------------------
    # 动作裁定
    # ------------------------------------------------------------------
    def run_action(
        self,
        entry_id: int,
        action: str,
        round_no: int | None = None,
    ) -> tuple[dict[str, Any] | None, str, bool]:
        """裁定一个动作是否被接受；返回 (记录, 说明, 是否被接受)。

        记录为 None 仅表示查询不存在；其余拒绝都会把理由写入执行轨迹并随
        记录返回，未获接受的动作不能改变状态、轮次与查询缓存。
        """
        with self._lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"查询语句 {entry_id} 不存在或已归档", False
            self._normalize(entry)

            if action not in ACTION_TRANSITIONS:
                note = f"动作「{action or '空'}」不属于交互查询可执行范围（可执行：{'、'.join(ACTION_TRANSITIONS)}）"
                self._record(entry, action=action or "空", accepted=False, note=note,
                             from_status=entry["status"], to_status=None)
                return entry, note, False

            allowed_from, target = ACTION_TRANSITIONS[action]
            current = entry["status"]
            current_round = entry["round"]

            # 过期动作：动作针对的轮次与查询当前轮次不一致。
            if round_no is not None and round_no != current_round:
                note = (f"动作针对第 {round_no} 轮，但查询已推进到第 {current_round} 轮，"
                        f"属于过期动作，不予执行")
                self._record(entry, action=action, accepted=False, note=note,
                             from_status=current, to_status=None)
                return entry, note, False

            # 终态互斥且不可回退：胜出依据为执行轨迹中首个被接受的终裁动作。
            if current in TERMINAL_STATUSES:
                final_event = self._final_event(entry)
                winner = (f"「{final_event['action']}」（轨迹 #{final_event['seq']}）"
                          if final_event else "先前裁定")
                note = f"第 {current_round} 轮已由{winner}裁定为「{current}」，完成与超时互斥且不可回退"
                if action == "保存结果" and current == "已完成" and entry.get("结果身份"):
                    note += f"；重复保存只认可首次结果 {entry['结果身份']}，查询缓存不予变更"
                self._record(entry, action=action, accepted=False, note=note,
                             from_status=current, to_status=None)
                return entry, note, False

            # 状态只能按顺序流转：草稿不能取得结果，未启动不能终止。
            if current not in allowed_from:
                note = (f"当前状态为「{current}」，「{action}」仅允许在"
                        f"「{'、'.join(allowed_from)}」状态执行；查询状态只能按 {STATUS_FLOW_TEXT} 顺序流转")
                self._record(entry, action=action, accepted=False, note=note,
                             from_status=current, to_status=None)
                return entry, note, False

            if action == "启动查询":
                entry["round"] = current_round + 1
            entry["status"] = target
            entry["pending"] = target not in TERMINAL_STATUSES
            entry["abnormal"] = target == "已超时"

            extra: dict[str, Any] | None = None
            if action == "保存结果":
                result_id = entry.get("结果身份") or f"RES-{entry['语句编号']}-R{entry['round']}"
                entry["结果身份"] = result_id
                cache_entry = self._cache.admit_query_result(
                    query_id=int(entry["id"]),
                    query_code=str(entry["语句编号"]),
                    round_no=int(entry["round"]),
                    result_id=result_id,
                    trajectory_seq=len(entry["trajectory"]) + 1,
                    timeout=entry.get("超时秒数"),
                )
                entry["缓存策略"] = cache_entry["策略编号"]
                extra = {"cache": {"策略编号": cache_entry["策略编号"],
                                   "缓存条目": cache_entry["id"],
                                   "结果身份": cache_entry["结果身份"]}}
                note = (f"查询语句已保存结果，第 {entry['round']} 轮裁定为「已完成」，"
                        f"结果 {result_id} 已准入查询缓存 {cache_entry['策略编号']}")
            elif action == "启动查询":
                note = f"查询语句已启动查询，第 {entry['round']} 轮执行开始"
            elif action == "终止查询":
                note = f"查询语句已终止查询，第 {entry['round']} 轮裁定为「已超时」，超时轮次不生成查询缓存"
            else:
                note = f"查询语句已提交执行，状态流转为「{target}」"

            self._record(entry, action=action, accepted=True, note=note,
                         from_status=current, to_status=target, extra=extra)
            return entry, note, True

    # ------------------------------------------------------------------
    # 兼容解释与执行轨迹
    # ------------------------------------------------------------------
    def _normalize(self, entry: dict[str, Any]) -> None:
        """把旧记录解释成当前口径：状态不在序列里的按草稿解释，pending/abnormal
        依状态重算，缺失的轮次与执行轨迹按状态补齐，解释过程写入轨迹。"""
        notes: list[str] = []
        status = str(entry.get("status") or "")
        if status not in STATUS_ORDER:
            notes.append(f"原状态「{status or '空'}」不在状态序列里，按「草稿」解释")
            status = "草稿"
            entry["status"] = status
        pending = status not in TERMINAL_STATUSES
        if entry.get("pending") != pending:
            notes.append(f"pending 依状态「{status}」重算为「{'真' if pending else '假'}」")
        entry["pending"] = pending
        abnormal = status == "已超时"
        if entry.get("abnormal") != abnormal:
            notes.append(f"abnormal 依状态「{status}」重算为「{'真' if abnormal else '假'}」")
        entry["abnormal"] = abnormal
        expect_round = 1 if status in ("执行中", *TERMINAL_STATUSES) else 0
        round_no = entry.get("round")
        if not isinstance(round_no, int) or isinstance(round_no, bool) or round_no < expect_round:
            entry["round"] = expect_round
            if expect_round:
                notes.append(f"执行轮次缺失，按状态「{status}」补齐为第 {expect_round} 轮")
        trajectory = entry.get("trajectory")
        if not isinstance(trajectory, list):
            trajectory = []
            entry["trajectory"] = trajectory
        if not trajectory:
            notes.insert(0, "旧记录缺少执行轨迹")
        if notes:
            self._record(entry, action="兼容解释", accepted=True, note="；".join(notes),
                         from_status=None, to_status=status)

    def _record(
        self,
        entry: dict[str, Any],
        *,
        action: str,
        accepted: bool,
        note: str,
        from_status: str | None,
        to_status: str | None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        trajectory = entry.setdefault("trajectory", [])
        event: dict[str, Any] = {
            "seq": len(trajectory) + 1,
            "at": _now(),
            "action": action,
            "round": entry.get("round", 0),
            "from": from_status,
            "to": to_status,
            "accepted": accepted,
            "reason": note,
        }
        if extra:
            event.update(extra)
        trajectory.append(event)
        return event

    @staticmethod
    def _final_event(entry: dict[str, Any]) -> dict[str, Any] | None:
        """当前轮次首个被接受的终裁事件：完成与终止争用时的胜出依据。"""
        current_round = entry.get("round", 0)
        for event in reversed(entry.get("trajectory", [])):
            if (event.get("accepted")
                    and event.get("to") in TERMINAL_STATUSES
                    and event.get("round") == current_round):
                return event
        return None
