#!/bin/sh
# Щоденний бекап БД (cron: 15 3 * * * /opt/kadastr/deploy/backup.sh)
set -e
cd "$(dirname "$0")/.."
mkdir -p backups
sqlite3 data/app.db ".backup backups/app-$(date +%F).db"
find backups -name 'app-*.db' -mtime +14 -delete
