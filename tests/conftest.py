import os
from types import SimpleNamespace

import pytest

from app import create_app
from app.extensions import db
from app.models import Agency, User
from app.utils import utcnow

PLATFORM = "http://tp.test"


def tenant_url(slug):
    return f"http://{slug}.tp.test"


@pytest.fixture()
def app(tmp_path):
    url = os.environ.get("TEST_DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    app = create_app({
        "TESTING": True, "SQLALCHEMY_DATABASE_URI": url, "WTF_CSRF_ENABLED": False, "RATELIMIT_ENABLED": False,
        "BASE_DOMAIN": "tp.test", "PLATFORM_EXTRA_HOSTS": [], "UPLOAD_DIR": tmp_path / "uploads",
        "SECRET_KEY": "test-secret-key", "URL_SCHEME": "http", "PUBLIC_PORT": "", "INTERNAL_TOKEN": "tok",
        "REQUIRE_MANUAL_APPROVAL": True,
    })
    with app.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


def register(client, name="Acme Reisen", email="owner@acme-mail.com", password="correct horse battery"):
    return client.post("/register", base_url=PLATFORM, data={
        "agency_name": name, "email": email, "password": password, "country": "DE", "accept_terms": "on"})


def login(client, email="owner@acme-mail.com", password="correct horse battery"):
    return client.post("/login", base_url=PLATFORM, data={"email": email, "password": password})


def make_active_agency(client, name="Acme Reisen", email="owner@acme-mail.com"):
    """Регистрирует агентство, заполняет юр. данные, одобряет и подтверждает e-mail."""
    register(client, name, email)
    user = User.query.filter_by(email=email).first()
    agency = db.session.get(Agency, user.agency_id)
    agency.legal_name, agency.owner_name = name, "Max Muster"
    agency.street, agency.zip_code, agency.city, agency.country = "Hauptstr. 1", "60311", "Frankfurt", "DE"
    agency.phone, agency.contact_email = "+49 69 123", email
    agency.vat_id = "DE123456789"
    agency.insurance_text = "Muster Versicherung AG"
    agency.terms_md = "## AGB\nText"
    agency.status = "active"
    user.email_confirmed_at = utcnow()
    db.session.commit()
    return agency, user


class Block(SimpleNamespace):
    pass


def fake_response(content, stop_reason="end_turn"):
    return SimpleNamespace(content=content, stop_reason=stop_reason, usage=SimpleNamespace(input_tokens=10, output_tokens=5))


def text_block(text):
    return Block(type="text", text=text)


def tool_block(name, input_, id_="tu_1"):
    return Block(type="tool_use", name=name, input=input_, id=id_)
