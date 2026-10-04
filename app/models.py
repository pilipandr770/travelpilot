"""Модели данных.

Правило мультитенантности: КАЖДАЯ таблица с данными агентства имеет agency_id,
и КАЖДЫЙ запрос из кабинета/публичного сайта фильтруется по нему
(см. хелпер `scoped()` и docs/03-SECURITY.md).
"""
from decimal import Decimal

from flask import current_app
from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from . import crypto
from .extensions import db
from .utils import mask_secret, token, utcnow

# ---------------------------------------------------------------- справочники
AGENCY_STATUSES = ("sandbox", "pending_review", "active", "suspended")
LEAD_STATUSES = ("new", "contacted", "proposal", "booked", "lost")
BOOKING_STATUSES = ("draft", "proposal_sent", "awaiting_payment", "paid", "confirmed", "cancelled")
OFFER_KINDS = ("package", "flight", "hotel", "other")
CURRENCIES = ("EUR", "USD", "GBP", "CHF")

# Какие секреты можно хранить и как они называются в интерфейсе
SECRET_PROVIDERS = {
    "anthropic": "Anthropic API key",
    "stripe_secret": "Stripe restricted key",
    "stripe_webhook": "Stripe webhook secret",
    "duffel": "Duffel access token",
    "telegram_bot": "Telegram bot token",
}


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    agency_id = db.Column(db.Integer, db.ForeignKey("agencies.id"), index=True)
    email = db.Column(db.String(254), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    is_superadmin = db.Column(db.Boolean, nullable=False, default=False)
    active = db.Column(db.Boolean, nullable=False, default=True)
    email_confirmed_at = db.Column(db.DateTime)
    ui_language = db.Column(db.String(5), nullable=False, default="ru")
    failed_logins = db.Column(db.Integer, nullable=False, default=0)
    locked_until = db.Column(db.DateTime)
    last_login_at = db.Column(db.DateTime)
    session_version = db.Column(db.Integer, nullable=False, default=1)  # ++ при смене пароля → все сессии аннулируются
    # Зарезервировано под 2FA (фаза «Безопасность», см. docs/02-ROADMAP.md)
    totp_secret_enc = db.Column(db.Text)
    totp_enabled = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    agency = db.relationship("Agency", back_populates="users")

    @property
    def is_active(self):  # Flask-Login
        return bool(self.active)

    def get_id(self):
        return f"{self.id}.{self.session_version}"

    def set_password(self, password: str):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    def is_locked(self) -> bool:
        return bool(self.locked_until and self.locked_until > utcnow())


class Agency(db.Model):
    __tablename__ = "agencies"

    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(63), unique=True, nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="sandbox")
    plan = db.Column(db.String(20), nullable=False, default="trial")
    trial_ends_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    # --- юридические данные (Impressum) ---
    legal_name = db.Column(db.String(160))
    legal_form = db.Column(db.String(40))
    owner_name = db.Column(db.String(120))
    street = db.Column(db.String(160))
    zip_code = db.Column(db.String(20))
    city = db.Column(db.String(80))
    country = db.Column(db.String(2), default="DE")
    phone = db.Column(db.String(40))
    contact_email = db.Column(db.String(254))
    vat_id = db.Column(db.String(30))
    tax_number = db.Column(db.String(40))
    registry_info = db.Column(db.String(200))
    insurance_text = db.Column(db.Text)  # Berufshaftpflicht / Vermögensschadenhaftpflicht
    terms_md = db.Column(db.Text)  # AGB агентства
    vat_verified_at = db.Column(db.DateTime)
    accepted_terms_at = db.Column(db.DateTime)

    # --- брендинг ---
    logo_file = db.Column(db.String(80))
    hero_file = db.Column(db.String(80))
    gallery = db.Column(db.JSON, nullable=False, default=list)
    primary_color = db.Column(db.String(7), nullable=False, default="#0b6cff")
    tagline = db.Column(db.String(160))
    hero_title = db.Column(db.String(160))
    hero_subtitle = db.Column(db.String(300))
    about_md = db.Column(db.Text)
    default_language = db.Column(db.String(5), nullable=False, default="de")
    languages = db.Column(db.JSON, nullable=False, default=lambda: ["de"])
    site_published = db.Column(db.Boolean, nullable=False, default=False)

    # --- домен ---
    custom_domain = db.Column(db.String(253), unique=True)
    custom_domain_verified = db.Column(db.Boolean, nullable=False, default=False)
    domain_verify_token = db.Column(db.String(64))

    # --- чат-бот ---
    bot_enabled = db.Column(db.Boolean, nullable=False, default=True)
    bot_name = db.Column(db.String(60), default="Reise-Assistent")
    bot_greeting = db.Column(db.String(400))
    bot_tone = db.Column(db.String(300))
    bot_rules = db.Column(db.Text)
    bot_faq = db.Column(db.Text)
    bot_model = db.Column(db.String(60))

    # --- блог ---
    blog_enabled = db.Column(db.Boolean, nullable=False, default=False)
    blog_mode = db.Column(db.String(10), nullable=False, default="draft")  # draft | auto
    blog_posts_per_week = db.Column(db.Integer, nullable=False, default=2)
    blog_tone = db.Column(db.String(300))
    blog_topics = db.Column(db.Text)  # по строке на тему
    blog_language = db.Column(db.String(5), nullable=False, default="de")
    blog_model = db.Column(db.String(60))
    blog_ai_label = db.Column(db.Boolean, nullable=False, default=True)
    last_blog_run_at = db.Column(db.DateTime)
    last_blog_error = db.Column(db.String(300))

    # --- состояние интеграций ---
    llm_last_error = db.Column(db.String(300))
    llm_last_error_at = db.Column(db.DateTime)
    enabled_connectors = db.Column(db.JSON, nullable=False, default=list)  # напр. ["duffel"]

    users = db.relationship("User", back_populates="agency")

    # ------------------------------------------------------------------ URL
    def site_host(self) -> str:
        if self.custom_domain and self.custom_domain_verified:
            return self.custom_domain
        return f"{self.slug}.{current_app.config['BASE_DOMAIN']}"

    def site_url(self, path: str = "/") -> str:
        cfg = current_app.config
        port = cfg.get("PUBLIC_PORT") or ""
        host = self.site_host()
        # порт нужен только для платформенных поддоменов в dev
        if port and port not in ("80", "443") and host.endswith(cfg["BASE_DOMAIN"]):
            host = f"{host}:{port}"
        return f"{cfg['URL_SCHEME']}://{host}{path}"

    # ------------------------------------------------------- юридическая полнота
    def legal_missing(self) -> list[str]:
        required = {
            "legal_name": "Название юрлица / ИП",
            "owner_name": "Ответственное лицо",
            "street": "Улица и дом",
            "zip_code": "Индекс",
            "city": "Город",
            "country": "Страна",
            "phone": "Телефон",
            "contact_email": "Контактный e-mail",
            "insurance_text": "Страховка ответственности (Vermögensschadenhaftpflicht)",
            "terms_md": "AGB (условия агентства)",
        }
        missing = [label for field, label in required.items() if not (getattr(self, field) or "").strip()]
        if not ((self.vat_id or "").strip() or (self.tax_number or "").strip()):
            missing.append("USt-IdNr. или налоговый номер")
        if not self.accepted_terms_at:
            missing.append("Принятие условий платформы / AVV")
        return missing

    def can_publish(self) -> tuple[bool, list[str]]:
        problems = self.legal_missing()
        if self.status != "active":
            problems.append("Агентство ещё не одобрено администратором платформы")
        return (not problems, problems)

    # ------------------------------------------------------------ секреты
    def get_secret(self, provider: str) -> str | None:
        row = AgencySecret.query.filter_by(agency_id=self.id, provider=provider).first()
        if not row:
            return None
        try:
            return crypto.decrypt(row.ciphertext, agency_id=self.id, provider=provider)
        except crypto.DecryptionError:
            return None

    def has_secret(self, provider: str) -> bool:
        return AgencySecret.query.filter_by(agency_id=self.id, provider=provider).count() > 0

    def secret_hint(self, provider: str) -> str:
        row = AgencySecret.query.filter_by(agency_id=self.id, provider=provider).first()
        return ("•" * 8 + row.last4) if row else ""

    def set_secret(self, provider: str, value: str):
        if provider not in SECRET_PROVIDERS:
            raise ValueError("unknown provider")
        value = value.strip()
        row = AgencySecret.query.filter_by(agency_id=self.id, provider=provider).first()
        if not row:
            row = AgencySecret(agency_id=self.id, provider=provider)
            db.session.add(row)
        row.ciphertext = crypto.encrypt(value, agency_id=self.id, provider=provider)
        row.last4 = value[-4:]
        row.updated_at = utcnow()

    def delete_secret(self, provider: str):
        AgencySecret.query.filter_by(agency_id=self.id, provider=provider).delete()

    def checklist(self) -> list[dict]:
        active_offers = Offer.query.filter_by(agency_id=self.id, is_active=True).count()
        return [
            {"key": "legal", "label": "Юридические данные (Impressum, AGB, страховка)", "done": not self.legal_missing(), "endpoint": "dash.legal"},
            {"key": "brand", "label": "Бренд: логотип, название, тексты", "done": bool(self.logo_file and self.hero_title), "endpoint": "dash.brand"},
            {"key": "key", "label": "Ключ Anthropic (для чат-бота и блога)", "done": self.has_secret("anthropic"), "endpoint": "dash.integrations"},
            {"key": "bot", "label": "Настройка чат-бота", "done": bool(self.bot_greeting), "endpoint": "dash.bot"},
            {"key": "offers", "label": "Хотя бы одно активное предложение", "done": active_offers > 0, "endpoint": "dash.offers"},
            {"key": "blog", "label": "Блог: темы и источники", "done": bool((self.blog_topics or "").strip()), "endpoint": "dash.blog_settings"},
            {"key": "publish", "label": "Сайт опубликован", "done": bool(self.site_published), "endpoint": "dash.index"},
        ]


