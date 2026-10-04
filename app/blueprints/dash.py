"""Кабинет турагента (/app/...). Каждый запрос ограничен g.agency (= агентство текущего пользователя)."""
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

import dns.exception
import dns.resolver
import httpx
from email_validator import EmailNotValidError, validate_email
from flask import (Blueprint, abort, current_app, flash, g, redirect, render_template, request, send_file,
                   url_for)
from flask_login import current_user, login_required, logout_user
from itsdangerous import URLSafeTimedSerializer
from sqlalchemy import func

from .. import uploads
from ..extensions import db
from ..i18n import SITE_LANGS
from ..models import (CURRENCIES, LEAD_STATUSES, OFFER_KINDS, SECRET_PROVIDERS, Agency, BlogPost, Booking,
                      CrosspostChannel, Document, FeedSource, Lead, Offer, Review, SiteEvent, UsageEvent, scoped)
from ..security import audit
from ..services import blog as blog_service
from ..services import documents, guide, llm, offer_import, stripe_service, vat
from ..utils import clean_url, make_slug, safe_next_url, strip_html, utcnow, valid_color

bp = Blueprint("dash", __name__, url_prefix="/app")


@bp.before_request
def _guard():
    if not g.is_platform:
        abort(404)
    if not current_user.is_authenticated:
        return redirect(url_for("auth.login", next=request.path))
    if not current_user.agency:
        return redirect(url_for("admin.index")) if current_user.is_superadmin else abort(403)
    g.agency = current_user.agency


# ------------------------------------------------------------------ helpers
def own(model, obj_id):
    return scoped(model, g.agency.id).filter(model.id == obj_id).first_or_404()


def sval(name: str, maxlen: int) -> str | None:
    return (request.form.get(name) or "").strip()[:maxlen] or None


def ival(name: str, default=None, lo=0, hi=10_000):
    try:
        return max(lo, min(hi, int(request.form.get(name, ""))))
    except (ValueError, TypeError):
        return default


def dval(name: str):
    try:
        return datetime.strptime(request.form.get(name, ""), "%Y-%m-%d").date()
    except ValueError:
        return None


def money(name: str):
    raw = (request.form.get(name) or "").replace(",", ".").strip()
    try:
        value = Decimal(raw)
        return value if 0 <= value < Decimal("10000000") else None
    except InvalidOperation:
        return None


def checked(name: str) -> bool:
    return request.form.get(name) in ("on", "1", "true")


def preview_url() -> str:
    tok = URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="preview").dumps(g.agency.id)
    return g.agency.site_url(f"/?preview={tok}")


def _commit(msg="Сохранено."):
    db.session.commit()
    flash(msg, "success")


@bp.context_processor
def _ctx():
    return {"agency": g.agency, "SITE_LANGS": SITE_LANGS, "OFFER_KINDS": OFFER_KINDS, "CURRENCIES": CURRENCIES,
            "LEAD_STATUSES": LEAD_STATUSES}


# ------------------------------------------------------------------- обзор
@bp.route("/")
def index():
    since = utcnow() - timedelta(days=30)
    counts = dict(db.session.query(SiteEvent.kind, func.count()).filter(
        SiteEvent.agency_id == g.agency.id, SiteEvent.created_at >= since).group_by(SiteEvent.kind).all())
    new_leads = scoped(Lead, g.agency.id).filter(Lead.status == "new").count()
    return render_template("dash/index.html", checklist=g.agency.checklist(), counts=counts, new_leads=new_leads,
                           preview=preview_url(), email_confirmed=bool(current_user.email_confirmed_at))


@bp.route("/request-review", methods=["POST"])
def request_review():
    a = g.agency
    if not current_user.email_confirmed_at:
        flash("Сначала подтвердите e-mail.", "error")
    elif a.legal_missing():
        flash("Заполните юридические данные: " + ", ".join(a.legal_missing()), "error")
    elif a.status == "sandbox":
        if current_app.config["REQUIRE_MANUAL_APPROVAL"]:
            a.status = "pending_review"
            flash("Заявка на проверку отправлена. Мы свяжемся с вами.", "success")
        else:
            a.status = "active"
            flash("Агентство активировано.", "success")
        audit("request_review", agency_id=a.id, user_id=current_user.id)
        db.session.commit()
    return redirect(url_for("dash.index"))


@bp.route("/publish", methods=["POST"])
def publish_site():
    ok, problems = g.agency.can_publish()
    if not current_user.email_confirmed_at:
        ok, problems = False, problems + ["Подтвердите e-mail"]
    if not ok:
        flash("Публикация невозможна: " + "; ".join(problems), "error")
    else:
        g.agency.site_published = True
        audit("publish_site", agency_id=g.agency.id, user_id=current_user.id)
        _commit("Сайт опубликован.")
    return redirect(url_for("dash.index"))


