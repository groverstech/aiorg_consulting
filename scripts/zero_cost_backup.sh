#!/bin/sh
# Nightly backup for the zero-cost (Docker Compose) setup: a compressed pg_dump in ./backups.
# Keeps the newest 14. Schedule it with cron (Linux/macOS) or Task Scheduler (Windows/WSL).
set -eu
cd "$(dirname "$0")/.."
mkdir -p backups
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
docker compose -f deploy/docker-compose.yml exec -T db pg_dump -U wwos -Fc wwos > "backups/wwos-$STAMP.dump"
ls -1t backups/wwos-*.dump | tail -n +15 | xargs -r rm --
echo "backup written: backups/wwos-$STAMP.dump"