class AgencySecret(db.Model):
    __tablename__ = "agency_secrets"
    __table_args__ = (db.UniqueConstraint("agency_id", "provider", name="uq_secret_agency_provider"),)

    id = db.Column(db.Integer, primary_key=True)
    agency_id = db.Column(db.Integer, db.ForeignKey("agencies.id"), nullable=False, index=True)
    provider = db.Column(db.String(40), nullable=False)
    ciphertext = db.Column(db.Text, nullable=False)
    last4 = db.Column(db.String(8), nullable=False, default="")
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow)


class Lead(db.Model):
    __tablename__ = "leads"

    id = db.Column(db.Integer, primary_key=True)
    agency_id = db.Column(db.Integer, db.ForeignKey("agencies.id"), nullable=False, index=True)
    name = db.Column(db.String(120))
    email = db.Column(db.String(254))
    phone = db.Column(db.String(40))
    language = db.Column(db.String(5))
    message = db.Column(db.Text)
    source = db.Column(db.String(30), default="form")  # form | chat | manual
    status = db.Column(db.String(20), nullable=False, default="new")
    consent_at = db.Column(db.DateTime)  # согласие на обработку данных (DSGVO)
    consent_marketing = db.Column(db.Boolean, nullable=False, default=False)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    bookings = db.relationship("Booking", back_populates="lead")