@bp.route("/unpublish", methods=["POST"])
def unpublish_site():
    g.agency.site_published = False
    _commit("Сайт снят с публикации.")
    return redirect(url_for("dash.index"))


# --------------------------------------------------------- юридические данные
LEGAL_FIELDS = {"legal_name": 160, "legal_form": 40, "owner_name": 120, "street": 160, "zip_code": 20, "city": 80,
                "phone": 40, "vat_id": 30, "tax_number": 40, "registry_info": 200, "insurance_text": 1000}


@bp.route("/legal", methods=["GET", "POST"])
def legal():
    a = g.agency
    if request.method == "POST":
        old_vat = a.vat_id
        for field, n in LEGAL_FIELDS.items():
            setattr(a, field, sval(field, n))
        a.country = (sval("country", 2) or "DE").upper()
        a.terms_md = (request.form.get("terms_md") or "").strip()[:20000] or None
        try:
            a.contact_email = validate_email(request.form.get("contact_email", ""), check_deliverability=False).normalized
        except EmailNotValidError:
            flash("Контактный e-mail некорректен.", "error")
        if a.vat_id != old_vat:
            a.vat_verified_at = None
        _commit()
        return redirect(url_for("dash.legal"))
    return render_template("dash/legal.html", missing=a.legal_missing(), terms_template=TERMS_TEMPLATE)


@bp.route("/legal/check-vat", methods=["POST"])
def check_vat():
    ok, msg = vat.check_vat(g.agency.vat_id or "")
    if ok:
        g.agency.vat_verified_at = utcnow()
        db.session.commit()
    flash(msg, "success" if ok else "error")
    return redirect(url_for("dash.legal"))


TERMS_TEMPLATE = """## Allgemeine Geschäftsbedingungen (Beispielvorlage – bitte juristisch prüfen lassen!)

### 1. Geltungsbereich und Rolle des Reisebüros
Wir vermitteln Reiseleistungen und Pauschalreisen als Reisebüro (Reisevermittler). Der Reisevertrag kommt
ausschließlich mit dem jeweiligen Reiseveranstalter bzw. Leistungsträger zustande. Dessen Reisebedingungen gelten
ergänzend und werden Ihnen vor Vertragsschluss zur Verfügung gestellt.

### 2. Angebote und Preise
Angebote sind freibleibend und stehen unter dem Vorbehalt der Verfügbarkeit. Verbindlich sind erst die schriftliche
Reisebestätigung und die Rechnung des Veranstalters.

### 3. Zahlung
Zahlungen erfolgen nach den Angaben in der Reisebestätigung des Veranstalters. Bei Pauschalreisen erhalten Sie vor
einer Anzahlung den Sicherungsschein.

### 4. Haftung
Wir haften für sorgfältige Vermittlung und Beratung. Für die Durchführung der Reise haftet der Veranstalter.

### 5. Datenschutz
Es gilt unsere Datenschutzerklärung.
"""


# ------------------------------------------------------------ бренд и сайт
@bp.route("/brand", methods=["GET", "POST"])
def brand():
    a = g.agency
    if request.method == "POST":
        a.name = sval("name", 120) or a.name
        a.tagline = sval("tagline", 160)
        a.hero_title = sval("hero_title", 160)
        a.hero_subtitle = sval("hero_subtitle", 300)
        a.about_md = (request.form.get("about_md") or "").strip()[:6000] or None
        a.primary_color = valid_color(request.form.get("primary_color", ""), a.primary_color)
        langs = [l for l in request.form.getlist("languages") if l in SITE_LANGS] or ["de"]
        default = request.form.get("default_language")
        a.default_language = default if default in langs else langs[0]
        a.languages = langs
        try:
            for field, key in (("logo_file", "logo"), ("hero_file", "hero")):
                f = request.files.get(key)
                if f and f.filename:
                    uploads.delete_image(a.id, getattr(a, field))
                    setattr(a, field, uploads.save_image(f, a.id, 600 if key == "logo" else 1920))
            gallery = list(a.gallery or [])
            for f in request.files.getlist("gallery"):
                if f and f.filename and len(gallery) < 6:
                    gallery.append(uploads.save_image(f, a.id, 1400))
            a.gallery = gallery
        except uploads.UploadError as exc:
            flash(str(exc), "error")
        _commit()
        return redirect(url_for("dash.brand"))
    return render_template("dash/brand.html", preview=preview_url())


