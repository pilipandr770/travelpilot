import hashlib
import hmac
import io
import json
import time
from datetime import date, timedelta
from decimal import Decimal

import pytest
from PIL import Image

from app import crypto, uploads
from app.extensions import db
from app.models import (Agency, AgencySecret, BlogPost, Booking, ChatSession, Lead, Offer, Review, UsageEvent, User)
from app.security import UnsafeURL, assert_public_url
from app.services import blog, chat, documents, llm, offer_import, stripe_service
from app.utils import render_markdown, utcnow

from .conftest import (PLATFORM, fake_response, login, make_active_agency, register, tenant_url, text_block,
                       tool_block)


# ----------------------------------------------------------- платформа / auth
def test_landing_languages(client):
    de = client.get("/", base_url=PLATFORM)
    assert de.status_code == 200 and b"KI-gest" in de.data
    ru = client.get("/?lang=ru", base_url=PLATFORM)
    assert "турагентство с ИИ" in ru.get_data(as_text=True)


def test_unknown_subdomain_is_404(client):
    assert client.get("/", base_url="http://nope.tp.test").status_code == 404
    assert client.get("/", base_url="http://evil.example").status_code == 404


def test_register_login_logout(client):
    r = register(client)
    assert r.status_code == 302 and "/app" in r.headers["Location"]
    assert client.get("/app/", base_url=PLATFORM).status_code == 200
    client.post("/logout", base_url=PLATFORM)
    assert client.get("/app/", base_url=PLATFORM).status_code == 302
    assert login(client, password="wrong password!!").status_code == 401
    assert login(client).status_code == 302


def test_register_rejects_short_password_and_duplicate(client):
    assert register(client, password="short").status_code == 400
    assert register(client).status_code == 302
    client.post("/logout", base_url=PLATFORM)
    assert register(client).status_code == 400  # e-mail уже занят


def test_account_lockout(client):
    register(client)
    client.post("/logout", base_url=PLATFORM)
    for _ in range(5):
        login(client, password="bad bad bad bad")
    assert login(client).status_code == 429  # даже верный пароль заблокирован


def test_open_redirect_blocked(client):
    register(client)
    client.post("/logout", base_url=PLATFORM)
    r = client.post("/login?next=//evil.com", base_url=PLATFORM, data={"email": "owner@acme-mail.com", "password": "correct horse battery"})
    assert r.headers["Location"].endswith("/app/")


def test_security_headers(client):
    r = client.get("/", base_url=PLATFORM)
    csp = r.headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert r.headers["X-Content-Type-Options"] == "nosniff"


def test_dashboard_requires_login(client):
    assert client.get("/app/offers", base_url=PLATFORM).status_code == 302
    assert client.get("/admin/", base_url=PLATFORM).status_code == 302


def test_dashboard_not_reachable_on_tenant_host(client):
    agency, _ = make_active_agency(client)
    assert client.get("/app/", base_url=tenant_url(agency.slug)).status_code == 404
    assert client.get("/login", base_url=tenant_url(agency.slug)).status_code == 404


# ------------------------------------------------------- изоляция арендаторов
def test_tenant_isolation_idor(client, app):
    a, _ = make_active_agency(client, "Alpha Tours", "a@alpha-mail.com")
    offer = Offer(agency_id=a.id, title="Alpha secret", is_active=True)
    db.session.add(offer)
    lead = Lead(agency_id=a.id, name="Alpha Kunde")
    db.session.add(lead)
    db.session.commit()
    client.post("/logout", base_url=PLATFORM)
    register(client, "Beta Reisen", "b@beta-mail.com")  # залогинен как Beta
    for url in (f"/app/offers/{offer.id}", f"/app/leads/{lead.id}"):
        assert client.get(url, base_url=PLATFORM).status_code == 404
    assert client.post(f"/app/offers/{offer.id}/toggle", base_url=PLATFORM).status_code == 404
    assert client.post(f"/app/offers/{offer.id}/delete", base_url=PLATFORM).status_code == 404
    assert client.post(f"/app/leads/{lead.id}/delete", base_url=PLATFORM).status_code == 404
    assert db.session.get(Offer, offer.id) is not None


def test_secret_encryption_and_binding(client):
    a, _ = make_active_agency(client, "Alpha Tours", "a@alpha-mail.com")
    b, _ = make_active_agency(client, "Beta Reisen", "b@beta-mail.com")
    a.set_secret("anthropic", "sk-ant-" + "x" * 30)
    db.session.commit()
    row = AgencySecret.query.filter_by(agency_id=a.id).first()
    assert "sk-ant" not in row.ciphertext and a.get_secret("anthropic").startswith("sk-ant-")
    # подмена строки другому агентству не расшифровывается
    db.session.add(AgencySecret(agency_id=b.id, provider="anthropic", ciphertext=row.ciphertext, last4="xxxx"))
    db.session.commit()
    assert b.get_secret("anthropic") is None


