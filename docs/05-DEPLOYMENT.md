# 05. Деплой

## Требования
VPS (2 vCPU/4 GB достаточно для старта), Docker + compose plugin, домен с возможностью wildcard A-записи.

## Шаги
1. DNS: `A example.com → IP`, `A *.example.com → IP`, `A sites.example.com → IP` (CNAME-цель для своих доменов агентов).
2. Сервер: `git clone`/распаковать zip, `cp .env.example .env`, заполнить:
   `APP_ENV=production`, `BASE_DOMAIN=example.com`, `URL_SCHEME=https`, `PUBLIC_PORT=` (пусто), `CUSTOM_DOMAIN_TARGET=sites.example.com`,
   `POSTGRES_PASSWORD`, `ACME_EMAIL`, SMTP, `PLATFORM_OPERATOR_*`; ключи — `docker compose -f docker-compose.prod.yml run --rm web flask --app wsgi gen-keys`.
3. `make prod` (или `docker compose -f docker-compose.prod.yml --env-file .env up -d --build`). Миграции применяются при старте `web`.
4. `docker compose -f docker-compose.prod.yml exec web flask --app wsgi create-admin you@example.com`.
5. Проверить: `https://example.com/_internal/health`, регистрация, письмо, публикация, поддомен.

## TLS
Caddy выпускает сертификаты по запросу (`on_demand_tls`) для apex, поддоменов и кастомных доменов; разрешение — `/_internal/tls-ask`.
Лимиты Let's Encrypt: для массовых поддоменов рассмотрите wildcard-сертификат через DNS-challenge (кастомный образ Caddy с плагином вашего DNS-провайдера).

## Кастомный домен агента
Кабинет → «Домен»: агент добавляет домен, ставит TXT `_travelpilot-verify.<домен>` и CNAME на `CUSTOM_DOMAIN_TARGET`, жмёт «Проверить».

## Бэкапы
`docker compose exec db pg_dump -U travelpilot travelpilot | gzip > backup.sql.gz` (cron, хранить вне сервера, шифровать) + volume `uploads`.
Хранить копию `ENCRYPTION_KEYS` отдельно: без неё сохранённые ключи агентов не расшифровать.

## Обновление
`git pull && make prod` (миграции автоматически). Откат: `flask --app wsgi db downgrade -1` + прежний образ.

## Другие хостинги
Образ универсален (Fly.io, Railway, Hetzner, k8s): нужны Postgres, переменные из .env, TLS-прокси с on-demand сертификатами и `TRUSTED_PROXY_COUNT`=числу прокси перед приложением; для >1 реплики web — Redis в `RATELIMIT_STORAGE_URI` и общее хранилище для uploads.