@bp.route("/brand/gallery/<name>/delete", methods=["POST"])
def gallery_delete(name):
    gallery = list(g.agency.gallery or [])
    if name in gallery:
        gallery.remove(name)
        g.agency.gallery = gallery
        uploads.delete_image(g.agency.id, name)
        db.session.commit()
    return redirect(url_for("dash.brand"))


@bp.route("/media/<name>")
def media(name):
    path = uploads.image_path(g.agency.id, name)
    if not path:
        abort(404)
    return send_file(path, mimetype="image/webp", max_age=3600)


# ------------------------------------------------------------------ чат-бот
@bp.route("/bot", methods=["GET", "POST"])
def bot():
    a = g.agency
    if request.method == "POST":
        a.bot_enabled = checked("bot_enabled")
        a.bot_name = sval("bot_name", 60) or "Reise-Assistent"
        a.bot_greeting = sval("bot_greeting", 400)
        a.bot_tone = sval("bot_tone", 300)
        a.bot_rules = (request.form.get("bot_rules") or "").strip()[:3000] or None
        a.bot_faq = (request.form.get("bot_faq") or "").strip()[:6000] or None
        model = request.form.get("bot_model")
        a.bot_model = model if model in current_app.config["ALLOWED_CLAUDE_MODELS"] else None
        _commit()
        return redirect(url_for("dash.bot"))
    return render_template("dash/bot.html", models=current_app.config["ALLOWED_CLAUDE_MODELS"], preview=preview_url(),
                           has_key=a.has_secret("anthropic"))


# --------------------------------------------------------------- интеграции
SECRET_RULES = {
    "anthropic": (re.compile(r"^sk-ant-[\w-]{20,}$"), "Ключ Anthropic начинается с sk-ant-"),
    "stripe_secret": (re.compile(r"^(rk_(live|test)_|sk_test_)[\w]{10,}$"),
                      "Используйте restricted-ключ (rk_live_… / rk_test_…) с правами только на Checkout Sessions. "
                      "Полные live-ключи sk_live_… не принимаются."),
    "stripe_webhook": (re.compile(r"^whsec_[\w]{10,}$"), "Webhook-секрет начинается с whsec_"),
    "duffel": (re.compile(r"^duffel_(test|live)_[\w-]{10,}$"), "Токен Duffel начинается с duffel_test_ или duffel_live_"),
    "telegram_bot": (re.compile(r"^\d{6,12}:[\w-]{30,}$"), "Формат токена: 123456789:AA…"),
}


@bp.route("/integrations")
def integrations():
    a = g.agency
    month_start = utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    usage = db.session.query(UsageEvent.kind, func.sum(UsageEvent.tokens_in), func.sum(UsageEvent.tokens_out),
                             func.count()).filter(UsageEvent.agency_id == a.id, UsageEvent.created_at >= month_start
                                                  ).group_by(UsageEvent.kind).all()
    return render_template("dash/integrations.html", providers=SECRET_PROVIDERS, usage=usage,
                           webhook_url=a.site_url("/webhooks/stripe"))


@bp.route("/integrations/<provider>/save", methods=["POST"])
def secret_save(provider):
    if provider not in SECRET_PROVIDERS:
        abort(404)
    value = (request.form.get("value") or "").strip()
    rx, hint = SECRET_RULES[provider]
    if not rx.match(value):
        flash(hint, "error")
    else:
        g.agency.set_secret(provider, value)
        audit("secret_set", agency_id=g.agency.id, user_id=current_user.id, detail=provider)
        _commit(f"{SECRET_PROVIDERS[provider]}: сохранён (зашифрован, больше не отображается).")
    return redirect(url_for("dash.integrations"))


@bp.route("/integrations/<provider>/delete", methods=["POST"])
def secret_delete(provider):
    if provider not in SECRET_PROVIDERS:
        abort(404)
    g.agency.delete_secret(provider)
    audit("secret_delete", agency_id=g.agency.id, user_id=current_user.id, detail=provider)
    _commit("Ключ удалён.")
    return redirect(url_for("dash.integrations"))


@bp.route("/integrations/<provider>/test", methods=["POST"])
def secret_test(provider):
    a = g.agency
    if provider == "anthropic":
        ok, msg = llm.test_key(a)
    elif provider == "stripe_secret":
        ok, msg = stripe_service.test_key(a)
    elif provider == "telegram_bot":
        token = a.get_secret("telegram_bot")
        try:
            r = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10) if token else None
            ok, msg = (bool(r and r.status_code == 200), "Бот отвечает." if r and r.status_code == 200 else "Telegram отклонил токен.")
        except httpx.HTTPError:
            ok, msg = False, "Telegram недоступен."
    else:
        ok, msg = False, "Для этого ключа автотест не предусмотрен."
    flash(msg, "success" if ok else "error")
    return redirect(url_for("dash.integrations"))


