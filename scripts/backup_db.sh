#!/usr/bin/env bash
# Ежедневный бэкап SQLite-базы CallReview: локально + Google Диск (rclone).
# Запускается systemd-таймером call-eval-backup.timer (см. deploy/).
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DB="$APP_DIR/data/app.db"
LOCAL_DIR="$APP_DIR/data/backups"
REMOTE="${BACKUP_REMOTE:-gdrive:CallReview-backups}"
LOCAL_KEEP=14        # сколько последних копий держать на сервере
REMOTE_KEEP_DAYS=60  # сколько дней хранить копии на Google Диске

mkdir -p "$LOCAL_DIR"
# статус для баннера в приложении (app/backup_status.py):
# backup_ok — время последней успешной выгрузки, backup_error — причина последнего сбоя
STEP="старт"
trap 'rc=$?; [ $rc -eq 0 ] || echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) ошибка на этапе «$STEP» (код $rc)" > "$LOCAL_DIR/backup_error"' EXIT

STEP="проверка базы"
[ -s "$DB" ] || { echo "нет базы: $DB" >&2; exit 1; }
STAMP="$(date -u +%Y-%m-%d_%H%M%S)"
TMP="$LOCAL_DIR/app-$STAMP.db"

STEP="снимок базы"
# .backup — консистентный снимок даже при одновременной записи приложением
sqlite3 "$DB" ".backup '$TMP'"
sqlite3 "$TMP" "PRAGMA integrity_check;" | grep -qx ok
gzip -9 "$TMP"
FILE="$TMP.gz"
echo "local: $FILE ($(du -h "$FILE" | cut -f1))"

# локальная ротация
ls -1t "$LOCAL_DIR"/app-*.db.gz | tail -n +$((LOCAL_KEEP + 1)) | xargs -r rm -f

# выгрузка + ротация на Google Диске
STEP="выгрузка на Google Диск"
rclone copy "$FILE" "$REMOTE"
STEP="очистка старых копий на Диске"
rclone delete "$REMOTE" --min-age "${REMOTE_KEEP_DAYS}d" --include "app-*.db.gz"
echo "remote: uploaded to $REMOTE"
date -u +%Y-%m-%dT%H:%M:%SZ > "$LOCAL_DIR/backup_ok"
rm -f "$LOCAL_DIR/backup_error"
