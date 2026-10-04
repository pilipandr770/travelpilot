"""Определение арендатора (агентства) по Host.

  BASE_DOMAIN              → платформа (лендинг, регистрация, кабинет /app)
  <slug>.BASE_DOMAIN       → публичный сайт агентства
  <custom-domain>          → публичный сайт агентства (только если домен подтверждён)
"""
from flask import current_app, g, render_template, request

from .models import Agency
from .utils import RESERVED_SLUGS

_SKIP_PREFIXES = ("/_internal/", "/static/")


def init_tenancy(app):
    @app.before_request
    def _resolve():
        g.agency = None
        g.is_platform = False
        g.tenant_unknown = False
        if request.path.startswith(_SKIP_PREFIXES):
            return None
        cfg = current_app.config
        host = request.host.split(":")[0].lower().rstrip(".")
        base = cfg["BASE_DOMAIN"]
        if host == base or host == f"www.{base}" or host in cfg["PLATFORM_EXTRA_HOSTS"]:
            g.is_platform = True
            return None
        if host.endswith("." + base):
            slug = host[: -(len(base) + 1)]
            if "." not in slug and slug not in RESERVED_SLUGS:
                g.agency = Agency.query.filter_by(slug=slug).first()
        else:
            g.agency = Agency.query.filter_by(custom_domain=host, custom_domain_verified=True).first()
        if g.agency is None:
            g.tenant_unknown = True
            return render_template("errors/unknown_site.html"), 404
        if g.agency.status == "suspended":
            return render_template("errors/suspended.html"), 503
        return None


def agency_from_host_for_tls(domain: str) -> bool:
    """True, если для домена можно выпускать сертификат (Caddy on-demand TLS ask)."""
    cfg = current_app.config
    domain = (domain or "").lower().rstrip(".")
    base = cfg["BASE_DOMAIN"]
    if domain == base or domain == f"www.{base}":
        return True
    if domain.endswith("." + base):
        slug = domain[: -(len(base) + 1)]
        if "." in slug or slug in RESERVED_SLUGS:
            return False
        return Agency.query.filter_by(slug=slug).first() is not None
    return Agency.query.filter_by(custom_domain=domain, custom_domain_verified=True).first() is not None