@bp.route("/integrations/connectors", methods=["POST"])
def connectors_save():
    enabled = [c for c in request.form.getlist("connectors") if c in ("duffel",)]
    g.agency.enabled_connectors = enabled
    _commit()
    return redirect(url_for("dash.integrations"))


# --------------------------------------------------------------- предложения
@bp.route("/offers")
def offers():
    rows = scoped(Offer, g.agency.id).order_by(Offer.is_active.desc(), Offer.created_at.desc()).all()
    return render_template("dash/offers.html", offers=rows, has_key=g.agency.has_secret("anthropic"))


def _apply_offer(o: Offer):
    o.title = sval("title", 190) or "Предложение"
    o.destination = sval("destination", 115)
    o.kind = request.form.get("kind") if request.form.get("kind") in OFFER_KINDS else "package"
    o.description = (request.form.get("description") or "").strip()[:3000] or None
    o.includes = (request.form.get("includes") or "").strip()[:1500] or None
    o.excludes = (request.form.get("excludes") or "").strip()[:1500] or None
    o.operator_name = sval("operator_name", 115)
    o.price_from = money("price_from")
    o.currency = request.form.get("currency") if request.form.get("currency") in CURRENCIES else "EUR"
    o.price_note = sval("price_note", 75) or "pro Person"
    o.nights = ival("nights", None, 0, 365)
    o.departure_from = sval("departure_from", 75)
    o.valid_from, o.valid_until = dval("valid_from"), dval("valid_until")
    o.url = clean_url(request.form.get("url"))
    o.is_active = checked("is_active")
    if checked("verified_now") or o.last_verified_at is None:
        o.last_verified_at = utcnow()


@bp.route("/offers/new", methods=["GET", "POST"])
def offer_new():
    if request.method == "POST":
        o = Offer(agency_id=g.agency.id, source="manual")
        _apply_offer(o)
        db.session.add(o)
        _commit()
        return redirect(url_for("dash.offers"))
    return render_template("dash/offer_form.html", offer=None)


@bp.route("/offers/<int:offer_id>", methods=["GET", "POST"])
def offer_edit(offer_id):
    o = own(Offer, offer_id)
    if request.method == "POST":
        _apply_offer(o)
        _commit()
        return redirect(url_for("dash.offers"))
    return render_template("dash/offer_form.html", offer=o)


@bp.route("/offers/<int:offer_id>/toggle", methods=["POST"])
def offer_toggle(offer_id):
    o = own(Offer, offer_id)
    o.is_active = not o.is_active
    db.session.commit()
    return redirect(url_for("dash.offers"))


@bp.route("/offers/<int:offer_id>/delete", methods=["POST"])
def offer_delete(offer_id):
    o = own(Offer, offer_id)
    Booking.query.filter_by(agency_id=g.agency.id, offer_id=o.id).update({"offer_id": None})
    db.session.delete(o)
    _commit("Предложение удалено.")
    return redirect(url_for("dash.offers"))


@bp.route("/offers/import", methods=["POST"])
def offer_import_text():
    text = (request.form.get("text") or "").strip()
    if len(text) < 20:
        flash("Вставьте текст предложения.", "error")
        return redirect(url_for("dash.offers"))
    try:
        created = offer_import.import_from_text(g.agency, text)
        flash(f"Распознано предложений: {len(created)}. Они сохранены НЕАКТИВНЫМИ — проверьте цены и даты и включите.", "success")
    except llm.LLMError as exc:
        flash(str(exc), "error")
    return redirect(url_for("dash.offers"))


# ------------------------------------------------------------------ клиенты
@bp.route("/leads")
def leads():
    q = scoped(Lead, g.agency.id)
    status = request.args.get("status")
    if status in LEAD_STATUSES:
        q = q.filter(Lead.status == status)
    search = (request.args.get("q") or "").strip()[:80]
    if search:
        like = f"%{search}%"
        q = q.filter(db.or_(Lead.name.ilike(like), Lead.email.ilike(like), Lead.phone.ilike(like)))
    return render_template("dash/leads.html", leads=q.order_by(Lead.created_at.desc()).limit(200).all(),
                           status=status, search=search)


@bp.route("/leads/new", methods=["POST"])
def lead_new():
    lead = Lead(agency_id=g.agency.id, source="manual", name=sval("name", 120), email=sval("email", 254),
                phone=sval("phone", 40), consent_at=utcnow() if checked("consent") else None)
    db.session.add(lead)
    db.session.commit()
    return redirect(url_for("dash.lead", lead_id=lead.id))


