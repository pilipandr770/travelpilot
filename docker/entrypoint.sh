#!/bin/sh
set -e
case "$1" in
  web)
    # Миграции применяет только web (один раз при старте). Воркер ждёт готовности БД.
    flask --app wsgi db upgrade
    exec gunicorn wsgi:app --bind 0.0.0.0:8000 --workers "${WEB_CONCURRENCY:-3}" \
         --access-logfile - --forwarded-allow-ips="*"
    ;;
  worker)
    exec python -m worker.run
    ;;
  *)
    exec "$@"
    ;;
esac