def test_secret_rotation_multifernet(app):
    old = crypto.generate_key()
    new = crypto.generate_key()
    app.config["ENCRYPTION_KEYS"] = [old]
    token = crypto.encrypt("v", agency_id=1, provider="p")
    app.config["ENCRYPTION_KEYS"] = [new, old]
    assert crypto.decrypt(token, agency_id=1, provider="p") == "v"


def test_secret_validation_and_never_shown(client):
    register(client)
    good = "sk-ant-api03-" + "A" * 40
    r = client.post("/app/integrations/anthropic/save", base_url=PLATFORM, data={"value": "garbage"})
    assert r.status_code == 302 and not Agency.query.first().has_secret("anthropic")
    client.post("/app/integrations/anthropic/save", base_url=PLATFORM, data={"value": good})
    assert Agency.query.first().has_secret("anthropic")
    page = client.get("/app/integrations", base_url=PLATFORM).get_data(as_text=True)
    assert good not in page and "AAAA" in page  # видны только последние 4 символа
    # полные боевые Stripe-ключи не принимаются
    client.post("/app/integrations/stripe_secret/save", base_url=PLATFORM, data={"value": "sk_live_" + "a" * 30})
    assert not Agency.query.first().has_secret("stripe_secret")
    client.post("/app/integrations/stripe_secret/save", base_url=PLATFORM, data={"value": "rk_live_" + "a" * 30})
    assert Agency.query.first().has_secret("stripe_secret")


# ------------------------------------------------------------- публикация
def test_publish_gating_and_site_visibility(client):
    register(client)
    agency = Agency.query.first()
    host = tenant_url(agency.slug)
    assert b"coming soon" in client.get("/", base_url=host).data.lower() or client.get("/", base_url=host).status_code == 200
    r = client.post("/app/publish", base_url=PLATFORM)
    assert r.status_code == 302 and not Agency.query.first().site_published  # не хватает данных
    client.post("/logout", base_url=PLATFORM)

    agency2, _ = make_active_agency(client, "Zeta Reisen", "z@zeta-mail.com")
    login(client, "z@zeta-mail.com")
    client.post("/app/publish", base_url=PLATFORM)
    assert db.session.get(Agency, agency2.id).site_published
    page = client.get("/", base_url=tenant_url(agency2.slug))
    assert page.status_code == 200 and b"Zeta Reisen" in page.data
    assert b"noindex" not in page.data
    robots = client.get("/robots.txt", base_url=tenant_url(agency2.slug)).get_data(as_text=True)
    assert "Allow: /" in robots and "Sitemap:" in robots


def test_unpublished_site_noindex_and_preview(client):
    agency, user = make_active_agency(client)
    host = tenant_url(agency.slug)
    r = client.get("/", base_url=host)
    assert r.headers.get("X-Robots-Tag") == "noindex"
    assert "Disallow: /" in client.get("/robots.txt", base_url=host).get_data(as_text=True)
    login(client)
    from itsdangerous import URLSafeTimedSerializer
    tok = URLSafeTimedSerializer("test-secret-key", salt="preview").dumps(agency.id)
    r = client.get(f"/?preview={tok}", base_url=host)
    assert r.status_code == 302
    page = client.get("/", base_url=host)
    assert b"Acme Reisen" in page.data and b"noindex" in page.data


def test_suspended_agency_offline(client):
    agency, _ = make_active_agency(client)
    agency.status, agency.site_published = "suspended", True
    db.session.commit()
    assert client.get("/", base_url=tenant_url(agency.slug)).status_code == 503


def test_custom_domain_requires_verification(client):
    agency, _ = make_active_agency(client)
    agency.custom_domain = "reisen-acme.de"
    agency.site_published = True
    db.session.commit()
    assert client.get("/", base_url="http://reisen-acme.de").status_code == 404
    agency.custom_domain_verified = True
    db.session.commit()
    assert client.get("/", base_url="http://reisen-acme.de").status_code == 200


def test_tls_ask(client):
    agency, _ = make_active_agency(client)
    ok = client.get(f"/_internal/tls-ask?token=tok&domain={agency.slug}.tp.test", base_url="http://web:8000")
    assert ok.status_code == 200
    assert client.get("/_internal/tls-ask?token=tok&domain=unknown.tp.test", base_url="http://web:8000").status_code == 404
    assert client.get("/_internal/tls-ask?token=tok&domain=www.tp.test", base_url="http://web:8000").status_code == 200
    assert client.get("/_internal/tls-ask?token=bad&domain=tp.test", base_url="http://web:8000").status_code == 403
    assert client.get("/_internal/tls-ask?token=tok&domain=evil.com", base_url="http://web:8000").status_code == 404


