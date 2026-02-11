from __future__ import annotations

import json
from datetime import datetime, timezone

import aiosqlite

from src.db.models import (
    CircuitState,
    Connection,
    RetryItem,
    RetryStatus,
    SentOrder,
    SyncLog,
)
from src.encryption import FieldEncryptor


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class ConnectionRepository:
    def __init__(self, db: aiosqlite.Connection, encryptor: FieldEncryptor) -> None:
        self._db = db
        self._enc = encryptor

    def _row_to_model(self, row: aiosqlite.Row) -> Connection:
        return Connection(
            id=row["id"],
            name=row["name"],
            external_id=row["external_id"] or "",
            odoo_url=row["odoo_url"],
            odoo_db=row["odoo_db"],
            odoo_username=row["odoo_username"],
            odoo_api_key=self._enc.decrypt(row["odoo_api_key"]),
            webhook_url=row["webhook_url"],
            webhook_secret=self._enc.decrypt(row["webhook_secret"]) if row["webhook_secret"] else "",
            poll_interval_seconds=row["poll_interval_seconds"],
            enabled=bool(row["enabled"]),
            circuit_state=CircuitState(row["circuit_state"]),
            circuit_failure_count=row["circuit_failure_count"],
            circuit_last_failure_at=row["circuit_last_failure_at"],
            last_sync_at=row["last_sync_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def list_all(self) -> list[Connection]:
        cursor = await self._db.execute("SELECT * FROM connections ORDER BY name")
        rows = await cursor.fetchall()
        return [self._row_to_model(r) for r in rows]

    async def list_enabled(self) -> list[Connection]:
        cursor = await self._db.execute(
            "SELECT * FROM connections WHERE enabled = 1 ORDER BY name"
        )
        rows = await cursor.fetchall()
        return [self._row_to_model(r) for r in rows]

    async def get(self, conn_id: int) -> Connection | None:
        cursor = await self._db.execute(
            "SELECT * FROM connections WHERE id = ?", (conn_id,)
        )
        row = await cursor.fetchone()
        return self._row_to_model(row) if row else None

    async def create(self, conn: Connection) -> Connection:
        now = _now()
        cursor = await self._db.execute(
            """INSERT INTO connections
               (name, external_id, odoo_url, odoo_db, odoo_username, odoo_api_key,
                webhook_url, webhook_secret, poll_interval_seconds, enabled,
                circuit_state, circuit_failure_count, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                conn.name,
                conn.external_id,
                conn.odoo_url,
                conn.odoo_db,
                conn.odoo_username,
                self._enc.encrypt(conn.odoo_api_key),
                conn.webhook_url,
                self._enc.encrypt(conn.webhook_secret) if conn.webhook_secret else "",
                conn.poll_interval_seconds,
                int(conn.enabled),
                conn.circuit_state.value,
                0,
                now,
                now,
            ),
        )
        await self._db.commit()
        conn.id = cursor.lastrowid
        conn.created_at = now
        conn.updated_at = now
        return conn

    async def update(self, conn: Connection) -> Connection:
        now = _now()
        await self._db.execute(
            """UPDATE connections SET
               name=?, external_id=?, odoo_url=?, odoo_db=?, odoo_username=?, odoo_api_key=?,
               webhook_url=?, webhook_secret=?, poll_interval_seconds=?, enabled=?,
               updated_at=?
               WHERE id=?""",
            (
                conn.name,
                conn.external_id,
                conn.odoo_url,
                conn.odoo_db,
                conn.odoo_username,
                self._enc.encrypt(conn.odoo_api_key),
                conn.webhook_url,
                self._enc.encrypt(conn.webhook_secret) if conn.webhook_secret else "",
                conn.poll_interval_seconds,
                int(conn.enabled),
                now,
                conn.id,
            ),
        )
        await self._db.commit()
        conn.updated_at = now
        return conn

    async def delete(self, conn_id: int) -> None:
        await self._db.execute("DELETE FROM connections WHERE id = ?", (conn_id,))
        await self._db.commit()

    async def update_circuit_state(
        self, conn_id: int, state: CircuitState, failure_count: int
    ) -> None:
        now = _now()
        last_failure = now if state == CircuitState.OPEN else None
        await self._db.execute(
            """UPDATE connections SET
               circuit_state=?, circuit_failure_count=?,
               circuit_last_failure_at=COALESCE(?, circuit_last_failure_at),
               updated_at=?
               WHERE id=?""",
            (state.value, failure_count, last_failure, now, conn_id),
        )
        await self._db.commit()

    async def update_last_sync(self, conn_id: int, sync_at: str) -> None:
        await self._db.execute(
            "UPDATE connections SET last_sync_at=?, updated_at=? WHERE id=?",
            (sync_at, _now(), conn_id),
        )
        await self._db.commit()


class SyncLogRepository:
    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def create(self, log: SyncLog) -> SyncLog:
        cursor = await self._db.execute(
            """INSERT INTO sync_logs
               (connection_id, started_at, finished_at, orders_found,
                orders_sent, orders_failed, orders_skipped, error_message)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                log.connection_id,
                log.started_at,
                log.finished_at,
                log.orders_found,
                log.orders_sent,
                log.orders_failed,
                log.orders_skipped,
                log.error_message,
            ),
        )
        await self._db.commit()
        log.id = cursor.lastrowid
        return log

    async def list_by_connection(
        self, connection_id: int, limit: int = 50
    ) -> list[SyncLog]:
        cursor = await self._db.execute(
            """SELECT * FROM sync_logs
               WHERE connection_id = ?
               ORDER BY id DESC LIMIT ?""",
            (connection_id, limit),
        )
        rows = await cursor.fetchall()
        return [
            SyncLog(
                id=r["id"],
                connection_id=r["connection_id"],
                started_at=r["started_at"],
                finished_at=r["finished_at"],
                orders_found=r["orders_found"],
                orders_sent=r["orders_sent"],
                orders_failed=r["orders_failed"],
                orders_skipped=r["orders_skipped"],
                error_message=r["error_message"],
            )
            for r in rows
        ]


class RetryQueueRepository:
    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def enqueue(self, item: RetryItem) -> RetryItem:
        now = _now()
        cursor = await self._db.execute(
            """INSERT INTO retry_queue
               (connection_id, odoo_order_id, odoo_order_name, payload,
                status, attempts, max_attempts, next_retry_at,
                last_error, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                item.connection_id,
                item.odoo_order_id,
                item.odoo_order_name,
                item.payload,
                item.status.value,
                item.attempts,
                item.max_attempts,
                item.next_retry_at,
                item.last_error,
                now,
                now,
            ),
        )
        await self._db.commit()
        item.id = cursor.lastrowid
        return item

    async def get_pending(
        self, connection_id: int, now: str | None = None
    ) -> list[RetryItem]:
        if now is None:
            now = _now()
        cursor = await self._db.execute(
            """SELECT * FROM retry_queue
               WHERE connection_id = ? AND status = 'pending' AND next_retry_at <= ?
               ORDER BY next_retry_at""",
            (connection_id, now),
        )
        rows = await cursor.fetchall()
        return [self._row_to_model(r) for r in rows]

    async def list_by_connection(
        self, connection_id: int, limit: int = 100
    ) -> list[RetryItem]:
        cursor = await self._db.execute(
            """SELECT * FROM retry_queue
               WHERE connection_id = ?
               ORDER BY id DESC LIMIT ?""",
            (connection_id, limit),
        )
        rows = await cursor.fetchall()
        return [self._row_to_model(r) for r in rows]

    async def update_status(
        self,
        item_id: int,
        status: RetryStatus,
        attempts: int | None = None,
        next_retry_at: str | None = None,
        last_error: str | None = None,
    ) -> None:
        now = _now()
        await self._db.execute(
            """UPDATE retry_queue SET
               status=?,
               attempts=COALESCE(?, attempts),
               next_retry_at=COALESCE(?, next_retry_at),
               last_error=COALESCE(?, last_error),
               updated_at=?
               WHERE id=?""",
            (status.value, attempts, next_retry_at, last_error, now, item_id),
        )
        await self._db.commit()

    async def get_summary(self, connection_id: int) -> dict[str, int]:
        cursor = await self._db.execute(
            """SELECT status, COUNT(*) as cnt FROM retry_queue
               WHERE connection_id = ? GROUP BY status""",
            (connection_id,),
        )
        rows = await cursor.fetchall()
        return {r["status"]: r["cnt"] for r in rows}

    def _row_to_model(self, row: aiosqlite.Row) -> RetryItem:
        return RetryItem(
            id=row["id"],
            connection_id=row["connection_id"],
            odoo_order_id=row["odoo_order_id"],
            odoo_order_name=row["odoo_order_name"],
            payload=row["payload"],
            status=RetryStatus(row["status"]),
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            next_retry_at=row["next_retry_at"],
            last_error=row["last_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


class SentOrderRepository:
    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def mark_sent(self, order: SentOrder) -> None:
        await self._db.execute(
            """INSERT OR IGNORE INTO sent_orders
               (connection_id, odoo_order_id, odoo_order_name, odoo_write_date, sent_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                order.connection_id,
                order.odoo_order_id,
                order.odoo_order_name,
                order.odoo_write_date,
                order.sent_at or _now(),
            ),
        )
        await self._db.commit()

    async def is_sent(
        self, connection_id: int, odoo_order_id: int, write_date: str
    ) -> bool:
        cursor = await self._db.execute(
            """SELECT 1 FROM sent_orders
               WHERE connection_id = ? AND odoo_order_id = ? AND odoo_write_date = ?""",
            (connection_id, odoo_order_id, write_date),
        )
        return await cursor.fetchone() is not None

    async def get_sent_ids(self, connection_id: int) -> set[tuple[int, str]]:
        cursor = await self._db.execute(
            "SELECT odoo_order_id, odoo_write_date FROM sent_orders WHERE connection_id = ?",
            (connection_id,),
        )
        rows = await cursor.fetchall()
        return {(r["odoo_order_id"], r["odoo_write_date"]) for r in rows}

    async def list_by_connection(
        self, connection_id: int, limit: int = 30
    ) -> list[SentOrder]:
        cursor = await self._db.execute(
            """SELECT * FROM sent_orders
               WHERE connection_id = ?
               ORDER BY sent_at DESC LIMIT ?""",
            (connection_id, limit),
        )
        rows = await cursor.fetchall()
        return [
            SentOrder(
                id=r["id"],
                connection_id=r["connection_id"],
                odoo_order_id=r["odoo_order_id"],
                odoo_order_name=r["odoo_order_name"],
                odoo_write_date=r["odoo_write_date"],
                sent_at=r["sent_at"],
            )
            for r in rows
        ]

    async def trim_to_limit(self, connection_id: int, limit: int = 30) -> None:
        await self._db.execute(
            """DELETE FROM sent_orders WHERE connection_id = ? AND id NOT IN (
                   SELECT id FROM sent_orders WHERE connection_id = ?
                   ORDER BY sent_at DESC LIMIT ?
               )""",
            (connection_id, connection_id, limit),
        )
        await self._db.commit()
