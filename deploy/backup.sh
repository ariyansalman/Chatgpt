#!/usr/bin/env bash
# ─── Phase 4+: Automated PostgreSQL backup ─────────────────
# Runs pg_dump inside the compose "db" container, gzips the output,
# writes to ./backups/, and prunes files older than $RETENTION_DAYS.
#
# Add to host crontab (daily 03:00):
#   0 3 * * * cd /opt/telegram-bot && ./deploy/backup.sh >> logs/backup.log 2>&1

set -euo pipefail

cd "$(dirname "$0")/.."

BACKUP_DIR="${BACKUP_DIR:-./backups}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
POSTGRES_USER="${POSTGRES_USER:-botuser}"
POSTGRES_DB="${POSTGRES_DB:-botdb}"

mkdir -p "$BACKUP_DIR"

STAMP="$(date +%Y-%m-%d_%H-%M-%S)"
OUT="$BACKUP_DIR/backup_${STAMP}.sql.gz"

echo "[$(date -Iseconds)] Starting backup → $OUT"

docker compose exec -T db pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    | gzip -9 > "$OUT"

SIZE=$(du -h "$OUT" | cut -f1)
echo "[$(date -Iseconds)] Backup done ($SIZE)"

# Prune old backups
find "$BACKUP_DIR" -type f -name 'backup_*.sql.gz' -mtime +${RETENTION_DAYS} -print -delete

# Optional: upload to S3 / rsync elsewhere
# aws s3 cp "$OUT" "s3://$S3_BUCKET/telegram-bot/"

echo "[$(date -Iseconds)] Backup script finished"
