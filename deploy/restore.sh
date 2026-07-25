#!/usr/bin/env bash
# Restore a gzipped pg_dump into the compose "db" container.
# Usage:  ./deploy/restore.sh backups/backup_2026-07-02_03-00-00.sql.gz
set -euo pipefail

FILE="${1:-}"
if [[ -z "$FILE" || ! -f "$FILE" ]]; then
    echo "Usage: $0 <backup_file.sql.gz>"; exit 1
fi

cd "$(dirname "$0")/.."

POSTGRES_USER="${POSTGRES_USER:-botuser}"
POSTGRES_DB="${POSTGRES_DB:-botdb}"

echo "⚠️  This will OVERWRITE database '$POSTGRES_DB'. Continue? [y/N]"
read -r CONFIRM
[[ "$CONFIRM" == "y" || "$CONFIRM" == "Y" ]] || { echo "Aborted."; exit 1; }

echo "Restoring $FILE …"
gunzip -c "$FILE" | docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"
echo "✅ Restore complete."
