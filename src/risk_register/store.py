"""仓储层。

默认实现为进程内仓储，可整体序列化为 JSON 快照落盘并恢复，便于演示与
单测。所有 ID 由仓储按前缀发号，决定序号全局单调递增。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from .models import Decision, Flow, Restriction, Risk


class Repository:
    def __init__(self) -> None:
        self._risks: dict[str, Risk] = {}
        self._restrictions: dict[str, Restriction] = {}
        self._flows: dict[str, Flow] = {}
        self._seq = 0
        self._counters: dict[str, int] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------- 发号 ----
    def next_id(self, prefix: str) -> str:
        with self._lock:
            n = self._counters.get(prefix, 0) + 1
            self._counters[prefix] = n
            return f"{prefix}-{n:04d}"

    def next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    # ----------------------------------------------------- 风险 ----
    def save_risk(self, risk: Risk) -> None:
        self._risks[risk.id] = risk

    def get_risk(self, risk_id: str) -> Risk:
        try:
            return self._risks[risk_id]
        except KeyError:
            raise KeyError(f"风险不存在：{risk_id}") from None

    def list_risks(self, *, include_merged: bool = True) -> list[Risk]:
        rows = list(self._risks.values())
        if not include_merged:
            rows = [r for r in rows if r.merged_into is None]
        return sorted(rows, key=lambda r: r.created_at + r.id)

    # --------------------------------------------------- 限制 ----
    def save_restriction(self, r: Restriction) -> None:
        self._restrictions[r.id] = r

    def get_restriction(self, rid: str) -> Restriction:
        try:
            return self._restrictions[rid]
        except KeyError:
            raise KeyError(f"限制不存在：{rid}") from None

    def list_restrictions(self, *, status: str | None = None) -> list[Restriction]:
        rows = list(self._restrictions.values())
        if status:
            rows = [r for r in rows if r.status == status]
        return sorted(rows, key=lambda r: (r.imposed_at, r.id))

    # ----------------------------------------------------- 流程 ----
    def upsert_flow(self, flow: Flow) -> Flow:
        self._flows[flow.key] = flow
        return flow

    def get_flow(self, key: str) -> Flow:
        try:
            return self._flows[key]
        except KeyError:
            raise KeyError(f"流程不存在：{key}") from None

    def list_flows(self, flow_type: str | None = None) -> list[Flow]:
        rows = list(self._flows.values())
        if flow_type:
            rows = [f for f in rows if f.flow_type == flow_type]
        return sorted(rows, key=lambda f: f.key)

    # --------------------------------------------------- 快照 ----
    def snapshot(self) -> dict[str, Any]:
        return {
            "seq": self._seq,
            "counters": dict(self._counters),
            "risks": [r.to_dict() for r in self._risks.values()],
            "restrictions": [r.to_dict() for r in self._restrictions.values()],
            "flows": [f.to_dict() for f in self._flows.values()],
        }

    def restore(self, data: dict[str, Any]) -> None:
        with self._lock:
            self._risks = {d["id"]: Risk.from_dict(d) for d in data.get("risks", [])}
            self._restrictions = {
                d["id"]: Restriction.from_dict(d) for d in data.get("restrictions", [])
            }
            self._flows = {d["key"]: Flow.from_dict(d) for d in data.get("flows", [])}
            self._seq = int(data.get("seq", 0))
            self._counters = dict(data.get("counters", {}))

    def save_to(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(self.snapshot(), ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def load_from(self, path: str | Path) -> None:
        self.restore(json.loads(Path(path).read_text(encoding="utf-8")))
