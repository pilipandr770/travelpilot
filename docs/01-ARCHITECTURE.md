# 01. Архитектура

## Стек
Flask 3.1 (app factory) · SQLAlchemy 2 / Flask-Migrate · PostgreSQL 16 · gunicorn · Caddy (TLS) · Redis (лимиты в проде) ·
отдельный воркер блога · Anthropic SDK · Stripe SDK · reportlab (PDF) · nh3 (санитайзер) · Pillow.

## Мультитенантность по хосту (`app/tenancy.py`)
- apex `BASE_DOMAIN` (+`PLATFORM_EXTRA_HOSTS`) → платформа (лендинг, auth, `/app`, `/admin`).
- `<slug>.BASE_DOMAIN` → публичный сайт агентства.
- подтверждённый кастомный домен (DNS TXT `_travelpilot-verify.<domain>` + CNAME на `CUSTOM_DOMAIN_TARGET`) → сайт агентства.
- Caddy `on_demand_tls` спрашивает `/_internal/tls-ask?domain=…&token=INTERNAL_TOKEN` — сертификат выдаётся только для известных доменов.
- Зарезервированные slug (admin, www, api, app…) нельзя занять.

## Блюпринты
| Блюпринт | Хост | Назначение |
|---|---|---|
| platform | apex | лендинг, /legal/*, health, tls-ask |
| auth | apex | register/login/logout/confirm/forgot/reset |
| dash | apex | кабинет агента `/app/...` (бренд, бот, интеграции, офферы, лиды, брони, блог, отзывы, статистика, домен) |
| site | тенант | публичный сайт, чат-API, контакт, отзывы, webhook Stripe, theme.css, sitemap, rss, robots |
| admin | apex | одобрение агентств, статусы (sandbox → pending_review → active → suspended) |

## Сервисы (`app/services`)
chat (системный промпт, tools: search_offers / destination_info / save_lead) · blog (generate_post через forced tool `submit_post`,
pick_topic, publish_post, crosspost, run_due_jobs) · llm (обёртка Anthropic, модели из allowlist) · stripe_service ·
documents (PDF de/en/ru/uk) · guide · offer_import · telegram · vat (VIES) · connectors/ (manual, duffel, open_data).

## Данные (`app/models.py`)
User, Agency (юр./бренд/бот/блог/домен), AgencySecret (шифр.), Lead, ChatSession/Message, Offer, Booking, Document,
BlogPost, FeedSource, CrosspostChannel, Review, SiteEvent, UsageEvent, AuditLog. Все, кроме User/Agency, содержат `agency_id`.

## Секреты (BYOK)
`MultiFernet` из `ENCRYPTION_KEYS` (первый шифрует, остальные — ротация); контекст (agency_id, provider) входит в
шифруемые данные → перенос шифртекста между агентствами не расшифруется. UI показывает только last4.

## Воркер
`python -m worker.run`, цикл каждые `WORKER_INTERVAL_SECONDS`, pg advisory lock 727001 → безопасно несколько реплик.

## Поток чата
Origin-check → лимиты (IP/сессия/агентство в сутки) → история из БД → LLM ключом агента → tool-calls → ответ как `textContent`.
