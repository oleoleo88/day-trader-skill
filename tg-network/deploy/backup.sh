#!/usr/bin/env bash
# Ежедневный бэкап базы (cron: 15 4 * * * /opt/tgnet/deploy/backup.sh)
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p backups
sqlite3 data/tgnet.sqlite3 ".backup 'backups/tgnet-$(date +%F).sqlite3'"
find backups -name 'tgnet-*.sqlite3' -mtime +14 -delete