# ------------------------------------------------------------- чат (ИИ замокан)
@pytest.fixture()
def chat_agency(client):
    agency, _ = make_active_agency(client)
    agency.site_published = True
    agency.set_secret("anthropic", "sk-ant-" + "k" * 30)
    db.session.add_all([
        Offer(agency_id=agency.id, title="Antalya 7 Nächte", destination="Türkei", price_from=Decimal("599"),
              operator_name="TUI", is_active=True, valid_until=date.today() + timedelta(days=30), last_verified_at=utcnow()),
        Offer(agency_id=agency.id, title="Abgelaufen", destination="Türkei", price_from=Decimal("1"), is_active=True,
              valid_until=date.today() - timedelta(days=1)),
        Offer(agency_id=agency.id, title="Entwurf", destination="Türkei", is_active=False),
    ])
    db.session.commit()
    return agency


def test_chat_returns_only_real_active_offers(client, chat_agency, monkeypatch):
    calls = []

    def fake_call(agency, **kw):
        calls.append(kw)
        if len(calls) == 1:
            return fake_response([tool_block("search_offers", {"destination": "Türkei"})], "tool_use")
        return fake_response([text_block("Ich habe ein Angebot für Sie.")])

    monkeypatch.setattr(llm, "call", fake_call)
    r = client.post("/chat/message", base_url=tenant_url(chat_agency.slug), json={"message": "Urlaub in der Türkei?"})
    body = r.get_json()
    assert r.status_code == 200 and body["reply"].startswith("Ich habe")
    titles = [o["title"] for o in body["offers"]]
    assert titles == ["Antalya 7 Nächte"]  # ни просроченных, ни черновиков
    assert "Hard rules" not in calls[0]["system"] and "HARD RULES" in calls[0]["system"]
    assert "AI assistant" in calls[0]["system"]
    # история восстанавливается по session id
    hist = client.get(f"/chat/history/{body['session']}", base_url=tenant_url(chat_agency.slug)).get_json()
    assert len(hist["messages"]) == 2


def test_chat_never_leaks_other_agency_offers(client, chat_agency, monkeypatch):
    other, _ = make_active_agency(client, "Other Tours", "o@other-mail.com")
    db.session.add(Offer(agency_id=other.id, title="Other secret deal", destination="Türkei", is_active=True))
    db.session.commit()
    seen = {}

    def fake_call(agency, **kw):
        if "tool_results" not in seen:
            seen["tool_results"] = True
            return fake_response([tool_block("search_offers", {"destination": "Türkei"})], "tool_use")
        seen["messages"] = json.dumps(kw["messages"], default=str)
        return fake_response([text_block("ok")])

    monkeypatch.setattr(llm, "call", fake_call)
    client.post("/chat/message", base_url=tenant_url(chat_agency.slug), json={"message": "Türkei"})
    assert "Other secret deal" not in seen["messages"]


def test_chat_save_lead_requires_consent(client, chat_agency, monkeypatch):
    seq = iter([
        fake_response([tool_block("save_lead", {"name": "Max", "email": "max@x.de", "consent_given": False}, "t1")], "tool_use"),
        fake_response([text_block("Brauche Ihre Einwilligung.")]),
        fake_response([tool_block("save_lead", {"name": "Max", "email": "max@x.de", "consent_given": True}, "t2")], "tool_use"),
        fake_response([text_block("Gespeichert.")]),
    ])
    monkeypatch.setattr(llm, "call", lambda agency, **kw: next(seq))
    host = tenant_url(chat_agency.slug)
    r1 = client.post("/chat/message", base_url=host, json={"message": "Meine Mail max@x.de"}).get_json()
    assert Lead.query.count() == 0
    client.post("/chat/message", base_url=host, json={"message": "Ja, einverstanden", "session": r1["session"]})
    lead = Lead.query.one()
    assert lead.email == "max@x.de" and lead.consent_at and lead.source == "chat" and lead.agency_id == chat_agency.id


