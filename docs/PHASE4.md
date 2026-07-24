# Phase 4 — Production Infrastructure

This phase adds **Docker**, **PostgreSQL**, and optional **Telegram webhook mode**
so the bot can run reliably in production instead of only via `python bot.py`.

---

## 1. What's new

| File | Purpose |
|------|---------|
| `Dockerfile` | Slim Python 3.11 image, non-root user, `tini` PID 1 |
| `docker-compose.yml` | Bot + PostgreSQL 16 + optional CryptoBot webhook receiver |
| `.dockerignore` | Keeps builds small |
| `.env.example` | All configurable env vars in one place |
| `deploy/nginx.conf.example` | Reverse-proxy template for webhook mode |
| `config/settings.py` | New `RUN_MODE`, `WEBHOOK_*` settings |
| `bot.py` | Supports `run_polling()` **or** `run_webhook()` |

---

## 2. Quick start (Docker + PostgreSQL)

```bash
cd telegram-bot
cp .env.example .env
# edit .env: set BOT_TOKEN, ADMIN_TELEGRAM_ID, POSTGRES_PASSWORD

docker compose up -d --build
docker compose logs -f bot
```

The compose file auto-injects
`DATABASE_URL=postgresql+psycopg2://botuser:...@db:5432/botdb`
so the bot connects to the Postgres container.

**Run migrations inside the container:**

```bash
docker compose exec bot python -m migrations.v2_add_referral_support_i18n
docker compose exec bot python -m migrations.v3_add_manual_payments
docker compose exec bot python -m migrations.v4_phase2
docker compose exec bot python -m migrations.v5_phase3
```

Or use Alembic (already scaffolded):

```bash
docker compose exec bot alembic upgrade head
```

---

## 3. Polling vs Webhook mode

**Polling (default)** — no public URL needed, easiest for VPS:

```
RUN_MODE=polling
```

**Webhook** — lower latency, no long-poll connection, needs HTTPS:

```
RUN_MODE=webhook
WEBHOOK_URL=https://bot.example.com
WEBHOOK_PATH=/telegram
WEBHOOK_PORT=8443
WEBHOOK_SECRET=some-long-random-string
```

Point nginx / caddy / traefik at `127.0.0.1:8443` (see `deploy/nginx.conf.example`).
Telegram POSTs updates to `https://bot.example.com/telegram`; PTB validates the
`X-Telegram-Bot-Api-Secret-Token` header against `WEBHOOK_SECRET` and rejects fakes.

---

## 4. Switching an existing SQLite install to PostgreSQL

1. Export data from SQLite:
   ```bash
   sqlite3 bot_database.db .dump > dump.sql
   ```
2. Clean it up (drop `PRAGMA`, `BEGIN TRANSACTION`, adjust `AUTOINCREMENT` →
   `SERIAL`) then load into Postgres, **or** simply start fresh:
   ```bash
   docker compose exec bot alembic upgrade head
   docker compose exec bot python -c "from database.init_data import initialize_database; initialize_database()"
   ```
3. Update `.env` — `DATABASE_URL=postgresql+psycopg2://...`

---

## 5. Backups

```bash
# Manual dump
docker compose exec db pg_dump -U botuser botdb | gzip > backup_$(date +%F).sql.gz

# Restore
gunzip -c backup_2026-07-02.sql.gz | docker compose exec -T db psql -U botuser -d botdb
```

Add a nightly cron on the host that runs the dump command and rotates old files.

---

## 6. Optional CryptoBot webhook receiver

The existing `webhook_server.py` (Flask, port 5000) is packaged as a compose
service under the `crypto` profile:

```bash
docker compose --profile crypto up -d
```

Then in @CryptoBot set the webhook to `https://your-domain.com/webhook/cryptobot`.

---

## 7. Health / observability

- Bot logs → `./logs/` (mounted volume, rotating files from `utils/logging_config.py`)
- Postgres data → `pgdata` named volume (survives `docker compose down`)
- To wipe everything and start over: `docker compose down -v`

Ready for production. ✅
