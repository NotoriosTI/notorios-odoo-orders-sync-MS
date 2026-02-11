## Como funciona internamente

### Estructura de archivos

```
src/
├── config.py              # Configuracion (lee env vars)
├── encryption.py          # Encriptacion Fernet para credenciales
├── main.py                # Entry point
├── cli.py                 # Todos los comandos CLI
├── db/
│   ├── models.py          # Estructuras de datos (dataclasses)
│   ├── database.py        # Creacion de tablas SQLite
│   └── repositories.py    # CRUD para cada tabla
├── odoo/
│   ├── client.py          # Comunicacion JSON-RPC con Odoo
│   └── mapper.py          # Transforma datos Odoo → payload webhook
└── poller/
    ├── circuit_breaker.py  # Proteccion contra fallos repetidos
    ├── sender.py           # Envio de webhooks a StockMaster
    ├── worker.py           # Un ciclo completo de polling
    └── scheduler.py        # Orquesta workers por conexion
```

### Capa 1: Base de datos (src/db/)

Hay 4 tablas en SQLite:

| Tabla | Para que |
|---|---|
| `connections` | Cada instancia Odoo configurada (URL, credenciales, intervalo) |
| `sent_orders` | Registro de ordenes ya enviadas (para no repetir) |
| `sync_logs` | Historial de cada ciclo de polling (cuantas encontro, envio, fallo) |
| `retry_queue` | Ordenes cuyo webhook fallo (para reintentar despues) |

Las credenciales (`api_key`, `webhook_secret`) se guardan encriptadas con Fernet. Cuando un repository lee una conexion, desencripta automaticamente.

### Capa 2: Cliente Odoo (src/odoo/)

**client.py** habla con Odoo via JSON-RPC:
- `authenticate()` → obtiene un `uid` (sesion)
- `search_read()` → busca registros con filtro (ej: ordenes confirmadas)
- `read()` → lee registros por ID (ej: datos del cliente, productos)
- Si la sesion expira, re-autentica automaticamente

**mapper.py** transforma datos crudos de Odoo al formato que espera StockMaster:
```
Orden Odoo + Partner + Lines + Products  →  Payload JSON normalizado
```
Optimizacion clave: en vez de hacer 1 request por cada producto/cliente (problema N+1), recolecta todos los IDs y hace lecturas batch (1 request para todos los partners, 1 para todos los productos, etc.)

Reglas del mapper:
- SKU: usa `default_code` del producto, si no tiene usa `barcode`, si no tiene usa el del template, si nada: `ODOO-{db}-{product_id}`
- Filtra items con cantidad 0
- Montos se pasan directo de Odoo sin conversion

### Capa 3: Motor de polling (src/poller/)

**circuit_breaker.py** - Protege contra fallos repetidos de una conexion:
```
CLOSED (normal) ──5 fallos──→ OPEN (bloqueado, no intenta)
                                    |
                               120 segundos
                                    |
                               HALF_OPEN (prueba 1 intento)
                                    |
                          2 exitos──→ CLOSED
                          1 fallo ──→ OPEN
```

**sender.py** - Envia el payload al webhook. Si falla, calcula cuando reintentar:
```
Intento 1: espera 30s
Intento 2: espera 60s
Intento 3: espera 120s
Intento 4: espera 240s
Intento 5+: espera 600s (maximo)
```

**worker.py** - Ejecuta un ciclo completo de polling para una conexion:
```
1. Circuit breaker permite? → Si no, skip
2. Autenticar en Odoo
3. Primera sync (seed)? → Registra ultimas 30 ordenes SIN enviar webhooks, setea last_sync_at, y retorna
4. Buscar ordenes con state=sale/done y write_date > ultimo sync
5. Filtrar las que ya se enviaron (idempotencia via sent_orders)
6. Fetch batch de datos relacionados (clientes, productos, lineas)
7. Por cada orden nueva:
   ├── Transformar a payload
   ├── Enviar webhook
   ├── OK → marcar en sent_orders
   └── Fallo → meter en retry_queue
8. Trim sent_orders a maximo 30 por conexion (rotacion FIFO)
9. Actualizar last_sync_at
10. Procesar retry_queue pendientes
11. Registrar sync_log
12. Actualizar circuit breaker (exito/fallo)
```

**scheduler.py** - Orquesta todo. Crea un asyncio.Task independiente por conexion:
```
Conexion "Tienda A"  →  Task A  →  [Worker → sleep 60s → Worker → sleep 60s → ...]
Conexion "Tienda B"  →  Task B  →  [Worker → sleep 30s → Worker → sleep 30s → ...]
```
Cada conexion tiene su propio HTTP client. Si una falla, las demás siguen funcionando (patron bulkhead).

### Flujo completo

```
┌─────────┐     JSON-RPC      ┌──────────┐
│  Odoo   │ ←──────────────── │  Worker   │
│  SaaS   │ ──ordenes──────→  │  (poll)   │
└─────────┘                   └─────┬─────┘
                                    │ payload
                                    v
                              ┌───────────┐     HTTP POST     ┌─────────────┐
                              │  Sender   │ ────────────────→ │ StockMaster │
                              └─────┬─────┘                   │  (webhook)  │
                                    │                         └─────────────┘
                               fallo?
                                    │
                              ┌─────v─────┐
                              │  Retry    │  (reintenta con backoff)
                              │  Queue    │
                              └───────────┘
```

### Aislamiento de errores

| Error | Impacto | Que pasa |
|---|---|---|
| Odoo no responde / timeout | Solo 1 conexion | Log, circuit breaker cuenta fallo, skip ciclo |
| HTTP 429 de Odoo | Solo 1 conexion | Log, espera al siguiente ciclo |
| Webhook falla | Solo 1 orden | Va a retry_queue, continua con la siguiente orden |
| Circuit breaker OPEN | Solo 1 conexion | Skip ciclo entero hasta que pase el recovery timeout |
| Excepcion inesperada | Solo 1 conexion | Catch en el task, circuit breaker cuenta fallo |