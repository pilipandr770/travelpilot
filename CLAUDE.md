# TravelPilot — контекст для Claude Code

Flask 3.1 (app factory), SQLAlchemy 2 + PostgreSQL 16, Alembic (Flask-Migrate), gunicorn, Docker. Язык общения с владельцем — русский; клиентские материалы — немецкий.

## Команды
- Тесты: `python -m pytest -q tests` (SQLite) / с `TEST_DATABASE_URL=postgresql://...`
- Миграции: `flask --app wsgi db migrate -m "..."` → проверить файл → `flask --app wsgi db upgrade`
- Ключи: `flask --app wsgi gen-keys`; админ: `flask --app wsgi create-admin EMAIL`
- Воркер: `python -m worker.run`

## Инварианты (не нарушать)
1. Каждая таблица с данными агентства имеет `agency_id`; запросы только через `scoped()` / `own()` (dash) — иначе утечка между тенантами.
2. Секреты агентов только через `agency.set_secret/get_secret` (Fernet, привязка к agency_id+provider); после сохранения не показываются (только last4). Никогда не логировать.
3. Stripe: только restricted-ключи (`sk_live_` отклоняется); ссылку на оплату создаёт человек; пакетные туры — только при `sicherungsschein_confirmed`.
4. Агент = Vermittler. Чат-бот не подтверждает бронь, не называет цену, которой нет в оффере, не обещает «лучшую цену». Документы ≠ замена подтверждения/счёта/Sicherungsschein оператора.
5. LLM-контент: блог по умолчанию черновик + пометка «KI-generiert»; RSS — только тема, не копирование. Весь HTML через `render_markdown` (nh3).
6. Исходящие запросы к URL пользователей — только `safe_fetch` (защита от SSRF).
7. Тексты UI: через `t()`; ключи лендинга/сайта — dotted (`app/i18n_data.py`), кабинет — русская строка как ключ.
8. CSP строгая (`script-src 'self'`): без inline-скриптов и onclick.

## Структура
`app/blueprints/{platform,auth,dash,site,admin}.py`, `app/services/*` (chat, blog, llm, stripe_service, documents, connectors/), `app/tenancy.py` (хост → тенант), `app/models.py`, `worker/run.py`, `tests/`.
Подробно: docs/01-ARCHITECTURE.md. План работ: docs/02-ROADMAP.md.
