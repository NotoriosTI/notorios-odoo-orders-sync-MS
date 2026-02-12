# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Qué hace

Microservicio Python que hace polling a instancias Odoo SaaS via JSON-RPC, extrae órdenes de venta confirmadas (state=sale/done) y las envía como webhooks normalizados a StockMaster. Multi-tenant: soporta múltiples conexiones Odoo simultáneas.

## Comandos de desarrollo

```bash
# Setup local
cp .env.example .env  # Configurar POLLER_ENCRYPTION_KEY (obligatoria)
pip install -e ".[dev]"

# Tests
pytest tests/ -v                      # Todos (27 tests)
pytest tests/test_mapper.py -v        # Un archivo
pytest tests/test_mapper.py::test_sku_fallback -v  # Un test específico

# Ejecutar localmente
python -m src.main run                # Iniciar polling
python -m src.main add                # Agregar conexión (interactivo)
python -m src.main list               # Listar conexiones
python -m src.main test 1             # Probar conexión Odoo + webhook
python -m src.main logs -c 1 -n 50   # Ver sync logs
python -m src.main retries -c 1      # Ver retry queue
python -m src.main send -c 1 --last 3  # Re-enviar webhooks

# Docker
docker compose build
docker compose up -d                  # Iniciar
docker compose run --rm poller python -m src.main <comando>
```

## Comandos de producción

El contenedor se llama `notorios-odoo-poller-orders-ms`. Usar `docker exec` para ejecutar comandos dentro del contenedor en ejecución:

```bash
# Agregar conexión (interactivo)
docker exec -it notorios-odoo-poller-orders-ms python -m src.main add

# Listar conexiones
docker exec -it notorios-odoo-poller-orders-ms python -m src.main list

# Probar conexión Odoo + webhook
docker exec -it notorios-odoo-poller-orders-ms python -m src.main test 1

# Ver sync logs
docker exec -it notorios-odoo-poller-orders-ms python -m src.main logs -c 1 -n 50

# Ver retry queue
docker exec -it notorios-odoo-poller-orders-ms python -m src.main retries -c 1

# Re-enviar webhooks
docker exec -it notorios-odoo-poller-orders-ms python -m src.main send -c 1 --last 3

# Ver logs del contenedor
docker logs notorios-odoo-poller-orders-ms --tail 100 -f

# Reiniciar servicio
docker restart notorios-odoo-poller-orders-ms
```

## Arquitectura

### Capas

```
CLI (cli.py) → AppContext → Repositories → SQLite (aiosqlite, WAL mode)
                         → Scheduler → PollWorker → OdooClient (JSON-RPC)
                                                  → WebhookSender (HTTP POST)
```

**Scheduler** crea 1 `asyncio.Task` + 1 `httpx.AsyncClient` por conexión (patrón bulkhead). Error en una conexión no afecta a las demás.

**PollWorker.execute()** es 1 ciclo completo: check circuit breaker → auth Odoo → fetch órdenes → dedup via sent_orders → batch fetch datos relacionados → enviar webhooks → retry queue → sync_log.

**Primera sync (seed)**: Si `last_sync_at` es null, registra las últimas 30 órdenes en `sent_orders` SIN enviar webhooks, setea `last_sync_at`, y retorna. Los siguientes ciclos solo procesan órdenes con `write_date > last_sync_at`.

### Patrones clave

- **Idempotencia**: `sent_orders` con unique index `(connection_id, odoo_order_id, odoo_write_date)`. Se usa INSERT OR IGNORE.
- **Circuit breaker**: CLOSED→OPEN (5 fallos) →HALF_OPEN (120s) →CLOSED (2 éxitos). Estado persistido en tabla `connections`.
- **Retry con backoff**: 30s, 60s, 120s, 240s, 600s (máx 5 intentos). Payloads JSON guardados en `retry_queue`.
- **Encriptación transparente**: `ConnectionRepository` encrypt/decrypt `odoo_api_key` y `webhook_secret` con Fernet. El resto del código trabaja con valores en claro.
- **Batch N+1**: `fetch_batch_data()` recolecta IDs de partners, products, templates de todas las órdenes y hace 1 read por modelo. Lines indexadas en `batch._lines_by_order` (atributo dinámico).
- **Rotación sent_orders**: Máximo 30 registros por conexión (`trim_to_limit`), FIFO.

### Tablas SQLite

| Tabla | Propósito |
|---|---|
| `connections` | Configuración Odoo + estado circuit breaker + last_sync_at |
| `sent_orders` | Dedup de órdenes enviadas |
| `sync_logs` | Historial de cada ciclo de polling |
| `retry_queue` | Webhooks fallidos pendientes de reintento |

## Convenciones del código

- **Python 3.12+**, `from __future__ import annotations` en todos los archivos
- **Async everywhere**: aiosqlite, httpx.AsyncClient, asyncio
- **Dataclasses** para modelos, no ORMs
- **Sin TUI**: CLI con argparse puro, sin Textual
- **Montos directo**: Sin conversión ×100, se pasan tal cual de Odoo
- **Odoo JSON-RPC**: `execute_kw` con `args=[positional_args]` (lista envolvente, no *args). kwargs de `search_read` solo incluye `limit`/`order` si tienen valor truthy
- **SKU fallback**: `default_code` → `barcode` → template `default_code` → `ODOO-{db}-{product_id}`
- **Webhook headers**: `X-Webhook-Secret`, `X-Odoo-Connection-Id`
- **pytest-asyncio**: `asyncio_mode = "auto"` en pyproject.toml

## Variables de entorno

| Variable | Requerida | Default |
|---|---|---|
| `POLLER_ENCRYPTION_KEY` | Sí | - |
| `POLLER_DB_PATH` | No | `data/poller.db` |
| `POLLER_LOG_LEVEL` | No | `INFO` |
| `POLLER_DEFAULT_WEBHOOK_URL` | No | `""` |