@bp.route("/leads/<int:lead_id>", methods=["GET", "POST"])
def lead(lead_id):
    l = own(Lead, lead_id)
    if request.method == "POST":
        l.name, l.email, l.phone = sval("name", 120), sval("email", 254), sval("phone", 40)
        status = request.form.get("status")
        l.status = status if status in LEAD_STATUSES else l.status
        l.notes = (request.form.get("notes") or "").strip()[:4000] or None
        _commit()
        return redirect(url_for("dash.lead", lead_id=l.id))
    from ..models import ChatSession
    sessions = ChatSession.query.filter_by(agency_id=g.agency.id, lead_id=l.id).all()
    offers_ = scoped(Offer, g.agency.id).filter_by(is_active=True).order_by(Offer.title).all()
    return render_template("dash/lead.html", lead=l, sessions=sessions, offers=offers_)


@bp.route("/leads/<int:lead_id>/delete", methods=["POST"])
def lead_delete(lead_id):
    """Право на удаление (DSGVO Art. 17). Брони и документы клиента отвязываются/удаляются."""
    from ..models import ChatSession
    l = own(Lead, lead_id)
    for s in ChatSession.query.filter_by(agency_id=g.agency.id, lead_id=l.id).all():
        db.session.delete(s)
    for b in Booking.query.filter_by(agency_id=g.agency.id, lead_id=l.id).all():
        for d in list(b.documents):
            _remove_doc_file(d)
            db.session.delete(d)
        db.session.delete(b)
    db.session.delete(l)
    audit("lead_delete", agency_id=g.agency.id, user_id=current_user.id, detail=str(lead_id))
    _commit("Клиент и связанные данные удалены.")
    return redirect(url_for("dash.leads"))


@bp.route("/leads/<int:lead_id>/bookings", methods=["POST"])
def booking_new(lead_id):
    l = own(Lead, lead_id)
    offer = None
    if request.form.get("offer_id"):
        offer = own(Offer, ival("offer_id", 0))
    b = Booking(agency_id=g.agency.id, lead_id=l.id, offer_id=offer.id if offer else None,
                title=(offer.title if offer else sval("title", 190)) or "Поездка",
                kind=offer.kind if offer else "package", operator_name=offer.operator_name if offer else None,
                amount=(offer.price_from if offer and offer.price_from else Decimal("0")),
                currency=offer.currency if offer else "EUR", destination=offer.destination if offer else None)
    db.session.add(b)
    l.status = "proposal" if l.status in ("new", "contacted") else l.status
    db.session.commit()
    return redirect(url_for("dash.booking", booking_id=b.id))


# --------------------------------------------------------- брони и документы
@bp.route("/bookings/<int:booking_id>", methods=["GET", "POST"])
def booking(booking_id):
    b = own(Booking, booking_id)
    if request.method == "POST":
        b.title = sval("title", 190) or b.title
        b.kind = request.form.get("kind") if request.form.get("kind") in OFFER_KINDS else b.kind
        b.operator_name = sval("operator_name", 115)
        b.operator_booking_ref = sval("operator_booking_ref", 75)
        b.destination = sval("destination", 115)
        amount = money("amount")
        b.amount = amount if amount is not None else b.amount
        b.currency = request.form.get("currency") if request.form.get("currency") in CURRENCIES else b.currency
        b.pax_adults, b.pax_children = ival("pax_adults", 1, 1, 50), ival("pax_children", 0, 0, 50)
        b.travel_start, b.travel_end = dval("travel_start"), dval("travel_end")
        b.notes = (request.form.get("notes") or "").strip()[:4000] or None
        b.sicherungsschein_confirmed = checked("sicherungsschein_confirmed")
        status = request.form.get("status")
        if status in ("draft", "proposal_sent", "awaiting_payment", "paid", "confirmed", "cancelled"):
            b.status = status
            if status == "confirmed" and b.lead:
                b.lead.status = "booked"
        _commit()
        return redirect(url_for("dash.booking", booking_id=b.id))
    review = Review.query.filter_by(agency_id=g.agency.id, booking_id=b.id).first()
    return render_template("dash/booking.html", booking=b, review=review,
                           review_url=g.agency.site_url(f"/review/{b.review_token}"),
                           has_stripe=g.agency.has_secret("stripe_secret"))