class ChatSession(db.Model):
    __tablename__ = "chat_sessions"

    id = db.Column(db.Integer, primary_key=True)
    agency_id = db.Column(db.Integer, db.ForeignKey("agencies.id"), nullable=False, index=True)
    public_id = db.Column(db.String(48), unique=True, nullable=False, default=lambda: token(24))
    lead_id = db.Column(db.Integer, db.ForeignKey("leads.id"))
    language = db.Column(db.String(5))
    user_messages = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    last_message_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    messages = db.relationship("ChatMessage", back_populates="session", order_by="ChatMessage.id", cascade="all, delete-orphan")
    lead = db.relationship("Lead")


class ChatMessage(db.Model):
    __tablename__ = "chat_messages"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey("chat_sessions.id"), nullable=False, index=True)
    role = db.Column(db.String(12), nullable=False)  # user | assistant
    content = db.Column(db.Text, nullable=False)
    meta = db.Column(db.JSON)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    session = db.relationship("ChatSession", back_populates="messages")


class Offer(db.Model):
    __tablename__ = "offers"

    id = db.Column(db.Integer, primary_key=True)
    agency_id = db.Column(db.Integer, db.ForeignKey("agencies.id"), nullable=False, index=True)
    title = db.Column(db.String(200), nullable=False)
    destination = db.Column(db.String(120))
    kind = db.Column(db.String(12), nullable=False, default="package")
    description = db.Column(db.Text)
    includes = db.Column(db.Text)
    excludes = db.Column(db.Text)
    operator_name = db.Column(db.String(120))  # Reiseveranstalter / поставщик
    price_from = db.Column(db.Numeric(10, 2))
    currency = db.Column(db.String(3), nullable=False, default="EUR")
    price_note = db.Column(db.String(80), default="pro Person")
    nights = db.Column(db.Integer)
    departure_from = db.Column(db.String(80))
    valid_from = db.Column(db.Date)
    valid_until = db.Column(db.Date)
    url = db.Column(db.String(500))
    source = db.Column(db.String(20), nullable=False, default="manual")  # manual | ai_import
    is_active = db.Column(db.Boolean, nullable=False, default=False)
    last_verified_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    def public_dict(self) -> dict:
        return {
            "ref": f"manual:{self.id}",
            "id": self.id,
            "title": self.title,
            "destination": self.destination,
            "kind": self.kind,
            "description": (self.description or "")[:400],
            "includes": (self.includes or "")[:300],
            "excludes": (self.excludes or "")[:300],
            "operator": self.operator_name,
            "price_from": float(self.price_from) if self.price_from is not None else None,
            "currency": self.currency,
            "price_note": self.price_note,
            "nights": self.nights,
            "departure_from": self.departure_from,
            "valid_until": self.valid_until.isoformat() if self.valid_until else None,
            "last_verified": self.last_verified_at.date().isoformat() if self.last_verified_at else None,
            "source": "agency",
        }


