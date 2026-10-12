from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from threading import RLock
from time import time
from typing import Any, Iterable

from .models import BotFlags, Fill, HaltReason, Order, OrderStatus, OrderType, Position, Side


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._write_depth = 0
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self):
        with self._lock:
            outer = self._write_depth == 0
            self._write_depth += 1
            try:
                yield
                if outer:
                    self.conn.commit()
            except BaseException:
                if outer:
                    self.conn.rollback()
                raise
            finally:
                self._write_depth -= 1

    def _init_schema(self) -> None:
        with self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS kv (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    price TEXT NOT NULL,
                    equity TEXT NOT NULL,
                    capital TEXT NOT NULL DEFAULT '0'
                );
                CREATE TABLE IF NOT EXISTS orders (
                    client_order_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fills (
                    fill_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    timestamp REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    details TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS idempotency (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                """
            )
        if self.get_json("flags") is None:
            self.set_json("flags", BotFlags().to_json())

        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(samples)")}
        if "capital" not in columns:
            with self.conn:
                self.conn.execute("ALTER TABLE samples ADD COLUMN capital TEXT NOT NULL DEFAULT '0'")

    def record_sample(self, timestamp: float, price: Decimal, equity: Decimal, capital: Decimal = Decimal("0")) -> None:
        with self.transaction():
            previous = self.conn.execute("SELECT id, timestamp, capital FROM samples ORDER BY id DESC LIMIT 1").fetchone()
            if previous and int(previous["timestamp"] // 60) == int(timestamp // 60) and previous["capital"] == str(capital):
                self.conn.execute("UPDATE samples SET timestamp=?, price=?, equity=? WHERE id=?",
                                  (timestamp, str(price), str(equity), previous["id"]))
                return
            self.conn.execute("INSERT INTO samples(timestamp, price, equity, capital) VALUES(?, ?, ?, ?)",
                              (timestamp, str(price), str(equity), str(capital)))

    def samples(self, limit: int = 300) -> list[dict]:
        rows = self.conn.execute("SELECT timestamp, price, equity, capital FROM samples ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in reversed(rows)]

    def set_json(self, key: str, value: Any) -> None:
        with self.transaction():
            self.conn.execute(
                "INSERT INTO kv(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    def get_json(self, key: str) -> Any:
        row = self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        return json.loads(row["value"])

    def add_event(self, level: str, message: str, details: dict[str, Any] | None = None) -> None:
        with self.transaction():
            self.conn.execute(
                "INSERT INTO events(timestamp, level, message, details) VALUES(?, ?, ?, ?)",
                (time(), level, message, json.dumps(details or {}, ensure_ascii=False)),
            )

    def events(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT timestamp, level, message, details FROM events ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "timestamp": row["timestamp"],
                "level": row["level"],
                "message": row["message"],
                "details": json.loads(row["details"]),
            }
            for row in rows
        ]

    def save_order(self, order: Order) -> None:
        with self.transaction():
            self.conn.execute(
                "INSERT INTO orders(client_order_id, payload, status, purpose, updated_at) "
                "VALUES(?, ?, ?, ?, ?) ON CONFLICT(client_order_id) DO UPDATE SET "
                "payload=excluded.payload, status=excluded.status, purpose=excluded.purpose, updated_at=excluded.updated_at",
                (
                    order.client_order_id,
                    json.dumps(order.to_json(), ensure_ascii=False),
                    order.status.value,
                    order.purpose,
                    order.updated_at,
                ),
            )

    def load_order(self, client_order_id: str) -> Order | None:
        row = self.conn.execute(
            "SELECT payload FROM orders WHERE client_order_id = ?", (client_order_id,)
        ).fetchone()
        if row is None:
            return None
        return _order_from_payload(json.loads(row["payload"]))

    def list_orders(self, statuses: Iterable[OrderStatus] | None = None) -> list[Order]:
        if statuses is None:
            rows = self.conn.execute("SELECT payload FROM orders ORDER BY updated_at DESC").fetchall()
        else:
            status_values = [status.value for status in statuses]
            placeholders = ",".join("?" for _ in status_values)
            rows = self.conn.execute(
                f"SELECT payload FROM orders WHERE status IN ({placeholders}) ORDER BY updated_at DESC",
                status_values,
            ).fetchall()
        return [_order_from_payload(json.loads(row["payload"])) for row in rows]

    def save_fill(self, fill: Fill) -> bool:
        with self.transaction():
            try:
                self.conn.execute(
                    "INSERT INTO fills(fill_id, payload, timestamp) VALUES(?, ?, ?)",
                    (fill.fill_id, json.dumps(fill.to_json(), ensure_ascii=False), fill.timestamp),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def list_fills(self, limit: int = 100) -> list[Fill]:
        rows = self.conn.execute(
            "SELECT payload FROM fills ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_fill_from_payload(json.loads(row["payload"])) for row in rows]

    def save_position(self, position: Position) -> None:
        self.set_json("position", position.to_json())

    def load_position(self, symbol: str) -> Position:
        raw = self.get_json("position") or {"symbol": symbol}
        return Position(
            symbol=raw.get("symbol", symbol),
            quantity=Decimal(raw.get("quantity", "0")),
            average_entry_price=Decimal(raw.get("average_entry_price", "0")),
            realized_pnl=Decimal(raw.get("realized_pnl", "0")),
            fees_paid=Decimal(raw.get("fees_paid", "0")),
            funding_paid=Decimal(raw.get("funding_paid", "0")),
        )

    def flags(self) -> BotFlags:
        raw = self.get_json("flags") or {}
        reason = HaltReason(raw.get("halt_reason", HaltReason.NONE.value))
        return BotFlags(
            halt_reason=reason,
            can_enter=bool(raw.get("can_enter", True)),
            needs_reconciliation=bool(raw.get("needs_reconciliation", False)),
            manual_resume_required=bool(raw.get("manual_resume_required", False)),
        )

    def set_flags(self, flags: BotFlags) -> None:
        self.set_json("flags", flags.to_json())

    def remember_idempotency(self, key: str, value: dict[str, Any]) -> bool:
        with self.transaction():
            try:
                self.conn.execute(
                    "INSERT INTO idempotency(key, value, created_at) VALUES(?, ?, ?)",
                    (key, json.dumps(value, ensure_ascii=False), time()),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def get_idempotency(self, key: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT value FROM idempotency WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        return json.loads(row["value"])


def _order_from_payload(raw: dict[str, Any]) -> Order:
    return Order(
        client_order_id=raw["client_order_id"],
        exchange_order_id=raw.get("exchange_order_id"),
        symbol=raw["symbol"],
        side=Side(raw["side"]),
        order_type=OrderType(raw["order_type"]),
        quantity=Decimal(raw["quantity"]),
        price=Decimal(raw["price"]) if raw.get("price") is not None else None,
        status=OrderStatus(raw["status"]),
        filled_quantity=Decimal(raw.get("filled_quantity", "0")),
        average_fill_price=Decimal(raw.get("average_fill_price", "0")),
        stop_price=Decimal(raw["stop_price"]) if raw.get("stop_price") is not None else None,
        reduce_only=bool(raw.get("reduce_only", False)),
        post_only=bool(raw.get("post_only", True)),
        close_position=bool(raw.get("close_position", False)),
        level_index=raw.get("level_index"),
        purpose=raw.get("purpose", "entry"),
        created_at=float(raw.get("created_at", time())),
        updated_at=float(raw.get("updated_at", time())),
    )


def _fill_from_payload(raw: dict[str, Any]) -> Fill:
    return Fill(
        fill_id=raw["fill_id"],
        client_order_id=raw["client_order_id"],
        symbol=raw["symbol"],
        side=Side(raw["side"]),
        quantity=Decimal(raw["quantity"]),
        price=Decimal(raw["price"]),
        fee=Decimal(raw["fee"]),
        fee_asset=raw["fee_asset"],
        realized_pnl=Decimal(raw.get("realized_pnl", "0")),
        funding=Decimal(raw.get("funding", "0")),
        timestamp=float(raw.get("timestamp", time())),
        maker=bool(raw.get("maker", True)),
    )
