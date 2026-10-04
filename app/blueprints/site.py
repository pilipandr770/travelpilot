"""Публичный сайт агентства (запросы на <slug>.BASE_DOMAIN или подтверждённый custom domain)."""
from datetime import date
from urllib.parse import urlparse
from xml.sax.saxutils import escape

from email_validator import EmailNotValidError, validate_email
from flask import (Blueprint, Response, abort, current_app, flash, g, jsonify, make_response, redirect,
                   render_template, request, send_file, url_for)
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import or_

from .. import uploads
from ..extensions import csrf, db, limiter
from ..i18n import SITE_LANGS
from ..models import BlogPost, Booking, ChatMessage, ChatSession, Lead, Offer, Review, SiteEvent
from ..services import chat as chat_service
from ..services import llm, stripe_service
from ..utils import render_markdown, safe_next_url, strip_html, utcnow, valid_color

bp = Blueprint("site", __name__)

_BOT_WORDS = ("bot", "spider", "crawl", "slurp", "preview", "monitor", "curl", "python-requests")


# ---------------------------------------------------------------- gating
def _serializer():
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="preview")


def _gate():
    """None — показываем сайт; иначе готовый ответ (заглушка «скоро»)."""
    a = g.agency
    g.preview = False
    cookie = request.cookies.get("tp_preview")
    if cookie:
        try:
            g.preview = _serializer().loads(cookie, max_age=3600) == a.id
        except BadSignature:
            g.preview = False
    token = request.args.get("preview")
    if token:
        try:
            if _serializer().loads(token, max_age=3600) == a.id:
                resp = make_response(redirect(request.path))
                resp.set_cookie("tp_preview", token, max_age=3600, httponly=True, samesite="Lax",
                                secure=current_app.config["URL_SCHEME"] == "https")
                return resp
        except BadSignature:
            pass
    if request.path in ("/webhooks/stripe", "/robots.txt") or a.site_published or g.preview:
        return None
    resp = make_response(render_template("site/soon.html", agency=a), 200)
    resp.headers["X-Robots-Tag"] = "noindex"
    return resp


@bp.before_request
def _tenant_only():
    if g.agency is None:
        abort(404)
    return _gate()


def _render(template: str, **ctx):
    a = g.agency
    resp = make_response(render_template(template, agency=a, preview=getattr(g, "preview", False),
                                         site_langs=[l for l in (a.languages or []) if l in SITE_LANGS],
                                         **ctx))
    if not a.site_published:
        resp.headers["X-Robots-Tag"] = "noindex"
    return resp


def _track(kind: str, path: str):
    if getattr(g, "preview", False) or not g.agency.site_published:
        return
    ua = (request.headers.get("User-Agent") or "").lower()
    if not ua or any(w in ua for w in _BOT_WORDS):
        return
    ref = urlparse(request.referrer or "").netloc.split(":")[0].lower() or None
    if ref and ref == request.host.split(":")[0].lower():
        ref = None
    db.session.add(SiteEvent(agency_id=g.agency.id, kind=kind, path=path[:200], referrer_host=(ref or "")[:120] or None))
    db.session.commit()


def _active_offers(limit=None):
    q = Offer.query.filter(Offer.agency_id == g.agency.id, Offer.is_active.is_(True),
                           or_(Offer.valid_until.is_(None), Offer.valid_until >= date.today()))
    q = q.order_by(Offer.created_at.desc())
    return q.limit(limit).all() if limit else q.all()


def _published_posts():
    return BlogPost.query.filter_by(agency_id=g.agency.id, status="published").order_by(BlogPost.published_at.desc())


# ------------------------------------------------------------------ страницы
def home():
    gate = _gate()
    if gate is not None:
        return gate
    _track("pageview", "/")
    reviews = Review.query.filter_by(agency_id=g.agency.id, approved=True).order_by(Review.created_at.desc()).limit(6).all()
    return _render("site/home.html", offers=_active_offers(6), posts=_published_posts().limit(3).all(),
                   reviews=reviews, about_html=render_markdown(g.agency.about_md or ""))


@bp.route("/offers")
def offers():
    _track("pageview", "/offers")
    return _render("site/offers.html", offers=_active_offers())


