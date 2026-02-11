from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import signal
import sys
from datetime import datetime, timezone

import httpx

from src.config import Settings
from src.db.database import init_db
from src.db.models import CircuitState, Connection, RetryStatus
from src.db.repositories import (
    ConnectionRepository,
    RetryQueueRepository,
    SentOrderRepository,
    SyncLogRepository,
)
from src.encryption import FieldEncryptor
from src.odoo.client import OdooClient
from src.odoo.mapper import fetch_batch_data, map_order_to_webhook_payload
from src.poller.scheduler import Scheduler
from src.poller.sender import WebhookSender

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="odoo-poller",
        description="Odoo Order Poller - Polling de órdenes Odoo hacia webhooks",
    )
    sub = parser.add_subparsers(dest="command")

    # run
    sub.add_parser("run", help="Iniciar el polling de todas las conexiones habilitadas")

    # add
    sub.add_parser("add", help="Agregar una nueva conexión Odoo (interactivo)")

    # list
    sub.add_parser("list", help="Listar todas las conexiones")

    # edit
    p = sub.add_parser("edit", help="Editar una conexión existente")
    p.add_argument("id", type=int, help="ID de la conexión")

    # delete
    p = sub.add_parser("delete", help="Eliminar una conexión")
    p.add_argument("id", type=int, help="ID de la conexión")

    # test
    p = sub.add_parser("test", help="Probar conexión Odoo y/o webhook")
    p.add_argument("id", type=int, help="ID de la conexión")

    # logs
    p = sub.add_parser("logs", help="Ver logs de sincronización")
    p.add_argument("--connection", "-c", type=int, help="Filtrar por ID de conexión")
    p.add_argument("--limit", "-n", type=int, default=20, help="Cantidad de logs (default: 20)")

    # retries
    p = sub.add_parser("retries", help="Ver cola de reintentos")
    p.add_argument("--connection", "-c", type=int, help="Filtrar por ID de conexión")

    # retry
    p = sub.add_parser("retry", help="Reintentar un item inmediatamente")
    p.add_argument("id", type=int, help="ID del retry item")

    # discard
    p = sub.add_parser("discard", help="Descartar un retry item")
    p.add_argument("id", type=int, help="ID del retry item")

    # reset-circuit
    p = sub.add_parser("reset-circuit", help="Resetear circuit breaker de una conexión")
    p.add_argument("id", type=int, help="ID de la conexión")

    # send
    p = sub.add_parser("send", help="Enviar webhooks manualmente de órdenes ya registradas")
    p.add_argument("-c", "--connection", type=int, required=True, help="ID de la conexión")
    p.add_argument("--last", type=int, default=0, help="Enviar las últimas N órdenes automáticamente")

    return parser


class AppContext:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.encryptor = FieldEncryptor(settings.encryption_key)
        self.db = None
        self.conn_repo: ConnectionRepository | None = None
        self.sync_log_repo: SyncLogRepository | None = None
        self.retry_repo: RetryQueueRepository | None = None
        self.sent_repo: SentOrderRepository | None = None

    async def init(self) -> None:
        self.db = await init_db(self.settings.db_path)
        self.conn_repo = ConnectionRepository(self.db, self.encryptor)
        self.sync_log_repo = SyncLogRepository(self.db)
        self.retry_repo = RetryQueueRepository(self.db)
        self.sent_repo = SentOrderRepository(self.db)

    async def close(self) -> None:
        if self.db:
            await self.db.close()


# ── Helpers ──────────────────────────────────────────────────

def _prompt(label: str, default: str = "", password: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    if password:
        value = getpass.getpass(f"{label}{suffix}: ")
    else:
        value = input(f"{label}{suffix}: ")
    return value.strip() or default


def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    if not rows:
        print("  (sin resultados)")
        return

    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], len(cell))

    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print(fmt.format(*["-" * w for w in widths]))
    for row in rows:
        padded = row + [""] * (len(headers) - len(row))
        print(fmt.format(*padded))


# ── Commands ─────────────────────────────────────────────────

