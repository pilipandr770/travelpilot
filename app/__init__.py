import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import click
from flask import Flask, g, jsonify, render_template, request
from werkzeug.middleware.proxy_fix import ProxyFix

from .config import Config
from .extensions import csrf, db, limiter, login_manager, migrate


def create_app(overrides: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_object(Config)
    if overrides:
        app.config.update(overrides)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    _check_production(app)
    Path(app.config["UPLOAD_DIR"]).mkdir(parents=True, exist_ok=True)
    if app.config["TRUSTED_PROXY_COUNT"]:
        n = app.config["TRUSTED_PROXY_COUNT"]
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=n, x_proto=n)

    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)

    from . import models  # noqa: F401
    from .i18n import init_i18n
    from .security import init_security
    from .tenancy import init_tenancy

    init_tenancy(app)   # порядок важен: сначала арендатор,
    init_i18n(app)      # затем язык
    init_security(app)

    from .blueprints.admin import bp as admin_bp
    from .blueprints.auth import bp as auth_bp
    from .blueprints.dash import bp as dash_bp
    from .blueprints.platform import bp as platform_bp
    from .blueprints.site import bp as site_bp
    for bp in (platform_bp, auth_bp, dash_bp, admin_bp, site_bp):
        app.register_blueprint(bp)

    @login_manager.user_loader
    def load_user(user_id):
        try:
            uid, version = user_id.split(".")
            user = db.session.get(models.User, int(uid))
        except (ValueError, AttributeError):
            return None
        return user if user and user.active and user.session_version == int(version) else None

    @app.context_processor
    def _inject():
        return {"cfg": app.config, "now_year": datetime.now(timezone.utc).year}

    @app.errorhandler(404)
    def _404(_e):
        if request.path.startswith("/chat/"):
            return jsonify(error="not_found"), 404
        return render_template("errors/error.html", code=404, message="Seite nicht gefunden"), 404

    @app.errorhandler(403)
    def _403(_e):
        return render_template("errors/error.html", code=403, message="Zugriff verweigert"), 403

    @app.errorhandler(413)
    def _413(_e):
        return render_template("errors/error.html", code=413, message="Datei zu groß (max. 12 MB)"), 413

    @app.errorhandler(429)
    def _429(_e):
        if request.path.startswith("/chat/"):
            return jsonify(error="rate_limited"), 429
        return render_template("errors/error.html", code=429, message="Zu viele Anfragen – bitte später erneut versuchen"), 429

    @app.errorhandler(500)
    def _500(_e):
        db.session.rollback()
        return render_template("errors/error.html", code=500, message="Interner Fehler"), 500

    _register_cli(app)
    return app


def _check_production(app: Flask):
    if app.config["APP_ENV"] != "production":
        return
    problems = []
    if app.config["SECRET_KEY"].startswith("dev-only"):
        problems.append("SECRET_KEY не задан")
    if not app.config["ENCRYPTION_KEYS"]:
        problems.append("ENCRYPTION_KEYS не задан")
    if not app.config["INTERNAL_TOKEN"]:
        problems.append("INTERNAL_TOKEN не задан")
    if app.config["URL_SCHEME"] != "https":
        problems.append("URL_SCHEME должен быть https")
    if problems:
        raise RuntimeError("Небезопасная конфигурация продакшена: " + "; ".join(problems))


def _register_cli(app: Flask):
    @app.cli.command("create-admin")
    @click.argument("email")
    @click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True)
    def create_admin(email, password):
        """Создать суперадминистратора платформы (одобряет агентства)."""
        from .models import User
        email = email.strip().lower()
        if len(password) < 12:
            raise click.ClickException("Пароль минимум 12 символов")
        user = User.query.filter_by(email=email).first()
        if user:
            user.is_superadmin = True
            user.set_password(password)
        else:
            user = User(email=email, is_superadmin=True, email_confirmed_at=datetime.utcnow(), ui_language="ru")
            user.set_password(password)
            db.session.add(user)
        db.session.commit()
        click.echo(f"Суперадмин: {email}")

    @app.cli.command("gen-keys")
    def gen_keys():
        """Сгенерировать SECRET_KEY, ENCRYPTION_KEYS, INTERNAL_TOKEN для .env"""
        import secrets

        from .crypto import generate_key
        click.echo(f"SECRET_KEY={secrets.token_urlsafe(48)}")
        click.echo(f"ENCRYPTION_KEYS={generate_key()}")
        click.echo(f"INTERNAL_TOKEN={secrets.token_urlsafe(32)}")

    @app.cli.command("run-blog-jobs")
    def run_blog_jobs():
        """Один прогон планировщика блога (то же, что делает воркер)."""
        from .services.blog import run_due_jobs
        click.echo(f"Создано статей: {run_due_jobs(app)}")

    @app.cli.command("purge-chats")
    @click.option("--days", default=90)
    def purge_chats(days):
        """Удалить чаты старше N дней (DSGVO: ограничение срока хранения)."""
        from datetime import timedelta

        from .models import ChatSession
        limit = datetime.utcnow() - timedelta(days=days)
        old = ChatSession.query.filter(ChatSession.last_message_at < limit).all()
        for s in old:
            db.session.delete(s)
        db.session.commit()
        click.echo(f"Удалено чатов: {len(old)}")
