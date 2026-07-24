#!/usr/bin/env bash
# Manual pg_dump wrapper for VPS operators.
# Usage: DATABASE_URL=postgres://... BACKUP_DIR=/var/backups/tsb ./scripts/pg_backup.sh
set -euo pipefail

: "${DATABASE_URL:?DATABASE_URL must be set}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/telegram-store}"
mkdir -p "$BACKUP_DIR"
TS=$(date -u +%Y%m%d_%H%M%S)
OUT="$BACKUP_DIR/pgdump_${TS}.sql.gz"
echo "Writing $OUT ..."
pg_dump --format=plain --no-owner --no-privileges "$DATABASE_URL" | gzip -9 > "$OUT"
echo "OK: $(du -h "$OUT" | cut -f1)  $OUT"