async def cmd_run(ctx: AppContext) -> None:
    print("Iniciando polling...")

    async def on_sync(conn_id: int, sync_log) -> None:
        if sync_log:
            if sync_log.error_message:
                logger.error(
                    "Conn #%d: ERROR - %s", conn_id, sync_log.error_message
                )
            else:
                logger.info(
                    "Conn #%d: encontradas=%d enviadas=%d fallidas=%d skip=%d",
                    conn_id,
                    sync_log.orders_found,
                    sync_log.orders_sent,
                    sync_log.orders_failed,
                    sync_log.orders_skipped,
                )

    async def on_circuit(conn_id: int, state: CircuitState) -> None:
        logger.warning("Conn #%d: circuit breaker -> %s", conn_id, state.value)

    scheduler = Scheduler(
        conn_repo=ctx.conn_repo,
        sync_log_repo=ctx.sync_log_repo,
        retry_repo=ctx.retry_repo,
        sent_repo=ctx.sent_repo,
        on_sync_complete=on_sync,
        on_circuit_state_change=on_circuit,
    )

    await scheduler.start()

    conns = await ctx.conn_repo.list_enabled()
    if conns:
        print(f"Polling activo para {len(conns)} conexión(es). Ctrl+C para detener.\n")
    else:
        print("No hay conexiones habilitadas. Esperando que se agreguen...")
        print("Usa 'python -m src.main add' para crear una.\n")

    stop_event = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    # Revisar periódicamente si hay conexiones nuevas/removidas
    async def watch_connections() -> None:
        known_ids: set[int] = {c.id for c in conns}
        while not stop_event.is_set():
            await asyncio.sleep(15)
            current = await ctx.conn_repo.list_enabled()
            current_ids = {c.id for c in current}

            for conn in current:
                if conn.id not in known_ids:
                    logger.info("Nueva conexión detectada: #%d '%s'", conn.id, conn.name)
                    await scheduler.add_connection(conn)

            for removed_id in known_ids - current_ids:
                logger.info("Conexión removida/deshabilitada: #%d", removed_id)
                await scheduler.remove_connection(removed_id)

            known_ids = current_ids

    watcher = asyncio.create_task(watch_connections())

    await stop_event.wait()

    watcher.cancel()
    print("\nDeteniendo polling...")
    await scheduler.stop()
    print("Detenido.")


async def cmd_add(ctx: AppContext) -> None:
    print("=== Nueva Conexión Odoo ===\n")

    conn = Connection(
        name=_prompt("Nombre"),
        external_id=_prompt("External ID (generado por StockMaster)"),
        odoo_url=_prompt("URL Odoo (ej: https://miempresa.odoo.com)"),
        odoo_db=_prompt("Base de datos Odoo"),
        odoo_username=_prompt("Usuario Odoo"),
        odoo_api_key=_prompt("API Key", password=True),
        webhook_url=_prompt("Webhook URL", default=ctx.settings.default_webhook_url),
        webhook_secret=_prompt("Webhook Secret (opcional)", password=True),
        poll_interval_seconds=int(_prompt("Intervalo de polling (segundos)", default="60")),
        enabled=_prompt("Habilitada (s/n)", default="s").lower() in ("s", "si", "y", "yes"),
    )

    if not conn.name or not conn.odoo_url or not conn.odoo_db:
        print("Error: Nombre, URL y DB son requeridos.")
        return

    created = await ctx.conn_repo.create(conn)
    print(f"\nConexión creada con ID: {created.id}")


async def cmd_list(ctx: AppContext) -> None:
    connections = await ctx.conn_repo.list_all()
    if not connections:
        print("No hay conexiones configuradas. Usa 'odoo-poller add' para crear una.")
        return

    headers = ["ID", "Nombre", "External ID", "URL", "DB", "Intervalo", "Estado", "Circuit", "Último Sync"]
    rows = []
    for c in connections:
        rows.append([
            str(c.id),
            c.name,
            c.external_id or "(sin configurar)",
            c.odoo_url,
            c.odoo_db,
            f"{c.poll_interval_seconds}s",
            "ON" if c.enabled else "OFF",
            c.circuit_state.value,
            c.last_sync_at or "nunca",
        ])

    _print_table(headers, rows)


async def cmd_edit(ctx: AppContext, conn_id: int) -> None:
    conn = await ctx.conn_repo.get(conn_id)
    if not conn:
        print(f"Conexión #{conn_id} no encontrada.")
        return

    print(f"=== Editar Conexión #{conn_id}: {conn.name} ===")
    print("(Deja vacío para mantener el valor actual)\n")

    conn.name = _prompt("Nombre", default=conn.name)
    conn.external_id = _prompt("External ID (StockMaster)", default=conn.external_id)
    conn.odoo_url = _prompt("URL Odoo", default=conn.odoo_url)
    conn.odoo_db = _prompt("Base de datos", default=conn.odoo_db)
    conn.odoo_username = _prompt("Usuario", default=conn.odoo_username)

    new_key = _prompt("API Key (vacío = sin cambios)", password=True)
    if new_key:
        conn.odoo_api_key = new_key

    conn.webhook_url = _prompt("Webhook URL", default=conn.webhook_url)
    new_secret = _prompt("Webhook Secret (vacío = sin cambios)", password=True)
    if new_secret:
        conn.webhook_secret = new_secret

    conn.poll_interval_seconds = int(
        _prompt("Intervalo (segundos)", default=str(conn.poll_interval_seconds))
    )
    enabled_str = _prompt("Habilitada (s/n)", default="s" if conn.enabled else "n")
    conn.enabled = enabled_str.lower() in ("s", "si", "y", "yes")

    await ctx.conn_repo.update(conn)
    print(f"\nConexión #{conn_id} actualizada.")