def test_chat_origin_and_limits(client, chat_agency, monkeypatch):
    monkeypatch.setattr(llm, "call", lambda agency, **kw: fake_response([text_block("hi")]))
    host = tenant_url(chat_agency.slug)
    bad = client.post("/chat/message", base_url=host, json={"message": "x"}, headers={"Origin": "https://evil.example"})
    assert bad.status_code == 403
    assert client.post("/chat/message", base_url=host, json={"message": "  "}).status_code == 400
    # дневной лимит (защита кошелька агента)
    chat_agency_id = chat_agency.id
    client.application.config["CHAT_DAILY_CAP_PER_AGENCY"] = 2
    db.session.add_all([UsageEvent(agency_id=chat_agency_id, kind="chat"), UsageEvent(agency_id=chat_agency_id, kind="chat")])
    db.session.commit()
    assert client.post("/chat/message", base_url=host, json={"message": "hello"}).status_code == 429


def test_chat_session_limit(client, chat_agency, monkeypatch):
    monkeypatch.setattr(llm, "call", lambda agency, **kw: fake_response([text_block("hi")]))
    client.application.config["CHAT_MAX_USER_MESSAGES_PER_SESSION"] = 2
    host = tenant_url(chat_agency.slug)
    sid = client.post("/chat/message", base_url=host, json={"message": "1"}).get_json()["session"]
    client.post("/chat/message", base_url=host, json={"message": "2", "session": sid})
    assert client.post("/chat/message", base_url=host, json={"message": "3", "session": sid}).status_code == 429


def test_chat_unavailable_without_key(client):
    agency, _ = make_active_agency(client)
    agency.site_published = True
    db.session.commit()
    assert client.post("/chat/message", base_url=tenant_url(agency.slug), json={"message": "hi"}).status_code == 503
    assert b"cw-root" not in client.get("/", base_url=tenant_url(agency.slug)).data


def test_chat_session_belongs_to_agency(client, chat_agency, monkeypatch):
    monkeypatch.setattr(llm, "call", lambda agency, **kw: fake_response([text_block("hi")]))
    other, _ = make_active_agency(client, "Other Tours", "o@other-mail.com")
    other.site_published = True
    other.set_secret("anthropic", "sk-ant-" + "z" * 30)
    db.session.commit()
    sid = client.post("/chat/message", base_url=tenant_url(chat_agency.slug), json={"message": "a"}).get_json()["session"]
    # чужой хост не видит историю и не продолжает сессию
    assert client.get(f"/chat/history/{sid}", base_url=tenant_url(other.slug)).get_json()["messages"] == []
    new_sid = client.post("/chat/message", base_url=tenant_url(other.slug), json={"message": "b", "session": sid}).get_json()["session"]
    assert new_sid != sid


# --------------------------------------------------------------------- блог
def _post_response(body):
    return fake_response([tool_block("submit_post", {
        "title": "Türkei im Herbst", "meta_description": "Tipps", "excerpt": "Kurz", "body_markdown": body,
        "tags": ["Türkei", "Herbst"]})], "tool_use")


def test_blog_generation_sanitizes_output(client, monkeypatch):
    agency, _ = make_active_agency(client)
    agency.set_secret("anthropic", "sk-ant-" + "k" * 30)
    agency.blog_topics = "Türkei im Herbst\nSpanien im Frühling"
    db.session.commit()
    evil = "## Titel\n<script>alert(1)</script>\n[klick](javascript:alert(1))\n<img src=x onerror=alert(2)>\n\nText **fett** [ok](https://example.com)"
    monkeypatch.setattr(llm, "call", lambda agency, **kw: _post_response(evil))
    post = blog.generate_post(agency)
    assert post.status == "draft" and post.generated_by_ai and post.topic == "Türkei im Herbst"
    html = render_markdown(post.body_md)
    assert "<script" not in html and "javascript:" not in html and "onerror" not in html and "<img" not in html
    assert 'rel="noopener noreferrer nofollow"' in html and "<strong>fett</strong>" in html
    # вторая генерация берёт следующую тему
    post2 = blog.generate_post(agency)
    assert post2.topic == "Spanien im Frühling" and post2.slug != post.slug


def test_blog_auto_mode_publishes_only_when_site_live(client, monkeypatch):
    agency, _ = make_active_agency(client)
    agency.blog_topics = "A\nB"
    agency.blog_mode = "auto"
    db.session.commit()
    monkeypatch.setattr(llm, "call", lambda agency, **kw: _post_response("Text"))
    assert blog.generate_post(agency).status == "draft"
    agency.site_published = True
    db.session.commit()
    assert blog.generate_post(agency).status == "published"