@bp.route("/blog")
def blog_list():
    _track("pageview", "/blog")
    page = max(1, request.args.get("page", 1, type=int))
    q = _published_posts()
    total = q.count()
    posts = q.offset((page - 1) * 12).limit(12).all()
    return _render("site/blog_list.html", posts=posts, page=page, pages=max(1, -(-total // 12)))


@bp.route("/blog/<slug>")
def blog_post(slug):
    post = BlogPost.query.filter_by(agency_id=g.agency.id, slug=slug, status="published").first_or_404()
    _track("post_view", f"/blog/{slug}")
    return _render("site/blog_post.html", post=post, html=render_markdown(post.body_md))


@bp.route("/blog/feed.xml")
def blog_feed():
    a = g.agency
    items = "".join(
        f"<item><title>{escape(p.title)}</title><link>{escape(a.site_url('/blog/' + p.slug))}</link>"
        f"<guid>{escape(a.site_url('/blog/' + p.slug))}</guid>"
        f"<pubDate>{p.published_at.strftime('%a, %d %b %Y %H:%M:%S +0000') if p.published_at else ''}</pubDate>"
        f"<description>{escape(p.excerpt or '')}</description></item>"
        for p in _published_posts().limit(30).all())
    xml = (f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>{escape(a.name)}</title>'
           f"<link>{escape(a.site_url('/blog'))}</link><description>{escape(a.tagline or a.name)}</description>{items}</channel></rss>")
    return Response(xml, mimetype="application/rss+xml")


@bp.route("/impressum")
def impressum():
    return _render("site/impressum.html")


@bp.route("/datenschutz")
def datenschutz():
    return _render("site/datenschutz.html")


@bp.route("/agb")
def agb():
    return _render("site/agb.html", terms_html=render_markdown(g.agency.terms_md or ""))


@bp.route("/set-language/<lang>")
def set_language(lang):
    if lang not in (g.agency.languages or []):
        abort(404)
    resp = redirect(safe_next_url(request.args.get("next"), "/"))
    resp.set_cookie("lang", lang, max_age=60 * 60 * 24 * 365, samesite="Lax", httponly=True,
                    secure=current_app.config["URL_SCHEME"] == "https")
    return resp


# --------------------------------------------------------------- контактная форма
@bp.route("/contact", methods=["POST"])
@limiter.limit("5 per hour")
def contact():
    if request.form.get("website"):  # honeypot
        abort(400)
    name = (request.form.get("name") or "").strip()[:120]
    message = strip_html((request.form.get("message") or "").strip())[:3000]
    phone = (request.form.get("phone") or "").strip()[:40] or None
    try:
        email = validate_email(request.form.get("email", ""), check_deliverability=False).normalized
    except EmailNotValidError:
        email = None
    if not (name and email and message and request.form.get("consent")):
        return _render("site/message.html", text_key="s.c_error"), 400
    db.session.add(Lead(agency_id=g.agency.id, name=name, email=email, phone=phone, message=message,
                        language=g.lang, source="form", consent_at=utcnow()))
    db.session.add(SiteEvent(agency_id=g.agency.id, kind="lead", path="/contact"))
    db.session.commit()
    return _render("site/message.html", text_key="s.c_thanks")


# ------------------------------------------------------------------- отзывы
@bp.route("/review/<token>", methods=["GET", "POST"])
@limiter.limit("20 per hour", methods=["POST"])
def review(token):
    b = Booking.query.filter_by(agency_id=g.agency.id, review_token=token).first_or_404()
    if b.status not in ("paid", "confirmed") or Review.query.filter_by(booking_id=b.id).first():
        return _render("site/message.html", text_key="s.review_thanks")
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()[:80]
        try:
            rating = int(request.form.get("rating", ""))
        except ValueError:
            rating = 0
        if name and 1 <= rating <= 5:
            db.session.add(Review(agency_id=g.agency.id, booking_id=b.id, author_name=strip_html(name), rating=rating,
                                  text=strip_html((request.form.get("text") or "").strip())[:1500] or None))
            db.session.commit()
            return _render("site/message.html", text_key="s.review_thanks")
    return _render("site/review.html", booking=b)


# ------------------------------------------------------------------- чат-API
@bp.route("/chat/message", methods=["POST"])
@csrf.exempt
@limiter.limit("20 per minute;150 per hour")
def chat_message():
    a = g.agency
    origin = request.headers.get("Origin")
    if origin and urlparse(origin).netloc.split(":")[0].lower() != request.host.split(":")[0].lower():
        abort(403)
    if not (a.bot_enabled and a.has_secret("anthropic")):
        return jsonify(error="unavailable"), 503
    data = request.get_json(silent=True) or {}
    text = str(data.get("message", ""))[:chat_service.MAX_USER_CHARS]
    public_id = data.get("session") if isinstance(data.get("session"), str) else None
    if not text.strip():
        return jsonify(error="empty"), 400
    if chat_service.daily_cap_reached(a):
        return jsonify(error="limit"), 429
    session = chat_service.get_or_create_session(a, (public_id or "")[:48] or None, g.lang)
    if session.user_messages >= current_app.config["CHAT_MAX_USER_MESSAGES_PER_SESSION"]:
        db.session.commit()
        return jsonify(error="limit"), 429
    try:
        return jsonify(chat_service.handle_message(a, session, text, g.lang))
    except llm.LLMError:
        db.session.rollback()
        return jsonify(error="unavailable"), 503
    except Exception:  # noqa: BLE001
        db.session.rollback()
        current_app.logger.exception("chat failure")
        return jsonify(error="server"), 500


@bp.route("/chat/history/<public_id>")
@limiter.limit("30 per minute")
def chat_history(public_id):
    s = ChatSession.query.filter_by(agency_id=g.agency.id, public_id=public_id[:48]).first()
    if not s:
        return jsonify(messages=[])
    msgs = ChatMessage.query.filter_by(session_id=s.id).order_by(ChatMessage.id.desc()).limit(30).all()[::-1]
    return jsonify(messages=[{"role": m.role, "content": m.content} for m in msgs])


# ------------------------------------------------------------------ платежи
@bp.route("/webhooks/stripe", methods=["POST"])
@csrf.exempt
def stripe_webhook():
    try:
        stripe_service.handle_webhook(g.agency, request.get_data(), request.headers.get("Stripe-Signature", ""))
    except stripe_service.PaymentError:
        return ("bad request", 400)
    return ("ok", 200)


@bp.route("/pay/success")
def pay_success():
    return _render("site/message.html", text_key="s.pay_ok")


@bp.route("/pay/cancel")
def pay_cancel():
    return _render("site/message.html", text_key="s.pay_cancel")


# ----------------------------------------------------- служебные: CSS/media/SEO
def _text_color(hex_color: str) -> str:
    r, g_, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return "#111111" if (0.299 * r + 0.587 * g_ + 0.114 * b) > 160 else "#ffffff"


@bp.route("/theme.css")
def theme_css():
    color = valid_color(g.agency.primary_color)
    css = f":root{{--brand:{color};--on-brand:{_text_color(color)};}}\n"
    resp = Response(css, mimetype="text/css")
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


@bp.route("/media/<name>")
def media(name):
    path = uploads.image_path(g.agency.id, name)
    if not path:
        abort(404)
    return send_file(path, mimetype="image/webp", max_age=86400)


@bp.route("/robots.txt")
def robots():
    a = g.agency
    if a.site_published:
        body = f"User-agent: *\nAllow: /\nDisallow: /chat/\nSitemap: {a.site_url('/sitemap.xml')}\n"
    else:
        body = "User-agent: *\nDisallow: /\n"
    return Response(body, mimetype="text/plain")


@bp.route("/sitemap.xml")
def sitemap():
    a = g.agency
    urls = [("/", None), ("/offers", None), ("/blog", None), ("/impressum", None), ("/datenschutz", None)]
    urls += [(f"/blog/{p.slug}", p.published_at) for p in _published_posts().all()]
    body = "".join(f"<url><loc>{escape(a.site_url(path))}</loc>"
                   + (f"<lastmod>{d.date().isoformat()}</lastmod>" if d else "") + "</url>" for path, d in urls)
    xml = f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</urlset>'
    return Response(xml, mimetype="application/xml")