async def cmd_delete(ctx: AppContext, conn_id: int) -> None:
    conn = await ctx.conn_repo.get(conn_id)
    if not conn:
        print(f"Conexión #{conn_id} no encontrada.")
        return

    confirm = input(f"Eliminar '{conn.name}' (ID: {conn_id})? (s/n): ")
    if confirm.lower() not in ("s", "si", "y", "yes"):
        print("Cancelado.")
        return

    await ctx.conn_repo.delete(conn_id)
    print(f"Conexión #{conn_id} eliminada.")


async def cmd_test(ctx: AppContext, conn_id: int) -> None:
    conn = await ctx.conn_repo.get(conn_id)
    if not conn:
        print(f"Conexión #{conn_id} no encontrada.")
        return

    # Test Odoo
    print(f"Probando conexión Odoo '{conn.name}'...")
    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            client = OdooClient(
                conn.odoo_url, conn.odoo_db, conn.odoo_username, conn.odoo_api_key,
                http_client=http,
            )
            uid = await client.authenticate()
            print(f"  Odoo OK - UID: {uid}")
    except Exception as e:
        print(f"  Odoo ERROR: {e}")

    # Test Webhook
    if conn.webhook_url:
        print(f"Probando webhook...")
        headers = {"Content-Type": "application/json"}
        if conn.webhook_secret:
            headers["X-Webhook-Secret"] = conn.webhook_secret

        headers["X-Odoo-Connection-Id"] = conn.external_id
        payload = {"source": "odoo", "test": True, "connection_name": conn.name}
        try:
            async with httpx.AsyncClient(timeout=15.0) as http:
                resp = await http.post(conn.webhook_url, json=payload, headers=headers)
                resp.raise_for_status()
                print(f"  Webhook OK - Status: {resp.status_code}")
        except Exception as e:
            print(f"  Webhook ERROR: {e}")


async def cmd_logs(ctx: AppContext, connection_id: int | None, limit: int) -> None:
    if connection_id:
        logs = await ctx.sync_log_repo.list_by_connection(connection_id, limit=limit)
    else:
        connections = await ctx.conn_repo.list_all()
        logs = []
        for c in connections:
            logs.extend(await ctx.sync_log_repo.list_by_connection(c.id, limit=limit))
        logs.sort(key=lambda l: l.id or 0, reverse=True)
        logs = logs[:limit]

    headers = ["ID", "Conn", "Inicio", "Encontradas", "Enviadas", "Fallidas", "Skip", "Error"]
    rows = []
    for log in logs:
        rows.append([
            str(log.id),
            str(log.connection_id),
            log.started_at,
            str(log.orders_found),
            str(log.orders_sent),
            str(log.orders_failed),
            str(log.orders_skipped),
            (log.error_message or "")[:50],
        ])

    _print_table(headers, rows)


async def cmd_retries(ctx: AppContext, connection_id: int | None) -> None:
    if connection_id:
        items = await ctx.retry_repo.list_by_connection(connection_id)
    else:
        connections = await ctx.conn_repo.list_all()
        items = []
        for c in connections:
            items.extend(await ctx.retry_repo.list_by_connection(c.id))
        items.sort(key=lambda i: i.id or 0, reverse=True)

    headers = ["ID", "Conn", "Orden", "Estado", "Intentos", "Próximo Retry", "Error"]
    rows = []
    for item in items:
        rows.append([
            str(item.id),
            str(item.connection_id),
            item.odoo_order_name or str(item.odoo_order_id),
            item.status.value,
            f"{item.attempts}/{item.max_attempts}",
            item.next_retry_at,
            (item.last_error or "")[:40],
        ])

    _print_table(headers, rows)


async def cmd_retry_now(ctx: AppContext, item_id: int) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    await ctx.retry_repo.update_status(
        item_id, RetryStatus.PENDING, next_retry_at=now
    )
    print(f"Retry #{item_id} marcado para reintento inmediato.")


