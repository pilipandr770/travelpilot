import logging

from .base import Connector, ConnectorResult, SearchQuery  # noqa: F401
from .duffel import DuffelConnector
from .manual import ManualConnector

log = logging.getLogger(__name__)

REGISTRY: dict[str, Connector] = {c.name: c for c in (ManualConnector(), DuffelConnector())}


def active_connectors(agency) -> list[Connector]:
    return [c for c in REGISTRY.values() if c.available(agency)]


def search_all(agency, query: SearchQuery) -> dict:
    """Опрашивает все доступные коннекторы; сбой одного не ломает остальные."""
    offers, errors = [], []
    for connector in active_connectors(agency):
        try:
            res = connector.search(agency, query)
            offers.extend(res.offers)
            if res.error:
                errors.append(f"{connector.name}: {res.error}")
        except Exception:
            log.exception("connector %s failed", connector.name)
            errors.append(f"{connector.name}: internal error")
    return {"offers": offers[: max(1, min(query.limit, 8))], "errors": errors}
