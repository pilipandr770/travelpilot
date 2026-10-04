"""Duffel (перелёты). ЭКСПЕРИМЕНТАЛЬНО: проверьте схему ответа по актуальной документации
https://duffel.com/docs/api — и тестируйте на тестовом токене (duffel_test_...)."""
import logging

import httpx

from .base import Connector, ConnectorResult, SearchQuery

log = logging.getLogger(__name__)
API = "https://api.duffel.com"


class DuffelConnector(Connector):
    name = "duffel"
    label = "Duffel (авиабилеты)"
    needs_secret = "duffel"

    def available(self, agency) -> bool:
        return "duffel" in (agency.enabled_connectors or []) and agency.has_secret("duffel")

    def search(self, agency, query: SearchQuery) -> ConnectorResult:
        if not (query.origin_iata and query.destination_iata and query.date_out):
            return ConnectorResult()
        token = agency.get_secret("duffel")
        slices = [{"origin": query.origin_iata.upper(), "destination": query.destination_iata.upper(),
                   "departure_date": query.date_out}]
        if query.date_back:
            slices.append({"origin": query.destination_iata.upper(), "destination": query.origin_iata.upper(),
                           "departure_date": query.date_back})
        body = {"data": {"slices": slices, "passengers": [{"type": "adult"}] * max(1, min(query.adults, 6)),
                         "cabin_class": "economy"}}
        headers = {"Authorization": f"Bearer {token}", "Duffel-Version": "v2",
                   "Accept": "application/json", "Content-Type": "application/json"}
        try:
            r = httpx.post(f"{API}/air/offer_requests", params={"return_offers": "true", "supplier_timeout": 10000},
                           json=body, headers=headers, timeout=25)
            r.raise_for_status()
            offers = (r.json().get("data") or {}).get("offers") or []
        except Exception as exc:  # сеть, 4xx/5xx, неожиданный JSON
            log.warning("duffel error: %s", exc)
            return ConnectorResult(error="Duffel временно недоступен")
        offers = sorted(offers, key=lambda o: float(o.get("total_amount") or 1e12))[: max(1, min(query.limit, 5))]
        out = []
        for o in offers:
            first_slice = (o.get("slices") or [{}])[0]
            out.append({
                "ref": f"duffel:{o.get('id')}",
                "title": f"Flug {query.origin_iata.upper()} → {query.destination_iata.upper()} ({(o.get('owner') or {}).get('name', '')})",
                "destination": query.destination_iata.upper(),
                "kind": "flight",
                "description": f"Dauer: {first_slice.get('duration', 'n/a')}; Segmente: {len(first_slice.get('segments') or [])}",
                "operator": (o.get("owner") or {}).get("name"),
                "price_from": float(o.get("total_amount") or 0) or None,
                "currency": o.get("total_currency"),
                "price_note": "Gesamtpreis laut Anbieter, Verfügbarkeit nicht garantiert",
                "valid_until": (o.get("expires_at") or "")[:10] or None,
                "source": "duffel",
            })
        return ConnectorResult(offers=out)
