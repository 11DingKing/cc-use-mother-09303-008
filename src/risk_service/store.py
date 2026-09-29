"""SQLite 持久化。

只依赖标准库 :mod:`sqlite3`。写入模型是追加事件日志（``event_log``），
每条事件包含前一条事件的哈希，形成可整体校验的决定链；
读模型（``risks`` / ``restrictions`` / ``exceptions`` 等）在同一事务内投影更新。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from . import events as ev

SCHEMA = """
CREATE TABLE IF NOT EXISTS ruleset_versions (
    version INTEGER PRIMARY KEY,
    published_at TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    published_by TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    spec TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT UNIQUE NOT NULL,
    event_type TEXT NOT NULL,
    aggregate_id TEXT,
    occurred_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    data TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS risks (
    risk_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    title TEXT NOT NULL,
    source TEXT NOT NULL,
    target TEXT NOT NULL,
    owner TEXT NOT NULL,
    mitigation TEXT NOT NULL DEFAULT '',
    next_review_at TEXT,
    current_level TEXT,
    current_rule_version INTEGER,
    latest_rating_seq INTEGER,
    merged_into TEXT,
    closed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_facts (
    risk_id TEXT PRIMARY KEY,
    facts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS restrictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    risk_id TEXT NOT NULL,
    action TEXT NOT NULL,
    flow TEXT NOT NULL,
    target_ref TEXT NOT NULL DEFAULT '*',
    origin TEXT NOT NULL,
    reason TEXT NOT NULL,
    added_seq INTEGER NOT NULL,
    added_at TEXT NOT NULL,
    released_seq INTEGER,
    released_at TEXT,
    release_reason TEXT,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS exceptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    risk_id TEXT NOT NULL,
    action TEXT NOT NULL,
    approver TEXT NOT NULL,
    reason TEXT NOT NULL,
    granted_seq INTEGER NOT NULL,
    granted_at TEXT NOT NULL,
    valid_until TEXT,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    closed_seq INTEGER,
    closed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_event_aggregate ON event_log(aggregate_id);
CREATE INDEX IF NOT EXISTS idx_restrictions_lookup ON restrictions(flow, active);
CREATE INDEX IF NOT EXISTS idx_exceptions_lookup ON exceptions(risk_id, action, status);
CREATE INDEX IF NOT EXISTS idx_risks_review ON risks(status, next_review_at);
"""


def iso(moment: datetime) -> str:
    return moment.isoformat()


def parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def canonical(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class Store:
    """封装数据库连接与事件追加。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        # HTTP 服务为多线程，所有线程共用同一连接（内存库尤其需要），
        # 写事务由 self._lock 串行化。
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self._lock = threading.RLock()
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        with self._lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self.conn
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    # ------------------------------------------------------------------ 规则

    def insert_ruleset(self, ruleset: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO ruleset_versions(version, published_at, effective_from, "
            "published_by, note, spec) VALUES (?,?,?,?,?,?)",
            (
                ruleset["version"],
                ruleset["published_at"],
                ruleset["effective_from"],
                ruleset["published_by"],
                ruleset["note"],
                json.dumps(ruleset["spec"], ensure_ascii=False),
            ),
        )

    def get_ruleset(self, version: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM ruleset_versions WHERE version = ?", (version,)
        ).fetchone()
        return self._ruleset_row(row) if row else None

    def latest_ruleset(self, at: datetime | None = None) -> dict[str, Any] | None:
        """取已生效的最新版本；``at`` 为空时取绝对最新版本。"""
        if at is None:
            row = self.conn.execute(
                "SELECT * FROM ruleset_versions ORDER BY version DESC LIMIT 1"
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM ruleset_versions WHERE effective_from <= ? "
                "ORDER BY version DESC LIMIT 1",
                (iso(at),),
            ).fetchone()
        return self._ruleset_row(row) if row else None

    @staticmethod
    def _ruleset_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["spec"] = json.loads(data["spec"])
        data["published_at"] = parse(data["published_at"])
        data["effective_from"] = parse(data["effective_from"])
        return data

    # ------------------------------------------------------------------ 事件

    def append_event(
        self,
        event_type: str,
        aggregate_id: str | None,
        data: dict[str, Any],
        actor: str,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        """追加一条事件并计算哈希链。必须在事务中调用。"""
        last = self.conn.execute(
            "SELECT seq, hash FROM event_log ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        seq = (last["seq"] + 1) if last else 1
        prev_hash = last["hash"] if last else "GENESIS"
        event_id = uuid.uuid4().hex
        payload = canonical(
            {
                "seq": seq,
                "event_id": event_id,
                "event_type": event_type,
                "aggregate_id": aggregate_id,
                "occurred_at": iso(occurred_at),
                "actor": actor,
                "data": data,
                "prev_hash": prev_hash,
            }
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        self.conn.execute(
            "INSERT INTO event_log(event_id, event_type, aggregate_id, occurred_at, "
            "actor, data, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?)",
            (
                event_id,
                event_type,
                aggregate_id,
                iso(occurred_at),
                actor,
                json.dumps(data, ensure_ascii=False),
                prev_hash,
                digest,
            ),
        )
        return {
            "seq": seq,
            "event_id": event_id,
            "event_type": event_type,
            "aggregate_id": aggregate_id,
            "occurred_at": iso(occurred_at),
            "actor": actor,
            "data": data,
            "prev_hash": prev_hash,
            "hash": digest,
        }

    def list_events(self, aggregate_id: str | None = None) -> list[dict[str, Any]]:
        if aggregate_id is None:
            rows = self.conn.execute("SELECT * FROM event_log ORDER BY seq").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM event_log WHERE aggregate_id = ? ORDER BY seq",
                (aggregate_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["data"] = json.loads(item["data"])
            result.append(item)
        return result

    def verify_chain(self) -> bool:
        """重放全部事件校验哈希链，任一被篡改即返回 False。"""
        prev_hash = "GENESIS"
        for row in self.conn.execute("SELECT * FROM event_log ORDER BY seq"):
            payload = canonical(
                {
                    "seq": row["seq"],
                    "event_id": row["event_id"],
                    "event_type": row["event_type"],
                    "aggregate_id": row["aggregate_id"],
                    "occurred_at": row["occurred_at"],
                    "actor": row["actor"],
                    "data": json.loads(row["data"]),
                    "prev_hash": prev_hash,
                }
            )
            if hashlib.sha256(payload.encode("utf-8")).hexdigest() != row["hash"]:
                return False
            if row["prev_hash"] != prev_hash:
                return False
            prev_hash = row["hash"]
        return True

    # ------------------------------------------------------------------ 风险

    def insert_risk(self, risk: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO risks(risk_id, status, title, source, target, owner, "
            "mitigation, next_review_at, current_level, current_rule_version, "
            "latest_rating_seq, merged_into, closed_at, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                risk["risk_id"],
                risk["status"],
                risk["title"],
                json.dumps(risk["source"], ensure_ascii=False),
                json.dumps(risk["target"], ensure_ascii=False),
                json.dumps(risk["owner"], ensure_ascii=False),
                risk.get("mitigation", ""),
                risk.get("next_review_at"),
                risk.get("current_level"),
                risk.get("current_rule_version"),
                risk.get("latest_rating_seq"),
                risk.get("merged_into"),
                risk.get("closed_at"),
                risk["created_at"],
                risk["updated_at"],
            ),
        )

    def get_risk(self, risk_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM risks WHERE risk_id = ?", (risk_id,)).fetchone()
        return self._risk_row(row) if row else None

    def list_risks(self, include_closed: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM risks"
        if not include_closed:
            sql += " WHERE status != 'CLOSED'"
        sql += " ORDER BY created_at, risk_id"
        return [self._risk_row(r) for r in self.conn.execute(sql)]

    @staticmethod
    def _risk_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        for key in ("source", "target", "owner"):
            data[key] = json.loads(data[key])
        data["active"] = data["status"] == ev.STATUS_OPEN
        return data

    def update_risk(self, risk_id: str, **fields: Any) -> None:
        if not fields:
            return
        clauses = ", ".join(f"{key} = ?" for key in fields)
        values = list(fields.values()) + [risk_id]
        self.conn.execute(f"UPDATE risks SET {clauses} WHERE risk_id = ?", values)

    def set_facts(self, risk_id: str, facts: dict[str, Any]) -> None:
        body = json.dumps(facts, ensure_ascii=False)
        self.conn.execute(
            "INSERT INTO risk_facts(risk_id, facts) VALUES (?,?) "
            "ON CONFLICT(risk_id) DO UPDATE SET facts = excluded.facts",
            (risk_id, body),
        )

    def get_facts(self, risk_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT facts FROM risk_facts WHERE risk_id = ?", (risk_id,)
        ).fetchone()
        return json.loads(row["facts"]) if row else {}

    def due_risks(self, now: datetime) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM risks WHERE status = 'OPEN' AND next_review_at IS NOT NULL "
            "AND next_review_at <= ? ORDER BY next_review_at, risk_id",
            (iso(now),),
        ).fetchall()
        return [self._risk_row(r) for r in rows]

    # -------------------------------------------------------------- 限制/例外

    def add_restriction(self, item: dict[str, Any]) -> int:
        cur = self.conn.execute(
            "INSERT INTO restrictions(risk_id, action, flow, target_ref, origin, "
            "reason, added_seq, added_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                item["risk_id"],
                item["action"],
                item["flow"],
                item.get("target_ref", ev.TARGET_ALL),
                item["origin"],
                item["reason"],
                item["added_seq"],
                item["added_at"],
            ),
        )
        return int(cur.lastrowid)

    def find_active_restriction(self, risk_id: str, action: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM restrictions WHERE risk_id = ? AND action = ? AND active = 1",
            (risk_id, action),
        ).fetchone()
        return dict(row) if row else None

    def list_restrictions(self, risk_id: str, active_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM restrictions WHERE risk_id = ?"
        if active_only:
            sql += " AND active = 1"
        sql += " ORDER BY id"
        return [dict(r) for r in self.conn.execute(sql, (risk_id,))]

    def all_active_restrictions(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT restrictions.* FROM restrictions JOIN risks ON risks.risk_id = "
            "restrictions.risk_id WHERE restrictions.active = 1 AND risks.status = 'OPEN'"
        ).fetchall()
        return [dict(r) for r in rows]

    def release_restriction(self, restriction_id: int, seq: int, at: datetime, reason: str) -> None:
        self.conn.execute(
            "UPDATE restrictions SET active = 0, released_seq = ?, released_at = ?, "
            "release_reason = ? WHERE id = ?",
            (seq, iso(at), reason, restriction_id),
        )

    def grant_exception(self, item: dict[str, Any]) -> int:
        cur = self.conn.execute(
            "INSERT INTO exceptions(risk_id, action, approver, reason, granted_seq, "
            "granted_at, valid_until, status) VALUES (?,?,?,?,?,?,?,?)",
            (
                item["risk_id"],
                item["action"],
                item["approver"],
                item["reason"],
                item["granted_seq"],
                item["granted_at"],
                item.get("valid_until"),
                ev.EXC_ACTIVE,
            ),
        )
        return int(cur.lastrowid)

    def get_exception(self, exception_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM exceptions WHERE id = ?", (exception_id,)).fetchone()
        return dict(row) if row else None

    def active_exception(self, risk_id: str, action: str, now: datetime) -> dict[str, Any] | None:
        """当前仍有效（未撤销、未到期）的例外。到期失效事件由服务层补发。"""
        row = self.conn.execute(
            "SELECT * FROM exceptions WHERE risk_id = ? AND action = ? AND status = ? "
            "AND (valid_until IS NULL OR valid_until > ?) ORDER BY id DESC LIMIT 1",
            (risk_id, action, ev.EXC_ACTIVE, iso(now)),
        ).fetchone()
        return dict(row) if row else None

    def expirable_exceptions(self, now: datetime) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM exceptions WHERE status = ? AND valid_until IS NOT NULL "
            "AND valid_until <= ? ORDER BY id",
            (ev.EXC_ACTIVE, iso(now)),
        ).fetchall()
        return [dict(r) for r in rows]

    def close_exception(self, exception_id: int, status: str, seq: int, at: datetime) -> None:
        self.conn.execute(
            "UPDATE exceptions SET status = ?, closed_seq = ?, closed_at = ? WHERE id = ?",
            (status, seq, iso(at), exception_id),
        )