class Booking(db.Model):
    __tablename__ = "bookings"

    id = db.Column(db.Integer, primary_key=True)
    agency_id = db.Column(db.Integer, db.ForeignKey("agencies.id"), nullable=False, index=True)
    lead_id = db.Column(db.Integer, db.ForeignKey("leads.id"), index=True)
    offer_id = db.Column(db.Integer, db.ForeignKey("offers.id"))
    title = db.Column(db.String(200), nullable=False)
    kind = db.Column(db.String(12), nullable=False, default="package")
    operator_name = db.Column(db.String(120))
    operator_booking_ref = db.Column(db.String(80))
    amount = db.Column(db.Numeric(10, 2), nullable=False, default=Decimal("0"))
    currency = db.Column(db.String(3), nullable=False, default="EUR")
    status = db.Column(db.String(20), nullable=False, default="draft")
    pax_adults = db.Column(db.Integer, nullable=False, default=1)
    pax_children = db.Column(db.Integer, nullable=False, default=0)
    travel_start = db.Column(db.Date)
    travel_end = db.Column(db.Date)
    destination = db.Column(db.String(120))
    notes = db.Column(db.Text)
    # Для пакетных туров платёжную ссылку можно создать только при подтверждённом Sicherungsschein
    sicherungsschein_confirmed = db.Column(db.Boolean, nullable=False, default=False)
    stripe_session_id = db.Column(db.String(120), index=True)
    payment_url = db.Column(db.String(600))
    paid_at = db.Column(db.DateTime)
    review_token = db.Column(db.String(48), unique=True, default=lambda: token(18))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    lead = db.relationship("Lead", back_populates="bookings")
    offer = db.relationship("Offer")
    documents = db.relationship("Document", back_populates="booking", order_by="Document.id")


