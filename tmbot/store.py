"""SQLite persistence.

Three things must survive a restart or a crash mid-request:

* the day's plans, so an adopted position gets the levels it was analysed with;
* per-trade ladder state (``tp1_done`` and friends), so a partial never fires twice;
* an action journal keyed by an idempotency key, so a request that succeeded at
  the broker but died on the way back to us is not replayed blindly.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .models import Fill, ManagedTrade, TradePlan, TradeStatus, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    plan_id    TEXT PRIMARY KEY,
    epic       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS plans_epic_created ON plans(epic, created_at DESC);

CREATE TABLE IF NOT EXISTS trades (
    deal_id    TEXT PRIMARY KEY,
    epic       TEXT NOT NULL,
    status     TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    payload    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS trades_status ON trades(status);

CREATE TABLE IF NOT EXISTS actions (
    key             TEXT PRIMARY KEY,
    deal_id         TEXT NOT NULL,
    kind            TEXT NOT NULL,
    status          TEXT NOT NULL,
    deal_reference  TEXT,
    detail          TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS actions_deal ON actions(deal_id, status);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    deal_id    TEXT,
    epic       TEXT,
    kind       TEXT NOT NULL,
    message    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fills (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id    TEXT NOT NULL,
    epic       TEXT NOT NULL,
    ts         TEXT NOT NULL,
    stage      TEXT NOT NULL,
    payload    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS fills_ts ON fills(ts);
CREATE INDEX IF NOT EXISTS fills_deal ON fills(deal_id);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

ACTION_PENDING = "PENDING"
ACTION_DONE = "DONE"
ACTION_FAILED = "FAILED"


class Store:
    def __init__(self, path: str = "tmbot.sqlite3"):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # The Telegram command thread reads while the supervisor writes.
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._connection.executescript(SCHEMA)
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cursor = self._connection.execute(sql, tuple(params))
            self._connection.commit()
            return cursor

    def _query(self, sql: str, params: Iterable[Any] = ()) -> List[sqlite3.Row]:
        with self._lock:
            return self._connection.execute(sql, tuple(params)).fetchall()

    # ------------------------------------------------------------------ plans

    def save_plan(self, plan: TradePlan) -> None:
        self._execute(
            "INSERT OR REPLACE INTO plans (plan_id, epic, created_at, payload) VALUES (?,?,?,?)",
            (plan.plan_id, plan.epic, plan.created_at.isoformat(), json.dumps(plan.to_dict())),
        )

    def plan(self, plan_id: str) -> Optional[TradePlan]:
        rows = self._query("SELECT payload FROM plans WHERE plan_id = ?", (plan_id,))
        return TradePlan.from_dict(json.loads(rows[0]["payload"])) if rows else None

    def latest_plan(self, epic: str, *, max_age_hours: Optional[float] = None) -> Optional[TradePlan]:
        rows = self._query(
            "SELECT payload, created_at FROM plans WHERE epic = ? ORDER BY created_at DESC LIMIT 1",
            (epic,),
        )
        if not rows:
            return None
        plan = TradePlan.from_dict(json.loads(rows[0]["payload"]))
        if max_age_hours is not None:
            if utcnow() - plan.created_at > timedelta(hours=max_age_hours):
                return None
        return plan

    # ------------------------------------------------------------------ trades

    def save_trade(self, trade: ManagedTrade) -> None:
        self._execute(
            "INSERT OR REPLACE INTO trades (deal_id, epic, status, updated_at, payload) "
            "VALUES (?,?,?,?,?)",
            (
                trade.deal_id, trade.epic, trade.status.value,
                utcnow().isoformat(), json.dumps(trade.to_dict()),
            ),
        )

    def trade(self, deal_id: str) -> Optional[ManagedTrade]:
        rows = self._query("SELECT payload FROM trades WHERE deal_id = ?", (deal_id,))
        return ManagedTrade.from_dict(json.loads(rows[0]["payload"])) if rows else None

    def trade_by_short_id(self, short_id: str) -> Optional[ManagedTrade]:
        for trade in self.trades_with_status(
            TradeStatus.MANAGING, TradeStatus.PENDING_CONFIRMATION, TradeStatus.ERROR
        ):
            if trade.short_id == short_id or trade.deal_id == short_id:
                return trade
        return None

    def trades_with_status(self, *statuses: TradeStatus) -> List[ManagedTrade]:
        if not statuses:
            return []
        placeholders = ",".join("?" for _ in statuses)
        rows = self._query(
            f"SELECT payload FROM trades WHERE status IN ({placeholders}) ORDER BY updated_at",
            [status.value for status in statuses],
        )
        return [ManagedTrade.from_dict(json.loads(row["payload"])) for row in rows]

    def trades_in_group(self, group_id: str) -> List[ManagedTrade]:
        """Every still-relevant leg of one three-deal basket, leg order first."""
        if not group_id:
            return []
        legs = [
            trade for trade in self.trades_with_status(
                TradeStatus.MANAGING,
                TradeStatus.PENDING_CONFIRMATION,
                TradeStatus.ERROR,
            )
            if trade.group_id == group_id
        ]
        return sorted(legs, key=lambda trade: trade.leg_index)

    def active_trades(self) -> List[ManagedTrade]:
        return self.trades_with_status(TradeStatus.MANAGING, TradeStatus.ERROR)

    def pending_trades(self) -> List[ManagedTrade]:
        return self.trades_with_status(TradeStatus.PENDING_CONFIRMATION)

    def known_deal_ids(self) -> set[str]:
        return {row["deal_id"] for row in self._query("SELECT deal_id FROM trades")}

    # ------------------------------------------------------------------ action journal

    def action(self, key: str) -> Optional[Dict[str, Any]]:
        rows = self._query("SELECT * FROM actions WHERE key = ?", (key,))
        return dict(rows[0]) if rows else None

    def begin_action(self, key: str, deal_id: str, kind: str, detail: str = "") -> bool:
        """Claim an idempotency key.  False means it is already done or in flight."""
        existing = self.action(key)
        if existing and existing["status"] in (ACTION_DONE, ACTION_PENDING):
            return False
        now = utcnow().isoformat()
        self._execute(
            "INSERT OR REPLACE INTO actions "
            "(key, deal_id, kind, status, deal_reference, detail, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (key, deal_id, kind, ACTION_PENDING, None, detail, now, now),
        )
        return True

    def complete_action(self, key: str, deal_reference: str = "", detail: str = "") -> None:
        self._execute(
            "UPDATE actions SET status = ?, deal_reference = ?, detail = ?, updated_at = ? "
            "WHERE key = ?",
            (ACTION_DONE, deal_reference, detail, utcnow().isoformat(), key),
        )

    def fail_action(self, key: str, detail: str = "") -> None:
        self._execute(
            "UPDATE actions SET status = ?, detail = ?, updated_at = ? WHERE key = ?",
            (ACTION_FAILED, detail, utcnow().isoformat(), key),
        )

    def release_action(self, key: str) -> None:
        """Drop a claim so the next cycle can retry it cleanly."""
        self._execute("DELETE FROM actions WHERE key = ? AND status = ?", (key, ACTION_PENDING))

    def pending_actions(self) -> List[Dict[str, Any]]:
        return [
            dict(row)
            for row in self._query(
                "SELECT * FROM actions WHERE status = ? ORDER BY created_at", (ACTION_PENDING,)
            )
        ]

    # ------------------------------------------------------------------ fills

    def record_fill(self, fill: Fill) -> None:
        """Append one closed portion.  Append-only: the record is the evidence."""
        self._execute(
            "INSERT INTO fills (deal_id, epic, ts, stage, payload) VALUES (?,?,?,?,?)",
            (fill.deal_id, fill.epic, fill.ts.isoformat(), fill.stage,
             json.dumps(fill.to_dict())),
        )

    def fills_for(self, deal_id: str) -> List[Fill]:
        return [
            Fill.from_dict(json.loads(row["payload"]))
            for row in self._query(
                "SELECT payload FROM fills WHERE deal_id = ? ORDER BY id", (deal_id,)
            )
        ]

    def fills_since(self, since: datetime) -> List[Fill]:
        return [
            Fill.from_dict(json.loads(row["payload"]))
            for row in self._query(
                "SELECT payload FROM fills WHERE ts >= ? ORDER BY ts", (since.isoformat(),)
            )
        ]

    def has_fill(self, deal_id: str, stage: str) -> bool:
        """Guard against double-recording the same exit across a restart."""
        rows = self._query(
            "SELECT 1 FROM fills WHERE deal_id = ? AND stage = ? LIMIT 1",
            (deal_id, stage),
        )
        return bool(rows)

    def closed_trades_since(self, since: datetime) -> List[ManagedTrade]:
        rows = self._query(
            "SELECT payload FROM trades WHERE status = ? AND updated_at >= ? "
            "ORDER BY updated_at",
            (TradeStatus.CLOSED.value, since.isoformat()),
        )
        return [ManagedTrade.from_dict(json.loads(row["payload"])) for row in rows]

    # ------------------------------------------------------------------ events & kv

    def log_event(self, kind: str, message: str, *, deal_id: str = "", epic: str = "") -> None:
        self._execute(
            "INSERT INTO events (ts, deal_id, epic, kind, message) VALUES (?,?,?,?,?)",
            (utcnow().isoformat(), deal_id, epic, kind, message),
        )

    def events_since(self, since: datetime, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM events WHERE ts >= ?"
        params: List[Any] = [since.isoformat()]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        return [dict(row) for row in self._query(sql + " ORDER BY ts", params)]

    def recent_events(self, limit: int = 30) -> List[Dict[str, Any]]:
        return [
            dict(row)
            for row in self._query(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
            )
        ]

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        rows = self._query("SELECT value FROM kv WHERE key = ?", (key,))
        return rows[0]["value"] if rows else default

    def set(self, key: str, value: str) -> None:
        self._execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?,?)", (key, value))