@bp.route("/bookings/<int:booking_id>/docs", methods=["POST"])
def booking_docs(booking_id):
    b = own(Booking, booking_id)
    kind = request.form.get("kind")
    lang = request.form.get("lang") if request.form.get("lang") in SITE_LANGS else "de"
    try:
        if kind in ("proposal", "confirmation"):
            if not b.operator_name:
                flash("Укажите туроператора/поставщика — без него документ юридически неполон.", "error")
                return redirect(url_for("dash.booking", booking_id=b.id))
            builder = documents.build_proposal if kind == "proposal" else documents.build_confirmation
            name = builder(g.agency, b, b.lead, lang)
            documents.save_document(g.agency, b, kind, lang, name)
        elif kind == "trip_guide":
            guide.generate(g.agency, b, lang)
        else:
            abort(400)
        flash("Документ создан. Проверьте его перед отправкой клиенту.", "success")
    except llm.LLMError as exc:
        flash(str(exc), "error")
    return redirect(url_for("dash.booking", booking_id=b.id))


def _doc_path(d: Document):
    from pathlib import Path
    return Path(current_app.config["UPLOAD_DIR"]) / str(d.agency_id) / "docs" / d.filename


def _remove_doc_file(d: Document):
    p = _doc_path(d)
    if p.is_file():
        p.unlink()


@bp.route("/documents/<int:doc_id>")
def document_download(doc_id):
    d = own(Document, doc_id)
    path = _doc_path(d)
    if not path.is_file():
        abort(404)
    return send_file(path, mimetype="application/pdf", as_attachment=True, download_name=d.download_name)


@bp.route("/documents/<int:doc_id>/delete", methods=["POST"])
def document_delete(doc_id):
    d = own(Document, doc_id)
    booking_id = d.booking_id
    _remove_doc_file(d)
    db.session.delete(d)
    db.session.commit()
    return redirect(url_for("dash.booking", booking_id=booking_id))


@bp.route("/bookings/<int:booking_id>/pay", methods=["POST"])
def booking_pay(booking_id):
    b = own(Booking, booking_id)
    try:
        stripe_service.create_checkout(g.agency, b, customer_email=b.lead.email if b.lead else None)
        audit("payment_link", agency_id=g.agency.id, user_id=current_user.id, detail=f"booking {b.id}")
        flash("Ссылка на оплату создана — отправьте её клиенту.", "success")
    except stripe_service.PaymentError as exc:
        flash(str(exc), "error")
    return redirect(url_for("dash.booking", booking_id=b.id))


@bp.route("/bookings/<int:booking_id>/delete", methods=["POST"])
def booking_delete(booking_id):
    b = own(Booking, booking_id)
    lead_id = b.lead_id
    for d in list(b.documents):
        _remove_doc_file(d)
        db.session.delete(d)
    Review.query.filter_by(agency_id=g.agency.id, booking_id=b.id).delete()
    db.session.delete(b)
    _commit("Бронь удалена.")
    return redirect(url_for("dash.lead", lead_id=lead_id) if lead_id else url_for("dash.leads"))


# --------------------------------------------------------------------- блог
@bp.route("/blog")
def blog():
    posts = scoped(BlogPost, g.agency.id).order_by(BlogPost.created_at.desc()).all()
    return render_template("dash/blog.html", posts=posts, has_key=g.agency.has_secret("anthropic"))


@bp.route("/blog/generate", methods=["POST"])
def blog_generate():
    topic = sval("topic", 190)
    try:
        post = blog_service.generate_post(g.agency, topic=topic, sources=[]) if topic else blog_service.generate_post(g.agency)
        flash("Черновик создан — проверьте и опубликуйте.", "success")
        return redirect(url_for("dash.post_edit", post_id=post.id))
    except llm.LLMError as exc:
        flash(str(exc), "error")
    except Exception:  # noqa: BLE001
        current_app.logger.exception("blog generate failed")
        flash("Не удалось сгенерировать статью. Подробности в логах.", "error")
    return redirect(url_for("dash.blog"))


@bp.route("/blog/<int:post_id>", methods=["GET", "POST"])
def post_edit(post_id):
    p = own(BlogPost, post_id)
    if request.method == "POST":
        p.title = sval("title", 190) or p.title
        p.excerpt = sval("excerpt", 390)
        p.meta_description = sval("meta_description", 190)
        p.body_md = (request.form.get("body_md") or "").strip()[:20000] or p.body_md
        p.tags = [t.strip()[:30] for t in (request.form.get("tags") or "").split(",") if t.strip()][:6]
        new_slug = make_slug(request.form.get("slug") or p.title, 100)
        if new_slug != p.slug and not BlogPost.query.filter_by(agency_id=g.agency.id, slug=new_slug).first():
            p.slug = new_slug
        _commit()
        return redirect(url_for("dash.post_edit", post_id=p.id))
    from ..utils import render_markdown
    return render_template("dash/post_edit.html", post=p, html=render_markdown(p.body_md),
                           public_url=g.agency.site_url(f"/blog/{p.slug}"))


