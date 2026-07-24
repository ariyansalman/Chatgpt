# Phase 4+ — Backups, Monitoring, CI/CD

Extends Phase 4 with automated backups, a Prometheus/Grafana monitoring stack,
and GitHub Actions pipelines for CI + auto-deploy.

---

## 1. Automated backups

Scripts: `deploy/backup.sh` (dump + prune) and `deploy/restore.sh`.

**Enable nightly backups on the VPS:**

```bash
chmod +x deploy/backup.sh deploy/restore.sh
crontab -e
# add:
0 3 * * * cd /opt/telegram-bot && ./deploy/backup.sh >> logs/backup.log 2>&1
```

- Dumps land in `./backups/backup_YYYY-MM-DD_HH-MM-SS.sql.gz`
- Retention: `RETENTION_DAYS=14` (edit in the script or export in env)
- Optional S3 upload — uncomment the `aws s3 cp` line in `backup.sh`

**Restore:**
```bash
./deploy/restore.sh backups/backup_2026-07-02_03-00-00.sql.gz
```

---

## 2. Monitoring (Prometheus + Grafana)

Files:
- `monitoring/exporter.py` — custom Python `/metrics` exporter (bot KPIs)
- `monitoring/prometheus.yml` — scrape config
- `monitoring/grafana/…` — auto-provisioned datasource + dashboard
- `docker-compose.monitoring.yml` — overlay with 4 extra services

**Start the full stack:**
```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.monitoring.yml \
  up -d --build
```

**Access:**
- Grafana: `http://<server>:3000` — login `admin` / `admin` (change immediately, or set `GRAFANA_USER` / `GRAFANA_PASSWORD` in `.env`)
- Prometheus: `http://<server>:9090`
- Raw metrics: `http://<server>:9090/api/v1/targets` (should show 3 targets UP)

**Metrics exposed by the bot exporter:**
| Metric | Meaning |
|---|---|
| `tgbot_users_total` | Registered users |
| `tgbot_users_banned` | Currently banned |
| `tgbot_orders_total{status}` | Orders grouped by status |
| `tgbot_revenue_usd_total` | Completed-order revenue |
| `tgbot_wallet_balance_usd_total` | Sum of wallet balances |
| `tgbot_pending_transactions` | Pending top-ups |
| `tgbot_products_active` | Active catalog products |
| `tgbot_low_stock_products` | Products with ≤5 keys left |

Postgres internals (connections, cache hit, size) come from `postgres_exporter`.

The provisioned dashboard "Telegram Store Bot" appears automatically in Grafana → Dashboards.

**Recommended alerts to add later (Grafana → Alerting):**
- `tgbot_low_stock_products > 0` — restock reminder
- `tgbot_pending_transactions > 20` — payment backlog
- `up{job="telegram-bot"} == 0` — exporter/bot down

---

## 3. CI/CD (GitHub Actions)

Two workflows live under `.github/workflows/`:

### `ci.yml` — runs on every push / PR
1. Installs deps, runs `ruff` lint (warn-only)
2. `compileall` on the whole bot
3. Builds the Docker image using GHA cache — catches broken Dockerfiles before deploy

### `deploy.yml` — runs on push to `main` (or manual dispatch)
1. `rsync` project to VPS (skips `.env`, `assets`, `uploads`, `backups`, `logs` — those stay server-side)
2. SSH-executes `docker compose up -d --build`
3. Runs `alembic upgrade head` inside the bot container

**One-time GitHub setup — Repo → Settings → Secrets and variables → Actions:**

| Secret | Value |
|---|---|
| `VPS_HOST` | server IP or hostname |
| `VPS_USER` | SSH user (`root` or a `deploy` user with docker access) |
| `VPS_SSH_KEY` | contents of a private key whose public part is in `~/.ssh/authorized_keys` on the VPS |
| `VPS_PROJECT_PATH` | e.g. `/opt/telegram-bot` |

**One-time VPS setup:**
```bash
sudo mkdir -p /opt/telegram-bot && sudo chown $USER /opt/telegram-bot
cd /opt/telegram-bot
# copy your .env once (never in git)
scp local/.env user@vps:/opt/telegram-bot/.env
# first manual bootstrap so docker/compose exist and image builds
git clone <repo> tmp && cp -r tmp/telegram-bot/. . && rm -rf tmp
docker compose up -d --build
```

After that, every `git push` to `main` auto-deploys.

---

## 4. Combined operational cheatsheet

```bash
# View everything
docker compose -f docker-compose.yml -f docker-compose.monitoring.yml ps

# Bot logs
docker compose logs -f bot

# Manual backup now
./deploy/backup.sh

# Restore
./deploy/restore.sh backups/backup_<STAMP>.sql.gz

# Update after code change (manual, if not using GitHub Actions)
docker compose up -d --build
docker compose exec bot alembic upgrade head
```

Full production stack ✅ — bot, database, backups, dashboards, and auto-deploy.