def test_public_blog_only_shows_published(client):
    agency, _ = make_active_agency(client)
    agency.site_published = True
    db.session.add_all([
        BlogPost(agency_id=agency.id, slug="pub", title="Public post", body_md="Hello", status="published", published_at=utcnow()),
        BlogPost(agency_id=agency.id, slug="draft", title="Draft post", body_md="Secret", status="draft")])
    db.session.commit()
    host = tenant_url(agency.slug)
    assert b"Public post" in client.get("/blog", base_url=host).data
    assert b"Draft post" not in client.get("/blog", base_url=host).data
    assert client.get("/blog/draft", base_url=host).status_code == 404
    assert client.get("/blog/pub", base_url=host).status_code == 200
    assert b"/blog/pub" in client.get("/sitemap.xml", base_url=host).data
    assert b"<rss" in client.get("/blog/feed.xml", base_url=host).data


def test_worker_schedule_respects_interval(app, client, monkeypatch):
    agency, _ = make_active_agency(client)
    agency.set_secret("anthropic", "sk-ant-" + "k" * 30)
    agency.blog_enabled, agency.blog_topics, agency.blog_posts_per_week = True, "T1\nT2\nT3", 2
    db.session.commit()
    monkeypatch.setattr(llm, "call", lambda agency, **kw: _post_response("Text"))
    assert blog.run_due_jobs(app) == 1
    assert blog.run_due_jobs(app) == 0  # интервал 3.5 дня ещё не прошёл
    agency.last_blog_run_at = utcnow() - timedelta(days=4)
    db.session.commit()
    assert blog.run_due_jobs(app) == 1


def test_blog_failure_does_not_crash_worker(app, client, monkeypatch):
    agency, _ = make_active_agency(client)
    agency.set_secret("anthropic", "sk-ant-" + "k" * 30)
    agency.blog_enabled, agency.blog_topics = True, "T1"
    db.session.commit()

    def boom(agency, **kw):
        raise llm.LLMError("Ключ Anthropic недействителен.")
    monkeypatch.setattr(llm, "call", boom)
    assert blog.run_due_jobs(app) == 0
    assert "недействителен" in Agency.query.first().last_blog_error


# ------------------------------------------------------- Stripe и платежи
def _booking(agency, **kw):
    lead = Lead(agency_id=agency.id, name="Kunde", email="k@x.de")
    db.session.add(lead)
    db.session.flush()
    b = Booking(agency_id=agency.id, lead_id=lead.id, title="Antalya", amount=Decimal("1200.50"), operator_name="TUI", **kw)
    db.session.add(b)
    db.session.commit()
    return b


def test_checkout_requires_sicherungsschein_for_packages(client, monkeypatch):
    agency, _ = make_active_agency(client)
    agency.set_secret("stripe_secret", "rk_test_" + "a" * 30)
    b = _booking(agency, kind="package")
    captured = {}

    class FakeSessions:
        def create(self, params):
            captured.update(params)
            return type("S", (), {"id": "cs_1", "url": "https://checkout.stripe.com/c/pay/cs_1"})()
    monkeypatch.setattr(stripe_service, "_client", lambda a: type("C", (), {"checkout": type("X", (), {"sessions": FakeSessions()})()})())
    with pytest.raises(stripe_service.PaymentError):
        stripe_service.create_checkout(agency, b)
    b.sicherungsschein_confirmed = True
    db.session.commit()
    url = stripe_service.create_checkout(agency, b, "k@x.de")
    assert url.startswith("https://checkout.stripe.com") and b.status == "awaiting_payment"
    assert captured["line_items"][0]["price_data"]["unit_amount"] == 120050
    assert captured["metadata"] == {"booking_id": str(b.id), "agency_id": str(agency.id)}
    assert captured["success_url"].startswith(f"http://{agency.slug}.tp.test")


def _signed(secret, payload):
    ts = int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def _event(booking_id, session_id="cs_1", agency_id=1):
    return json.dumps({"id": "evt_1", "object": "event", "type": "checkout.session.completed", "data": {"object": {
        "id": session_id, "object": "checkout.session", "payment_status": "paid",
        "metadata": {"booking_id": str(booking_id), "agency_id": str(agency_id)}}}}).encode()


def test_stripe_webhook_verifies_signature_and_tenant(client):
    a, _ = make_active_agency(client, "Alpha Tours", "a@alpha-mail.com")
    b_, _ = make_active_agency(client, "Beta Reisen", "b@beta-mail.com")
    for ag in (a, b_):
        ag.set_secret("stripe_webhook", "whsec_" + "s" * 30 + str(ag.id))
        ag.site_published = True
    booking = _booking(a, kind="flight")
    booking.stripe_session_id = "cs_1"
    db.session.commit()
    payload = _event(booking.id)
    secret_a = a.get_secret("stripe_webhook")

    # неверная подпись
    r = client.post("/webhooks/stripe", base_url=tenant_url(a.slug), data=payload, headers={"Stripe-Signature": _signed("whsec_wrong", payload)})
    assert r.status_code == 400 and db.session.get(Booking, booking.id).status != "paid"
    # событие, доставленное на домен ДРУГОГО агентства (с его секретом), не затрагивает чужую бронь
    r = client.post("/webhooks/stripe", base_url=tenant_url(b_.slug), data=payload, headers={"Stripe-Signature": _signed(b_.get_secret("stripe_webhook"), payload)})
    assert r.status_code == 200 and db.session.get(Booking, booking.id).status != "paid"
    # корректное событие
    r = client.post("/webhooks/stripe", base_url=tenant_url(a.slug), data=payload, headers={"Stripe-Signature": _signed(secret_a, payload)})
    assert r.status_code == 200
    db.session.expire_all()
    assert db.session.get(Booking, booking.id).status == "paid"


