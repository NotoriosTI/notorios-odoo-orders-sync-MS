from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime


class CircuitState(enum.Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class RetryStatus(enum.Enum):
    PENDING = "pending"
    SENT = "sent"
    DISCARDED = "discarded"


@dataclass
class Connection:
    id: int | None = None
    name: str = ""
    external_id: str = ""  # ID generado por StockMaster para X-Odoo-Connection-Id
    odoo_url: str = ""
    odoo_db: str = ""
    odoo_username: str = ""
    odoo_api_key: str = ""  # encriptado en DB
    webhook_url: str = ""
    webhook_secret: str = ""  # encriptado en DB
    poll_interval_seconds: int = 60
    enabled: bool = True
    circuit_state: CircuitState = CircuitState.CLOSED
    circuit_failure_count: int = 0
    circuit_last_failure_at: str | None = None
    last_sync_at: str | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass
class SyncLog:
    id: int | None = None
    connection_id: int = 0
    started_at: str = ""
    finished_at: str = ""
    orders_found: int = 0
    orders_sent: int = 0
    orders_failed: int = 0
    orders_skipped: int = 0
    error_message: str | None = None


@dataclass
class RetryItem:
    id: int | None = None
    connection_id: int = 0
    odoo_order_id: int = 0
    odoo_order_name: str = ""
    payload: str = ""  # JSON serializado
    status: RetryStatus = RetryStatus.PENDING
    attempts: int = 0
    max_attempts: int = 5
    next_retry_at: str = ""
    last_error: str | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass
class SentOrder:
    id: int | None = None
    connection_id: int = 0
    odoo_order_id: int = 0
    odoo_order_name: str = ""
    odoo_write_date: str = ""
    sent_at: str = ""
