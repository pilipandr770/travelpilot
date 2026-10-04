"""Ручные предложения агента (вставленные из порталов операторов или импортированные ИИ)."""
from datetime import date

from sqlalchemy import or_

from ...models import Offer
from .base import Connector, ConnectorResult, SearchQuery


class ManualConnector(Connector):
    name = "manual"
    label = "Предложения агентства (ручной ввод)"

    def search(self, agency, query: SearchQuery) -> ConnectorResult:
        q = Offer.query.filter(Offer.agency_id == agency.id, Offer.is_active.is_(True))
        today = date.today()
        q = q.filter(or_(Offer.valid_until.is_(None), Offer.valid_until >= today))
        if query.destination:
            like = f"%{query.destination.strip()}%"
            q = q.filter(or_(Offer.destination.ilike(like), Offer.title.ilike(like), Offer.description.ilike(like)))
        if query.max_price:
            q = q.filter(or_(Offer.price_from.is_(None), Offer.price_from <= query.max_price))
        if query.kind:
            q = q.filter(Offer.kind == query.kind)
        offers = q.order_by(Offer.price_from.asc().nullslast()).limit(max(1, min(query.limit, 8))).all()
        return ConnectorResult(offers=[o.public_dict() for o in offers])
