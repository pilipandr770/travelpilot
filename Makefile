.PHONY: up down logs keys admin test shell migrate prod
up:      ; docker compose up --build -d
down:    ; docker compose down
logs:    ; docker compose logs -f web worker
keys:    ; docker compose run --rm web flask --app wsgi gen-keys
admin:   ; docker compose run --rm web flask --app wsgi create-admin $(EMAIL)
migrate: ; docker compose run --rm web flask --app wsgi db upgrade
test:    ; python -m pytest -q tests
shell:   ; docker compose exec web sh
prod:    ; docker compose -f docker-compose.prod.yml --env-file .env up -d --build