async def cmd_discard(ctx: AppContext, item_id: int) -> None:
    await ctx.retry_repo.update_status(item_id, RetryStatus.DISCARDED)
    print(f"Retry #{item_id} descartado.")


async def cmd_reset_circuit(ctx: AppContext, conn_id: int) -> None:
    conn = await ctx.conn_repo.get(conn_id)
    if not conn:
        print(f"Conexión #{conn_id} no encontrada.")
        return

    await ctx.conn_repo.update_circuit_state(conn_id, CircuitState.CLOSED, 0)
    print(f"Circuit breaker de '{conn.name}' reseteado a CLOSED.")


async def cmd_send(ctx: AppContext, connection_id: int, last: int) -> None:
    conn = await ctx.conn_repo.get(connection_id)
    if not conn:
        print(f"Conexión #{connection_id} no encontrada.")
        return

    sent_orders = await ctx.sent_repo.list_by_connection(connection_id)
    if not sent_orders:
        print(f"No hay órdenes registradas para la conexión '{conn.name}'.")
        return

    # Seleccionar órdenes
    if last > 0:
        selected = sent_orders[:last]
    else:
        print(f"\nÓrdenes registradas para '{conn.name}':\n")
        headers = ["#", "Orden", "Odoo ID", "Write Date", "Registrada"]
        rows = []
        for i, so in enumerate(sent_orders):
            rows.append([
                str(i + 1),
                so.odoo_order_name or str(so.odoo_order_id),
                str(so.odoo_order_id),
                so.odoo_write_date,
                so.sent_at,
            ])
        _print_table(headers, rows)

        print()
        raw = input("Índices a enviar (separados por coma, ej: 1,3,5): ").strip()
        if not raw:
            print("Cancelado.")
            return

        try:
            indices = [int(x.strip()) for x in raw.split(",")]
        except ValueError:
            print("Error: ingresa números separados por coma.")
            return

        selected = []
        for idx in indices:
            if 1 <= idx <= len(sent_orders):
                selected.append(sent_orders[idx - 1])
            else:
                print(f"Índice {idx} fuera de rango, ignorado.")

    if not selected:
        print("No se seleccionaron órdenes.")
        return

    print(f"\nEnviando {len(selected)} orden(es) vía webhook...")

    async with httpx.AsyncClient(timeout=30.0) as http:
        client = OdooClient(
            conn.odoo_url, conn.odoo_db, conn.odoo_username, conn.odoo_api_key,
            http_client=http,
        )
        sender = WebhookSender(http)

        await client.authenticate()

        ok = 0
        fail = 0
        for so in selected:
            try:
                orders = await client.search_read(
                    "sale.order",
                    [["id", "=", so.odoo_order_id]],
                    [
                        "name", "state", "date_order", "write_date",
                        "partner_id", "partner_shipping_id",
                        "amount_untaxed", "amount_tax", "amount_total",
                        "currency_id", "note",
                    ],
                )
                if not orders:
                    print(f"  {so.odoo_order_name}: orden no encontrada en Odoo, saltando.")
                    fail += 1
                    continue

                batch = await fetch_batch_data(client, orders)
                payload = map_order_to_webhook_payload(
                    orders[0], batch, conn.odoo_db, conn.external_id
                )
                await sender.send(
                    conn.webhook_url, payload, conn.webhook_secret, conn.external_id
                )
                print(f"  {so.odoo_order_name}: OK")
                ok += 1
            except Exception as e:
                print(f"  {so.odoo_order_name}: ERROR - {e}")
                fail += 1

    print(f"\nResumen: {ok} enviadas, {fail} fallidas.")


# ── Main dispatch ────────────────────────────────────────────

async def run_cli(args: argparse.Namespace) -> None:
    settings = Settings.load()
    ctx = AppContext(settings)
    await ctx.init()

    try:
        match args.command:
            case "run":
                await cmd_run(ctx)
            case "add":
                await cmd_add(ctx)
            case "list":
                await cmd_list(ctx)
            case "edit":
                await cmd_edit(ctx, args.id)
            case "delete":
                await cmd_delete(ctx, args.id)
            case "test":
                await cmd_test(ctx, args.id)
            case "logs":
                await cmd_logs(ctx, args.connection, args.limit)
            case "retries":
                await cmd_retries(ctx, args.connection)
            case "retry":
                await cmd_retry_now(ctx, args.id)
            case "discard":
                await cmd_discard(ctx, args.id)
            case "reset-circuit":
                await cmd_reset_circuit(ctx, args.id)
            case "send":
                await cmd_send(ctx, args.connection, args.last)
            case _:
                build_parser().print_help()
    finally:
        await ctx.close()