def test_stripe_webhook_without_secret(client):
    a, _ = make_active_agency(client)
    a.site_published = True
    db.session.commit()
    assert client.post("/webhooks/stripe", base_url=tenant_url(a.slug), data=b"{}", headers={"Stripe-Signature": "x"}).status_code == 400


# ------------------------------------------------------------------- документы
def test_pdf_documents_with_cyrillic(client, app):
    agency, _ = make_active_agency(client, "Рейзен Агентство", "ru@acme-mail.com")
    b = _booking(agency, kind="package")
    b.destination, b.travel_start, b.travel_end = "Анталья", date(2026, 11, 1), date(2026, 11, 8)
    db.session.commit()
    for lang in ("de", "ru", "uk", "en"):
        name = documents.build_proposal(agency, b, b.lead, lang)
        path = app.config["UPLOAD_DIR"] / str(agency.id) / "docs" / name
        assert path.read_bytes().startswith(b"%PDF") and path.stat().st_size > 1500
    name = documents.build_markdown_pdf(agency, "## Заголовок\n- пункт **жирный**\n\nТекст [ссылка](https://example.com)", "ru")
    assert (app.config["UPLOAD_DIR"] / str(agency.id) / "docs" / name).stat().st_size > 1000


def test_documents_download_scoped(client, app):
    a, _ = make_active_agency(client, "Alpha Tours", "a@alpha-mail.com")
    b_ = _booking(a, kind="flight")
    login(client, "a@alpha-mail.com")
    client.post(f"/app/bookings/{b_.id}/docs", base_url=PLATFORM, data={"kind": "proposal", "lang": "de"})
    from app.models import Document
    doc = Document.query.one()
    assert client.get(f"/app/documents/{doc.id}", base_url=PLATFORM).status_code == 200
    client.post("/logout", base_url=PLATFORM)
    register(client, "Beta Reisen", "b@beta-mail.com")
    assert client.get(f"/app/documents/{doc.id}", base_url=PLATFORM).status_code == 404


def test_document_requires_operator(client):
    agency, _ = make_active_agency(client)
    b = _booking(agency, kind="package")
    b.operator_name = None
    db.session.commit()
    login(client)
    client.post(f"/app/bookings/{b.id}/docs", base_url=PLATFORM, data={"kind": "proposal", "lang": "de"})
    from app.models import Document
    assert Document.query.count() == 0


# ------------------------------------------------------------------ безопасность
@pytest.mark.parametrize("url", [
    "http://127.0.0.1/feed", "http://localhost/feed", "http://10.0.0.5/x", "http://192.168.1.1/",
    "http://169.254.169.254/latest/meta-data/", "http://[::1]/", "file:///etc/passwd", "ftp://example.com/",
    "http://example.com:8080/", "http://user:pw@example.com/", "http://0.0.0.0/",
])
def test_ssrf_guard_blocks(url):
    with pytest.raises(UnsafeURL):
        assert_public_url(url)


def _png(size=(10, 10)):
    buf = io.BytesIO()
    Image.new("RGB", size, "red").save(buf, "PNG")
    buf.seek(0)
    return buf


def test_upload_accepts_image_and_rejects_fake(client, app):
    register(client)
    agency = Agency.query.first()
    r = client.post("/app/brand", base_url=PLATFORM, data={"name": "Acme", "languages": ["de"], "default_language": "de",
                    "primary_color": "#112233", "logo": (_png(), "logo.png")}, content_type="multipart/form-data")
    assert r.status_code == 302
    db.session.expire_all()
    logo = db.session.get(Agency, agency.id).logo_file
    assert logo.endswith(".webp") and uploads.image_path(agency.id, logo)
    client.post("/app/brand", base_url=PLATFORM, data={"name": "Acme", "languages": ["de"], "default_language": "de",
                "hero": (io.BytesIO(b"<?php system($_GET['x']); ?>"), "hero.png")}, content_type="multipart/form-data")
    db.session.expire_all()
    assert db.session.get(Agency, agency.id).hero_file is None