@bp.route("/blog/<int:post_id>/publish", methods=["POST"])
def post_publish(post_id):
    p = own(BlogPost, post_id)
    blog_service.publish_post(g.agency, p)
    flash("Опубликовано." + (f" Кросспост: {p.crosspost_results}" if p.crosspost_results else ""), "success")
    return redirect(url_for("dash.post_edit", post_id=p.id))


@bp.route("/blog/<int:post_id>/unpublish", methods=["POST"])
def post_unpublish(post_id):
    p = own(BlogPost, post_id)
    p.status = "draft"
    _commit("Снято с публикации.")
    return redirect(url_for("dash.post_edit", post_id=p.id))


@bp.route("/blog/<int:post_id>/delete", methods=["POST"])
def post_delete(post_id):
    db.session.delete(own(BlogPost, post_id))
    _commit("Статья удалена.")
    return redirect(url_for("dash.blog"))


@bp.route("/blog/settings", methods=["GET", "POST"])
def blog_settings():
    a = g.agency
    if request.method == "POST":
        a.blog_enabled = checked("blog_enabled")
        a.blog_mode = "auto" if request.form.get("blog_mode") == "auto" else "draft"
        a.blog_posts_per_week = ival("blog_posts_per_week", 2, 1, 14)
        a.blog_tone = sval("blog_tone", 300)
        a.blog_topics = (request.form.get("blog_topics") or "").strip()[:4000] or None
        lang = request.form.get("blog_language")
        a.blog_language = lang if lang in SITE_LANGS else "de"
        model = request.form.get("blog_model")
        a.blog_model = model if model in current_app.config["ALLOWED_CLAUDE_MODELS"] else None
        a.blog_ai_label = checked("blog_ai_label")
        _commit()
        return redirect(url_for("dash.blog_settings"))
    feeds = scoped(FeedSource, a.id).all()
    channels = scoped(CrosspostChannel, a.id).all()
    return render_template("dash/blog_settings.html", feeds=feeds, channels=channels,
                           models=current_app.config["ALLOWED_CLAUDE_MODELS"], has_tg=a.has_secret("telegram_bot"))


@bp.route("/blog/feeds/add", methods=["POST"])
def feed_add():
    url = clean_url(request.form.get("url"))
    if not url:
        flash("Нужна ссылка http(s) на RSS/Atom.", "error")
    elif scoped(FeedSource, g.agency.id).count() >= 15:
        flash("Максимум 15 источников.", "error")
    else:
        db.session.add(FeedSource(agency_id=g.agency.id, url=url))
        _commit("Источник добавлен.")
    return redirect(url_for("dash.blog_settings"))


@bp.route("/blog/feeds/<int:feed_id>/delete", methods=["POST"])
def feed_delete(feed_id):
    db.session.delete(own(FeedSource, feed_id))
    _commit("Источник удалён.")
    return redirect(url_for("dash.blog_settings"))


@bp.route("/blog/channels/add", methods=["POST"])
def channel_add():
    target = sval("target", 120)
    if not target or not re.match(r"^(@[\w]{4,64}|-?\d{5,20})$", target):
        flash("Укажите @channel или числовой chat_id.", "error")
    else:
        db.session.add(CrosspostChannel(agency_id=g.agency.id, kind="telegram", target=target))
        _commit("Канал добавлен.")
    return redirect(url_for("dash.blog_settings"))


@bp.route("/blog/channels/<int:ch_id>/delete", methods=["POST"])
def channel_delete(ch_id):
    db.session.delete(own(CrosspostChannel, ch_id))
    _commit("Канал удалён.")
    return redirect(url_for("dash.blog_settings"))


# ------------------------------------------------------------------- отзывы
@bp.route("/reviews")
def reviews():
    return render_template("dash/reviews.html", reviews=scoped(Review, g.agency.id).order_by(Review.created_at.desc()).all())


@bp.route("/reviews/<int:review_id>/<action>", methods=["POST"])
def review_action(review_id, action):
    r = own(Review, review_id)
    if action == "approve":
        r.approved = True
    elif action == "hide":
        r.approved = False
    elif action == "delete":
        db.session.delete(r)
    else:
        abort(404)
    db.session.commit()
    return redirect(url_for("dash.reviews"))


