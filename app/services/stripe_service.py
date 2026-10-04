"""Оплата через Stripe-аккаунт АГЕНТА (restricted key). Деньги идут напрямую агенту — платформа их не касается."""
import logging
from decimal import ROUND_HALF_UP, Decimal

import stripe

from ..extensions import db
from ..models import Booking
from ..utils import utcnow

log = logging.getLogger(__name__)


class PaymentError(Exception):
    pass


def _client(agency) -> stripe.StripeClient:
    key = agency.get_secret("stripe_secret")
    if not key:
        raise PaymentError("Ключ Stripe не задан (Кабинет → Интеграции).")
    return stripe.StripeClient(key)


def test_key(agency) -> tuple[bool, str]:
    try:
        _client(agency).checkout.sessions.list(params={"limit": 1})
        return True, "Ключ Stripe работает (чтение Checkout Sessions)."
    except PaymentError as exc:
        return False, str(exc)
    except stripe.AuthenticationError:
        return False, "Ключ Stripe недействителен."
    except stripe.PermissionError:
        return False, "У restricted-ключа нет прав на Checkout Sessions."
    except stripe.StripeError as exc:
        return False, f"Ошибка Stripe: {exc.__class__.__name__}"


def create_checkout(agency, booking: Booking, customer_email: str | None = None) -> str:
    if booking.kind == "package" and not booking.sicherungsschein_confirmed:
        raise PaymentError("Для пакетного тура сначала подтвердите наличие Sicherungsschein.")
    if not booking.amount or booking.amount <= 0:
        raise PaymentError("Сумма должна быть больше нуля.")
    amount = int((Decimal(booking.amount) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    params = {
        "mode": "payment",
        "line_items": [{"quantity": 1, "price_data": {
            "currency": booking.currency.lower(), "unit_amount": amount,
            "product_data": {"name": booking.title[:120]}}}],
        "success_url": agency.site_url("/pay/success"),
        "cancel_url": agency.site_url("/pay/cancel"),
        "client_reference_id": str(booking.id),
        "metadata": {"booking_id": str(booking.id), "agency_id": str(agency.id)},
    }
    if customer_email:
        params["customer_email"] = customer_email
    try:
        session = _client(agency).checkout.sessions.create(params=params)
    except stripe.StripeError as exc:
        log.warning("stripe create session failed: %s", exc.__class__.__name__)
        raise PaymentError("Stripe отклонил запрос. Проверьте ключ и права.") from exc
    booking.stripe_session_id = session.id
    booking.payment_url = session.url
    booking.status = "awaiting_payment"
    db.session.commit()
    return session.url


def handle_webhook(agency, payload: bytes, signature: str) -> str:
    """Проверяет подпись СЕКРЕТОМ ЭТОГО АГЕНТСТВА и помечает бронь оплаченной."""
    secret = agency.get_secret("stripe_webhook")
    if not secret:
        raise PaymentError("webhook secret не задан")
    try:
        event = stripe.Webhook.construct_event(payload, signature, secret)
    except (ValueError, stripe.SignatureVerificationError) as exc:
        raise PaymentError("bad signature") from exc
    etype = event["type"]
    if etype in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        obj = event["data"]["object"]
        obj = obj.to_dict() if hasattr(obj, "to_dict") else dict(obj)
        booking_id = (obj.get("metadata") or {}).get("booking_id")
        if booking_id and str(obj.get("payment_status")) == "paid":
            booking = Booking.query.filter_by(id=int(booking_id), agency_id=agency.id).first()
            if booking and booking.stripe_session_id == obj.get("id"):
                booking.status = "paid"
                booking.paid_at = utcnow()
                db.session.commit()
                return "paid"
    return "ignored"
