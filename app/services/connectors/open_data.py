"""Бесплатные открытые данные: погода/климат (Open-Meteo) и предупреждения МИД Германии."""
import logging
import time
from datetime import date

import httpx

log = logging.getLogger(__name__)
_CACHE: dict[str, tuple[float, object]] = {}


def _cached(key: str, ttl: int, fn):
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    value = fn()
    _CACHE[key] = (now, value)
    return value


def geocode(place: str) -> dict | None:
    def _do():
        r = httpx.get("https://geocoding-api.open-meteo.com/v1/search",
                      params={"name": place, "count": 1, "language": "de"}, timeout=10)
        r.raise_for_status()
        results = r.json().get("results") or []
        return results[0] if results else None
    try:
        return _cached(f"geo:{place.lower()}", 86400, _do)
    except Exception as exc:
        log.warning("geocode failed: %s", exc)
        return None


def climate_summary(lat: float, lon: float, month: int) -> dict | None:
    """Средние значения за прошлый год для выбранного месяца (Open-Meteo archive)."""
    year = date.today().year - 1
    start = date(year, month, 1)
    end = date(year + (month == 12), (month % 12) + 1, 1)
    end = date.fromordinal(end.toordinal() - 1)

    def _do():
        r = httpx.get("https://archive-api.open-meteo.com/v1/archive", params={
            "latitude": lat, "longitude": lon, "start_date": start.isoformat(), "end_date": end.isoformat(),
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum", "timezone": "auto"}, timeout=15)
        r.raise_for_status()
        d = r.json().get("daily") or {}
        tmax = [x for x in d.get("temperature_2m_max", []) if x is not None]
        tmin = [x for x in d.get("temperature_2m_min", []) if x is not None]
        rain = [x for x in d.get("precipitation_sum", []) if x is not None]
        if not tmax:
            return None
        return {
            "avg_high_c": round(sum(tmax) / len(tmax), 1),
            "avg_low_c": round(sum(tmin) / len(tmin), 1) if tmin else None,
            "rainy_days": sum(1 for x in rain if x >= 1.0),
            "based_on": f"{start.isoformat()}..{end.isoformat()} (Open-Meteo archive)",
        }
    try:
        return _cached(f"clim:{lat:.2f}:{lon:.2f}:{month}", 86400 * 7, _do)
    except Exception as exc:
        log.warning("climate failed: %s", exc)
        return None


def travel_warning(country_code: str) -> dict | None:
    """Reise- und Sicherheitshinweise des Auswärtigen Amts (Open Data). Формат ответа парсится терпимо."""
    def _do():
        r = httpx.get("https://www.auswaertiges-amt.de/opendata/travelwarning", timeout=15)
        r.raise_for_status()
        payload = r.json().get("response") or {}
        for item in payload.values():
            if isinstance(item, dict) and (item.get("countryCode") or "").upper() == country_code.upper():
                return {
                    "country": item.get("countryName"),
                    "travel_warning": bool(item.get("warning")),
                    "partial_warning": bool(item.get("partialWarning")),
                    "situation_warning": bool(item.get("situationWarning")),
                    "source": "Auswärtiges Amt",
                    "link": "https://www.auswaertiges-amt.de/de/service/laender",
                }
        return None
    try:
        return _cached(f"aa:{country_code.upper()}", 3600 * 6, _do)
    except Exception as exc:
        log.warning("travel warning failed: %s", exc)
        return None


def destination_info(place: str, month: int | None = None) -> dict:
    geo = geocode(place)
    if not geo:
        return {"error": "place not found"}
    info = {"place": geo.get("name"), "country": geo.get("country"), "country_code": geo.get("country_code")}
    if month:
        info["climate"] = climate_summary(geo["latitude"], geo["longitude"], month)
    info["official_travel_advice"] = travel_warning(geo.get("country_code") or "") or "not available — verify at auswaertiges-amt.de"
    return info
