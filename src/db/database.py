from __future__ import annotations

import os

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS connections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    external_id TEXT NOT NULL DEFAULT '',
    odoo_url TEXT NOT NULL,
    odoo_db TEXT NOT NULL,
    odoo_username TEXT NOT NULL,
    odoo_api_key TEXT NOT NULL,
    webhook_url TEXT NOT NULL,
    webhook_secret TEXT NOT NULL DEFAULT '',
    poll_interval_seconds INTEGER NOT NULL DEFAULT 60,
    enabled INTEGER NOT NULL DEFAULT 1,
    circuit_state TEXT NOT NULL DEFAULT 'closed',
    circuit_failure_count INTEGER NOT NULL DEFAULT 0,
    circuit_last_failure_at TEXT,
    last_sync_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sync_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    connection_id INTEGER NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    orders_found INTEGER NOT NULL DEFAULT 0,
    orders_sent INTEGER NOT NULL DEFAULT 0,
    orders_failed INTEGER NOT NULL DEFAULT 0,
    orders_skipped INTEGER NOT NULL DEFAULT 0,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS retry_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    connection_id INTEGER NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    odoo_order_id INTEGER NOT NULL,
    odoo_order_name TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    next_retry_at TEXT NOT NULL,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sent_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    connection_id INTEGER NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    odoo_order_id INTEGER NOT NULL,
    odoo_order_name TEXT NOT NULL DEFAULT '',
    odoo_write_date TEXT NOT NULL,
    sent_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_sync_logs_connection ON sync_logs(connection_id);
CREATE INDEX IF NOT EXISTS idx_retry_queue_connection_status ON retry_queue(connection_id, status);
CREATE INDEX IF NOT EXISTS idx_retry_queue_next_retry ON retry_queue(next_retry_at) WHERE status = 'pending';
CREATE UNIQUE INDEX IF NOT EXISTS idx_sent_orders_unique ON sent_orders(connection_id, odoo_order_id, odoo_write_date);
CREATE INDEX IF NOT EXISTS idx_sent_orders_connection ON sent_orders(connection_id);
"""


async def get_connection(db_path: str) -> aiosqlite.Connection:
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def _migrate(db: aiosqlite.Connection) -> None:
    """Aplica migraciones incrementales a tablas existentes."""
    cursor = await db.execute("PRAGMA table_info(connections)")
    columns = {row[1] for row in await cursor.fetchall()}
    if "external_id" not in columns:
        await db.execute("ALTER TABLE connections ADD COLUMN external_id TEXT NOT NULL DEFAULT ''")
        await db.commit()


async def init_db(db_path: str) -> aiosqlite.Connection:
    db = await get_connection(db_path)
    await db.executescript(_SCHEMA)
    await db.commit()
    await _migrate(db)
    return db
