# TravelPilot — SaaS-платформа автоматизации для турагентов

Мультитенантная подписочная платформа для турагентов-посредников (Reisevermittler). Каждый агент получает:
фирменный сайт на поддомене `<slug>.BASE_DOMAIN` (позже — свой домен), чат-бота, автоблог с SEO и кросспостингом,
CRM заявок, офферы (вручную/через API операторов), оплату через собственный Stripe агента, PDF-документы и гайд
для клиента на его языке. Ключи (Anthropic, Stripe, операторы) агент вносит **свои** (BYOK).

Подробно о цели → [docs/00-PROJECT-GOAL.md](docs/00-PROJECT-GOAL.md).

## Быстрый старт (локально, Docker)

```bash
cp .env.example .env
docker compose run --rm web flask --app wsgi gen-keys   # вставьте вывод в .env (SECRET_KEY, ENCRYPTION_KEYS, INTERNAL_TOKEN)
docker compose up --build -d
make admin EMAIL=you@example.com                          # суперадмин (одобряет агентства)
```

- Платформа: http://localhost:8000  (лендинг, регистрация, `/app/` — кабинет, `/admin/` — суперадмин)
- Сайт агентства: `http://<slug>.localhost:8000` (`*.localhost` браузеры резолвят сами)
- Письма без SMTP пишутся в лог: `make logs` (там ссылка подтверждения e-mail)

Первый прогон: регистрация → подтвердить e-mail по ссылке из лога → заполнить «Юр. данные» → «Запросить проверку» →
в `/admin/` одобрить → «Опубликовать сайт».

## Без Docker

```bash
python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
cp .env.example .env            # DATABASE_URL можно не задавать — будет SQLite dev.db
flask --app wsgi db upgrade && flask --app wsgi run --port 8000
python -m worker.run            # воркер блога (отдельный процесс)
```

## Тесты

```bash
python -m pytest -q tests                                            # SQLite
TEST_DATABASE_URL=postgresql://user@host/db python -m pytest -q tests  # Postgres
```
64 теста: авторизация, изоляция тенантов, шифрование ключей, публикация, чат (LLM замокан), блог, Stripe, PDF, SSRF, XSS/SQLi, CSRF, админка.

## Прод

См. [docs/05-DEPLOYMENT.md](docs/05-DEPLOYMENT.md): `make prod` (Caddy + wildcard/on-demand TLS, Postgres, Redis, worker).

## Документация

| Файл | Содержание |
|---|---|
| docs/00-PROJECT-GOAL.md | Цель, продукт, бизнес-модель, правовая рамка |
| docs/01-ARCHITECTURE.md | Структура кода, мультитенантность, модель данных |
| docs/02-ROADMAP.md | Пошаговая реализация и план развития |
| docs/03-SECURITY.md | Текущая защита, план 2FA, чек-лист тестов на инъекции |
| docs/04-LEGAL-DE.md | Vermittler vs Veranstalter, Sicherungsschein, DSGVO, AI Act |
| docs/05-DEPLOYMENT.md | Деплой, домены, TLS, бэкапы |
| docs/06-CONNECTORS.md | Как добавить API оператора |
| docs/07-TESTING.md | Тесты и ручной сценарий |
| CLAUDE.md | Контекст для Claude Code |

## Известные ограничения

- 2FA не реализована (поля `User.totp_*` зарезервированы) — см. docs/03-SECURITY.md.
- Кабинет переведён на немецкий частично (источник — русский), лендинг/сайты: de/ru/en(/uk).
- Коннектор Duffel экспериментальный, не проверен на живом API. Остальные открытые API (Open-Meteo, Auswärtiges Amt) — только чтение.
- Юридические тексты (Impressum/AGB/Datenschutz/AVV) — шаблоны, **требуют проверки юристом** до запуска.
- Кросспостинг: реализован Telegram; X и WhatsApp — в плане.