class Document(db.Model):
    __tablename__ = "documents"

    id = db.Column(db.Integer, primary_key=True)
    agency_id = db.Column(db.Integer, db.ForeignKey("agencies.id"), nullable=False, index=True)
    booking_id = db.Column(db.Integer, db.ForeignKey("bookings.id"), index=True)
    kind = db.Column(db.String(20), nullable=False)  # proposal | confirmation | trip_guide
    language = db.Column(db.String(5))
    filename = db.Column(db.String(120), nullable=False)  # имя файла на диске (случайное)
    download_name = db.Column(db.String(160), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    booking = db.relationship("Booking", back_populates="documents")


class BlogPost(db.Model):
    __tablename__ = "blog_posts"
    __table_args__ = (db.UniqueConstraint("agency_id", "slug", name="uq_post_agency_slug"),)

    id = db.Column(db.Integer, primary_key=True)
    agency_id = db.Column(db.Integer, db.ForeignKey("agencies.id"), nullable=False, index=True)
    slug = db.Column(db.String(120), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    meta_description = db.Column(db.String(200))
    excerpt = db.Column(db.String(400))
    body_md = db.Column(db.Text, nullable=False)
    tags = db.Column(db.JSON, nullable=False, default=list)
    language = db.Column(db.String(5), nullable=False, default="de")
    status = db.Column(db.String(12), nullable=False, default="draft")  # draft | published
    generated_by_ai = db.Column(db.Boolean, nullable=False, default=False)
    topic = db.Column(db.String(200))
    source_urls = db.Column(db.JSON, nullable=False, default=list)
    published_at = db.Column(db.DateTime)
    crosspost_results = db.Column(db.JSON)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


class FeedSource(db.Model):
    __tablename__ = "feed_sources"

    id = db.Column(db.Integer, primary_key=True)
    agency_id = db.Column(db.Integer, db.ForeignKey("agencies.id"), nullable=False, index=True)
    url = db.Column(db.String(500), nullable=False)
    title = db.Column(db.String(160))
    active = db.Column(db.Boolean, nullable=False, default=True)
    last_fetched_at = db.Column(db.DateTime)
    last_error = db.Column(db.String(200))


class CrosspostChannel(db.Model):
    __tablename__ = "crosspost_channels"

    id = db.Column(db.Integer, primary_key=True)
    agency_id = db.Column(db.Integer, db.ForeignKey("agencies.id"), nullable=False, index=True)
    kind = db.Column(db.String(20), nullable=False)  # telegram (x, whatsapp — в дорожной карте)
    target = db.Column(db.String(120), nullable=False)  # telegram: @channel или chat_id
    enabled = db.Column(db.Boolean, nullable=False, default=True)


class Review(db.Model):
    __tablename__ = "reviews"

    id = db.Column(db.Integer, primary_key=True)
    agency_id = db.Column(db.Integer, db.ForeignKey("agencies.id"), nullable=False, index=True)
    booking_id = db.Column(db.Integer, db.ForeignKey("bookings.id"), unique=True)
    author_name = db.Column(db.String(80), nullable=False)
    rating = db.Column(db.Integer, nullable=False)
    text = db.Column(db.Text)
    approved = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


class SiteEvent(db.Model):
    """Приватная статистика без cookies и IP: только агрегируемые события."""
    __tablename__ = "site_events"

    id = db.Column(db.Integer, primary_key=True)
    agency_id = db.Column(db.Integer, db.ForeignKey("agencies.id"), nullable=False, index=True)
    kind = db.Column(db.String(20), nullable=False)  # pageview | chat_start | lead | post_view
    path = db.Column(db.String(200))
    referrer_host = db.Column(db.String(120))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)


class UsageEvent(db.Model):
    """Учёт токенов ИИ (платит агент своим ключом — ему нужна видимость расходов)."""
    __tablename__ = "usage_events"

    id = db.Column(db.Integer, primary_key=True)
    agency_id = db.Column(db.Integer, db.ForeignKey("agencies.id"), nullable=False, index=True)
    kind = db.Column(db.String(20), nullable=False)  # chat | blog | parse | guide
    model = db.Column(db.String(60))
    tokens_in = db.Column(db.Integer, nullable=False, default=0)
    tokens_out = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)


class AuditLog(db.Model):
    __tablename__ = "audit_log"

    id = db.Column(db.Integer, primary_key=True)
    agency_id = db.Column(db.Integer, index=True)
    user_id = db.Column(db.Integer)
    action = db.Column(db.String(60), nullable=False)
    detail = db.Column(db.String(300))
    ip = db.Column(db.String(45))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)


def scoped(model, agency_id: int):
    """Единая точка для запросов в рамках агентства: scoped(Offer, g.agency.id).filter(...)"""
    return model.query.filter(model.agency_id == agency_id)
