"""Воркер: по расписанию создаёт статьи блога для агентств (ключами самих агентств).

Запуск: python -m worker.run   (в docker-compose — сервис `worker`).
При нескольких репликах используется advisory lock Postgres, чтобы задания не дублировались.
"""
import logging
import os
import time

from app import create_app
from app.extensions import db
from app.services.blog import run_due_jobs

log = logging.getLogger("worker")
INTERVAL = int(os.environ.get("WORKER_INTERVAL_SECONDS", "300"))
LOCK_ID = 727001


def tick(app):
    with app.app_context():
        locked = True
        if db.engine.dialect.name == "postgresql":
            locked = db.session.execute(db.text("SELECT pg_try_advisory_lock(:id)"), {"id": LOCK_ID}).scalar()
        if not locked:
            return 0
        try:
            return run_due_jobs(app)
        finally:
            if db.engine.dialect.name == "postgresql":
                db.session.execute(db.text("SELECT pg_advisory_unlock(:id)"), {"id": LOCK_ID})
                db.session.commit()
            db.session.remove()


def main():
    app = create_app()
    log.info("worker started, interval=%ss", INTERVAL)
    while True:
        try:
            n = tick(app)
            if n:
                log.info("created %s posts", n)
        except Exception:  # noqa: BLE001
            log.exception("worker tick failed")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
