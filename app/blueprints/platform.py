"""Публичная часть платформы: лендинг, юридические страницы, служебные эндпоинты."""
import secrets

from flask import Blueprint, abort, current_app, g, jsonify, render_template, request

from ..extensions import csrf, db
from ..tenancy import agency_from_host_for_tls

bp = Blueprint("platform", __name__)


def landing():
    return render_template("platform/landing.html")


@bp.route("/")
def index():
    if g.agency is not None:
        from .site import home
        return home()
    return landing()


@bp.route("/legal/<page>")
def legal(page):
    if not g.is_platform or page not in ("impressum", "datenschutz", "agb", "avv"):
        abort(404)
    return render_template("platform/legal.html", page=page)


@bp.route("/_internal/health")
@csrf.exempt
def health():
    try:
        db.session.execute(db.text("SELECT 1"))
    except Exception:
        return jsonify(status="db_error"), 503
    return jsonify(status="ok")


@bp.route("/_internal/tls-ask")
@csrf.exempt
def tls_ask():
    """Caddy on-demand TLS: выпускать сертификат только для известных доменов."""
    expected = current_app.config["INTERNAL_TOKEN"]
    if not expected or not secrets.compare_digest(request.args.get("token", ""), expected):
        abort(403)
    return ("", 200) if agency_from_host_for_tls(request.args.get("domain", "")) else ("", 404)
