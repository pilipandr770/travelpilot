#!/bin/sh
# Использование: ./scripts/backup.sh  (из корня проекта, прод-compose)
set -e
mkdir -p backups
docker compose -f docker-compose.prod.yml exec -T db pg_dump -U travelpilot travelpilot | gzip > "backups/tp-$(date +%F-%H%M).sql.gz"
