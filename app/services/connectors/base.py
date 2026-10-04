"""Единый интерфейс «поставщика предложений».

Чтобы добавить нового поставщика: унаследуйте Connector, реализуйте search(),
зарегистрируйте в connectors/__init__.py. Подробности — docs/06-CONNECTORS.md.
"""
from dataclasses import dataclass, field


@dataclass
class SearchQuery:
    destination: str | None = None
    max_price: float | None = None
    month: str | None = None  # YYYY-MM
    kind: str | None = None
    limit: int = 5
    # для авиа-коннекторов
    origin_iata: str | None = None
    destination_iata: str | None = None
    date_out: str | None = None  # YYYY-MM-DD
    date_back: str | None = None
    adults: int = 1


@dataclass
class ConnectorResult:
    offers: list[dict] = field(default_factory=list)
    error: str | None = None


class Connector:
    name = "base"
    label = "Base"
    needs_secret: str | None = None  # какой секрет требуется

    def available(self, agency) -> bool:
        return True

    def search(self, agency, query: SearchQuery) -> ConnectorResult:  # pragma: no cover
        raise NotImplementedError
