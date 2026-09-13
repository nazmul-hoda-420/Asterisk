#!/bin/bash
# Daily PostgreSQL backup for the 'asterisk' database.
# Keeps a rolling 30-day window of dated backups; older ones are deleted.
# DB_PASSWORD comes from the project's .env (same file Django reads for
# SECRET_KEY/DEBUG) via the PGPASSWORD env var -- never as a pg_dump command-line
# argument, so it never shows up in `ps aux` for other users on this box.

set -euo pipefail

ENV_FILE="/var/www/asterisk/.env"
BACKUP_DIR="/var/backups/asterisk-db"
DB_NAME="asterisk"
DB_USER="asterisk"
DB_HOST="localhost"
DB_PORT="5432"
RETENTION_DAYS=30

# Pull just DB_PASSWORD out by line instead of sourcing the whole .env --
# other values in that file (e.g. SECRET_KEY) can contain shell-special
# characters that would break `source`.
DB_PASSWORD=$(grep -m1 '^DB_PASSWORD=' "$ENV_FILE" | cut -d '=' -f2-)
export PGPASSWORD="$DB_PASSWORD"

TIMESTAMP=$(date +%Y-%m-%d_%H%M)
OUT_FILE="${BACKUP_DIR}/asterisk_db_${TIMESTAMP}.dump"

pg_dump -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -Fc "$DB_NAME" -f "$OUT_FILE"

# Remove backups older than the retention window.
find "$BACKUP_DIR" -name "asterisk_db_*.dump" -mtime "+${RETENTION_DAYS}" -delete
