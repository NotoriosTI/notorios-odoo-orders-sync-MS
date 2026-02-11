# Odoo Order Poller - Contexto del Microservicio

## Qué hace

Microservicio Python que hace polling a instancias Odoo SaaS via JSON-RPC, extrae órdenes de venta confirmadas (state=sale/done) y las envía como webhooks normalizados a StockMaster. Multi-tenant: soporta múltiples conexiones Odoo simultáneas.

## Estructura

```
src/
├── config.py              # Settings desde env vars (POLLER_ENCRYPTION_KEY requerida)
├── encryption.py          # FieldEncryptor con Fernet para API keys y webhook secrets
├── main.py                # Entry point → argparse CLI
├── cli.py                 # Subcomandos: run, add, list, edit, delete, test, logs, retries, retry, discard, reset-circuit
├── db/
│   ├── models.py          # Dataclasses: Connection, SyncLog, RetryItem, SentOrder + enums CircuitState, RetryStatus
│   ├── database.py        # init_db(): SQLite WAL mode, foreign keys, 4 tablas
│   └── repositories.py    # ConnectionRepository (encrypt/decrypt transparente), SyncLogRepository, RetryQueueRepository, SentOrderRepository
├── odoo/
│   ├── client.py          # OdooClient: authenticate, search_read, read. Re-auth automática, detección HTTP 429
│   └── mapper.py          # fetch_batch_data (evita N+1), map_order_to_webhook_payload. SKU fallback: ODOO-{db}-{product_id}. Filtra items qty=0
└── poller/
    ├── circuit_breaker.py  # CLOSED→OPEN(5 fallos)→HALF_OPEN(120s)→CLOSED(2 éxitos). reset() manual
    ├── sender.py           # WebhookSender: POST con X-Webhook-Secret, X-Odoo-Connection-Id. Backoff: 30s,60s,120s,240s,600s
    ├── worker.py           # PollWorker.execute(): 1 ciclo completo (check CB → auth → fetch → dedup → batch → send → retry queue → sync_log)
    └── scheduler.py        # 1 asyncio.Task + 1 httpx.AsyncClient por conexión (bulkhead). Graceful shutdown
```

## Flujo de un ciclo de polling (PollWorker.execute)

1. Circuit breaker permite? → Si OPEN, skip
2. Autenticar en Odoo (o reusar sesión)
3. search_read sale.order con state in [sale,done] y write_date > last_sync_at
4. Filtrar ya enviadas via sent_orders (idempotencia por connection_id + order_id + write_date)
5. Batch fetch: partners, order lines, products, templates (evita N+1)
6. Por cada orden: mapear a payload → enviar webhook → OK=sent_orders / Fallo=retry_queue
7. Actualizar last_sync_at, procesar retry_queue pendientes, registrar sync_log
8. Actualizar circuit breaker (record_success / record_failure)

## Decisiones técnicas

- **Sin TUI**: CLI simple con argparse, sin dependencia de Textual
- **Montos**: Sin conversión ×100, se pasan directo de Odoo
- **Encriptación**: Fernet con master key desde POLLER_ENCRYPTION_KEY
- **Deploy**: Docker con docker-compose, SQLite en volume /app/data
- **Aislamiento**: Cada conexión en su propio Task y HTTP client. Error en una no afecta a otras
- **Odoo JSON-RPC**: execute_kw con args=[positional_args] (no *args). kwargs de search_read solo incluye limit/order si tienen valor

## Comandos Docker

```bash
docker compose run --rm poller python -m src.main add          # Agregar conexión
docker compose run --rm poller python -m src.main list         # Listar
docker compose run --rm poller python -m src.main test 1       # Probar conexión
docker compose up -d                                            # Iniciar polling
docker compose logs -f                                          # Ver logs
docker compose down                                             # Detener
docker compose run --rm poller python -m src.main logs -c 1    # Ver sync logs
docker compose run --rm poller python -m src.main retries      # Ver retry queue
docker compose run --rm poller python -m src.main reset-circuit 1  # Resetear CB
```

## Tests

```bash
pytest tests/ -v  # 27 tests: encryption, database, odoo_client, mapper, circuit_breaker, sender
```