def test_media_path_traversal_blocked(client):
    agency, _ = make_active_agency(client)
    agency.site_published = True
    db.session.commit()
    host = tenant_url(agency.slug)
    for name in ("../../etc/passwd", "..%2f..%2fetc%2fpasswd", "x.png", "a/b.webp"):
        assert client.get(f"/media/{name}", base_url=host).status_code == 404
    assert uploads.image_path(agency.id, "../x.webp") is None


def test_xss_in_contact_form_is_neutralised(client):
    agency, _ = make_active_agency(client)
    agency.site_published = True
    db.session.commit()
    r = client.post("/contact", base_url=tenant_url(agency.slug), data={
        "name": "<b>Eve</b>", "email": "eve@x.de", "message": "<script>alert(1)</script>Hallo", "consent": "on"})
    assert r.status_code == 200
    lead = Lead.query.one()
    assert "<script" not in lead.message
    login(client)
    page = client.get(f"/app/leads/{lead.id}", base_url=PLATFORM).get_data(as_text=True)
    assert "<script>alert" not in page and "&lt;b&gt;Eve&lt;/b&gt;" in page  # имя экранировано


def test_contact_form_requires_consent_and_honeypot(client):
    agency, _ = make_active_agency(client)
    agency.site_published = True
    db.session.commit()
    host = tenant_url(agency.slug)
    assert client.post("/contact", base_url=host, data={"name": "A", "email": "a@x.de", "message": "m"}).status_code == 400
    assert client.post("/contact", base_url=host, data={"name": "A", "email": "a@x.de", "message": "m", "consent": "on", "website": "spam"}).status_code == 400
    assert Lead.query.count() == 0


def test_sql_injection_attempts_are_inert(client):
    agency, _ = make_active_agency(client)
    db.session.add(Lead(agency_id=agency.id, name="Anna"))
    db.session.commit()
    login(client)
    for payload in ("' OR '1'='1", "'; DROP TABLE leads;--", "%' UNION SELECT * FROM users--"):
        r = client.get("/app/leads", base_url=PLATFORM, query_string={"q": payload, "status": payload})
        assert r.status_code == 200
    assert Lead.query.count() == 1


def test_csrf_enforced_when_enabled(app):
    app.config["WTF_CSRF_ENABLED"] = True
    c = app.test_client()
    r = c.post("/login", base_url=PLATFORM, data={"email": "a@b.de", "password": "x"})
    assert r.status_code == 400  # нет CSRF-токена


def test_password_change_invalidates_sessions(client):
    register(client)
    other = client.application.test_client()
    login(other)
    assert other.get("/app/", base_url=PLATFORM).status_code == 200
    client.post("/app/account", base_url=PLATFORM, data={"action": "password", "current": "correct horse battery", "new": "another very long pass"})
    assert other.get("/app/", base_url=PLATFORM).status_code == 302  # старая сессия аннулирована


def test_password_reset_single_use(client, monkeypatch):
    register(client)
    client.post("/logout", base_url=PLATFORM)
    sent = {}
    monkeypatch.setattr("app.blueprints.auth.send_mail", lambda to, subj, body: sent.update(body=body))
    client.post("/forgot", base_url=PLATFORM, data={"email": "owner@acme-mail.com"})
    token = sent["body"].split("/reset/")[1].split()[0]
    assert client.post(f"/reset/{token}", base_url=PLATFORM, data={"password": "brand new password!"}).status_code == 200
    assert client.post(f"/reset/{token}", base_url=PLATFORM, data={"password": "second try password"}).status_code == 400
    assert login(client, password="brand new password!").status_code == 302


def test_forgot_does_not_reveal_accounts(client):
    a = client.post("/forgot", base_url=PLATFORM, data={"email": "ghost@x.de"})
    register(client)
    client.post("/logout", base_url=PLATFORM)
    b = client.post("/forgot", base_url=PLATFORM, data={"email": "owner@acme-mail.com"})
    assert a.status_code == b.status_code and a.get_data() == b.get_data()


# ------------------------------------------------- прочее: импорт, отзывы, DSGVO
def test_offer_import_creates_inactive_offers(client, monkeypatch):
    agency, _ = make_active_agency(client)
    monkeypatch.setattr(llm, "call", lambda agency, **kw: fake_response([tool_block("submit_offers", {"offers": [
        {"title": "<b>Hotel Sol</b>", "price_from": 499, "currency": "eur", "valid_until": "2026-12-31", "url": "javascript:alert(1)", "kind": "package"},
        {"title": "Flug", "price_from": None, "kind": "weird"}]})], "tool_use"))
    created = offer_import.import_from_text(agency, "x" * 50)
    assert len(created) == 2 and all(not o.is_active and o.source == "ai_import" for o in created)
    assert created[0].title == "Hotel Sol" and created[0].url is None and created[0].currency == "EUR"
    assert created[1].kind == "package"


