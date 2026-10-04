# 06. Коннекторы операторов

Интерфейс — `app/services/connectors/base.py`: коннектор наследует `Connector`: `available(agency)` и `search(agency, SearchQuery) -> ConnectorResult`.
Реализованы: `manual` (офферы из кабинета/CSV), `duffel` (авиа, sandbox-токен, экспериментальный), `open_data` (Open-Meteo погода, Auswärtiges Amt — рекомендации по странам, только чтение).

## Добавить оператора
1. Создать `app/services/connectors/<name>.py` с классом по образцу `duffel.py`; HTTP — через `httpx` с таймаутами; внешние URL — только заранее известные хосты.
2. Зарегистрировать в `connectors/__init__.py` и добавить провайдера в список интеграций (`dash.integrations`) — ключи сохраняются через `agency.set_secret`.
3. Привести ответ к формату ConnectorResult (название, направление, даты, цена, валюта, ссылка/оператор). Бот показывает только то, что вернул коннектор или хранится в офферах.
4. Тесты с моками HTTP (`tests/`).

## Кандидаты для начала (тест/открытый доступ)
Duffel (sandbox), Hotelbeds APItude (test), Amadeus Self-Service (test), Open-Meteo, Auswärtiges Amt, REST Countries, Unsplash (медиа, с атрибуцией).
