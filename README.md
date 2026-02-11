# Odoo Order Poller

Microservicio que hace polling a instancias Odoo SaaS via JSON-RPC, extrae ordenes de venta confirmadas y las envia como webhooks normalizados a StockMaster.

## Setup

### 1. Configurar variables de entorno

```bash
cp .env.example .env
```

Generar la clave de encriptacion:

```bash
docker run --rm python:3.12-slim python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Editar `.env` y pegar la clave en `POLLER_ENCRYPTION_KEY`. Opcionalmente configurar `POLLER_DEFAULT_WEBHOOK_URL`.

### 2. Build

```bash
docker compose build
```

## Uso

### Agregar una conexion Odoo

```bash
docker compose run --rm poller python -m src.main add
```

Te pedira interactivamente: nombre, URL Odoo, base de datos, usuario, API key, webhook URL, intervalo de polling, etc.

### Listar conexiones

```bash
docker compose run --rm poller python -m src.main list
```

### Editar una conexion

```bash
docker compose run --rm poller python -m src.main edit 1
```

### Eliminar una conexion

```bash
docker compose run --rm poller python -m src.main delete 1
```

### Probar conexion (Odoo + Webhook)

```bash
docker compose run --rm poller python -m src.main test 1
```

### Iniciar el polling

```bash
docker compose up -d
```

Para ver logs en tiempo real:

```bash
docker compose logs -f
```

Para detener:

```bash
docker compose down
```

### Ver logs de sincronizacion

```bash
# Todos los logs (ultimos 20)
docker compose run --rm poller python -m src.main logs

# Filtrar por conexion y cantidad
docker compose run --rm poller python -m src.main logs -c 1 -n 50
```

### Ver cola de reintentos

```bash
docker compose run --rm poller python -m src.main retries

# Filtrar por conexion
docker compose run --rm poller python -m src.main retries -c 1
```

### Reintentar un item fallido

```bash
docker compose run --rm poller python -m src.main retry 3
```

### Descartar un retry

```bash
docker compose run --rm poller python -m src.main discard 3
```

### Enviar webhooks manualmente

Envia webhooks de ordenes ya registradas en `sent_orders`. Util para re-enviar ordenes especificas o enviar las que se registraron durante el seed inicial (primera sincronizacion).

```bash
# Enviar las ultimas 3 ordenes automaticamente (no-interactivo, compatible con Docker)
docker compose run --rm poller python -m src.main send -c 1 --last 3

# Modo interactivo: muestra tabla y pide seleccionar por indice
docker compose run --rm poller python -m src.main send -c 1
```

### Resetear circuit breaker

Cuando una conexion tiene el circuit breaker abierto (muchos fallos consecutivos), se puede resetear manualmente:

```bash
docker compose run --rm poller python -m src.main reset-circuit 1
```