def test_review_flow_and_moderation(client):
    agency, _ = make_active_agency(client)
    agency.site_published = True
    b = _booking(agency, kind="flight")
    b.status = "confirmed"
    db.session.commit()
    host = tenant_url(agency.slug)
    assert client.get(f"/review/{b.review_token}", base_url=host).status_code == 200
    assert client.get("/review/doesnotexist", base_url=host).status_code == 404
    client.post(f"/review/{b.review_token}", base_url=host, data={"name": "Anna", "rating": "5", "text": "Super <script>x</script>"})
    review = Review.query.one()
    assert not review.approved and "<script" not in review.text
    assert b"Anna" not in client.get("/", base_url=host).data  # до модерации не виден
    login(client)
    client.post(f"/app/reviews/{review.id}/approve", base_url=PLATFORM)
    assert b"Anna" in client.get("/", base_url=host).data
    # повторный отзыв на ту же бронь невозможен
    client.post(f"/review/{b.review_token}", base_url=host, data={"name": "Bob", "rating": "1"})
    assert Review.query.count() == 1


def test_review_link_blocked_for_other_agency(client):
    a, _ = make_active_agency(client, "Alpha Tours", "a@alpha-mail.com")
    b_, _ = make_active_agency(client, "Beta Reisen", "b@beta-mail.com")
    b_.site_published = True
    booking = _booking(a, kind="flight")
    booking.status = "confirmed"
    db.session.commit()
    assert client.get(f"/review/{booking.review_token}", base_url=tenant_url(b_.slug)).status_code == 404


def test_lead_delete_removes_everything(client, app):
    agency, _ = make_active_agency(client)
    b = _booking(agency, kind="flight")
    lead_id = b.lead_id
    s = ChatSession(agency_id=agency.id, lead_id=lead_id)
    db.session.add(s)
    db.session.commit()
    login(client)
    client.post(f"/app/bookings/{b.id}/docs", base_url=PLATFORM, data={"kind": "proposal", "lang": "de"})
    docs_dir = app.config["UPLOAD_DIR"] / str(agency.id) / "docs"
    assert any(docs_dir.iterdir())
    client.post(f"/app/leads/{lead_id}/delete", base_url=PLATFORM)
    db.session.expire_all()
    assert Lead.query.count() == 0 and Booking.query.count() == 0 and ChatSession.query.count() == 0
    assert not any(docs_dir.iterdir())


def test_admin_approval_flow(client):
    register(client, "Acme Reisen", "owner@acme-mail.com")
    admin = User(email="root@tp-mail.com", is_superadmin=True, email_confirmed_at=utcnow())
    admin.set_password("super long admin pass")
    db.session.add(admin)
    db.session.commit()
    c2 = client.application.test_client()
    assert login(c2, "owner@acme-mail.com", "correct horse battery").status_code == 302
    assert c2.get("/admin/", base_url=PLATFORM).status_code == 403  # обычный пользователь — нет
    c3 = client.application.test_client()
    from flask import g
    g.pop("_login_user", None)  # общий app-context теста кэширует current_user
    login(c3, "root@tp-mail.com", "super long admin pass")
    agency = Agency.query.first()
    assert c3.get("/admin/", base_url=PLATFORM).status_code == 200
    c3.post(f"/admin/agencies/{agency.id}/status", base_url=PLATFORM, data={"status": "active"})
    assert db.session.get(Agency, agency.id).status == "active"
    assert c3.post(f"/admin/agencies/{agency.id}/status", base_url=PLATFORM, data={"status": "bogus"}).status_code == 400


def test_reserved_slug_not_assignable(client):
    register(client, name="Admin", email="adm@x-mail.com")
    assert Agency.query.first().slug != "admin"


def test_theme_css_rejects_injection(client):
    agency, _ = make_active_agency(client)
    agency.primary_color = "red;}<s"
    agency.site_published = True
    db.session.commit()
    css = client.get("/theme.css", base_url=tenant_url(agency.slug)).get_data(as_text=True)
    assert "<script" not in css and "--brand:#0b6cff" in css


def test_production_config_guard():
    from app import create_app
    with pytest.raises(RuntimeError):
        create_app({"APP_ENV": "production", "SQLALCHEMY_DATABASE_URI": "sqlite://"})