# --------------------------------------------------------------- статистика
@bp.route("/stats")
def stats():
    a = g.agency
    since = utcnow() - timedelta(days=30)
    base = db.session.query(SiteEvent).filter(SiteEvent.agency_id == a.id, SiteEvent.created_at >= since)
    kinds = dict(db.session.query(SiteEvent.kind, func.count()).filter(
        SiteEvent.agency_id == a.id, SiteEvent.created_at >= since).group_by(SiteEvent.kind).all())
    top_pages = db.session.query(SiteEvent.path, func.count().label("n")).filter(
        SiteEvent.agency_id == a.id, SiteEvent.created_at >= since, SiteEvent.kind.in_(("pageview", "post_view"))
    ).group_by(SiteEvent.path).order_by(func.count().desc()).limit(10).all()
    referrers = db.session.query(SiteEvent.referrer_host, func.count().label("n")).filter(
        SiteEvent.agency_id == a.id, SiteEvent.created_at >= since, SiteEvent.referrer_host.isnot(None)
    ).group_by(SiteEvent.referrer_host).order_by(func.count().desc()).limit(10).all()
    views = kinds.get("pageview", 0) + kinds.get("post_view", 0)
    conv = round(100 * kinds.get("lead", 0) / views, 1) if views else 0
    usage = db.session.query(UsageEvent.kind, func.sum(UsageEvent.tokens_in), func.sum(UsageEvent.tokens_out)).filter(
        UsageEvent.agency_id == a.id, UsageEvent.created_at >= since).group_by(UsageEvent.kind).all()
    paid = scoped(Booking, a.id).filter(Booking.status.in_(("paid", "confirmed"))).count()
    return render_template("dash/stats.html", kinds=kinds, top_pages=top_pages, referrers=referrers, conv=conv,
                           usage=usage, views=views, paid=paid, _base=base)


# --------------------------------------------------------------------- домен
HOST_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$")


@bp.route("/domain", methods=["GET", "POST"])
def domain():
    a = g.agency
    if request.method == "POST":
        host = (request.form.get("domain") or "").strip().lower().rstrip(".")
        base = current_app.config["BASE_DOMAIN"]
        if not HOST_RE.match(host) or host == base or host.endswith("." + base):
            flash("Некорректный домен.", "error")
        elif Agency.query.filter(Agency.custom_domain == host, Agency.id != a.id).first():
            flash("Этот домен уже используется.", "error")
        else:
            from ..utils import token
            a.custom_domain, a.custom_domain_verified, a.domain_verify_token = host, False, token(18)
            _commit("Домен сохранён. Добавьте DNS-записи и нажмите «Проверить».")
        return redirect(url_for("dash.domain"))
    return render_template("dash/domain.html", target=current_app.config.get("CUSTOM_DOMAIN_TARGET") or f"sites.{current_app.config['BASE_DOMAIN']}")


@bp.route("/domain/verify", methods=["POST"])
def domain_verify():
    a = g.agency
    if not a.custom_domain:
        return redirect(url_for("dash.domain"))
    name = f"_travelpilot-verify.{a.custom_domain}"
    try:
        answers = dns.resolver.resolve(name, "TXT", lifetime=6)
        values = {b"".join(r.strings).decode() for r in answers}
    except (dns.exception.DNSException, UnicodeDecodeError):
        values = set()
    if a.domain_verify_token in values:
        a.custom_domain_verified = True
        audit("domain_verified", agency_id=a.id, user_id=current_user.id, detail=a.custom_domain)
        _commit("Домен подтверждён. Сертификат выпустится при первом заходе на сайт.")
    else:
        flash("TXT-запись не найдена. DNS может обновляться до нескольких часов.", "error")
    return redirect(url_for("dash.domain"))


@bp.route("/domain/remove", methods=["POST"])
def domain_remove():
    g.agency.custom_domain, g.agency.custom_domain_verified, g.agency.domain_verify_token = None, False, None
    _commit("Домен удалён.")
    return redirect(url_for("dash.domain"))


# ------------------------------------------------------------------ аккаунт
@bp.route("/account", methods=["GET", "POST"])
def account():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "language":
            lang = request.form.get("ui_language")
            current_user.ui_language = lang if lang in ("ru", "de") else "ru"
            db.session.commit()
        elif action == "password":
            if not current_user.check_password(request.form.get("current") or ""):
                flash("Текущий пароль неверен.", "error")
            elif len(request.form.get("new") or "") < 12:
                flash("Новый пароль — минимум 12 символов.", "error")
            else:
                current_user.set_password(request.form["new"])
                current_user.session_version += 1
                db.session.commit()
                audit("password_change", agency_id=g.agency.id, user_id=current_user.id)
                logout_user()
                flash("Пароль изменён. Войдите снова.", "success")
                return redirect(url_for("auth.login"))
        elif action == "resend":
            from .auth import send_confirmation
            send_confirmation(current_user)
            flash("Письмо отправлено.", "success")
        return redirect(url_for("dash.account"))
    return render_template("dash/account.html")
