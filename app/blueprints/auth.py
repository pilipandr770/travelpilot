"""Регистрация, вход, подтверждение e-mail, сброс пароля."""
from datetime import timedelta

from email_validator import EmailNotValidError, validate_email
from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, url_for
from flask_login import current_user, login_user, logout_user
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from ..extensions import db, limiter
from ..models import Agency, User
from ..security import audit
from ..services.mailer import send_mail
from ..utils import make_slug, safe_next_url, utcnow, RESERVED_SLUGS

bp = Blueprint("auth", __name__)

MIN_PASSWORD = 12
MAX_FAILED = 5
LOCK_MINUTES = 15


@bp.before_request
def _platform_only():
    if not g.is_platform:
        abort(404)


def _serializer(salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt=salt)


def _platform_url(path: str) -> str:
    cfg = current_app.config
    port = cfg["PUBLIC_PORT"]
    host = cfg["BASE_DOMAIN"] + (f":{port}" if port and port not in ("80", "443") else "")
    return f"{cfg['URL_SCHEME']}://{host}{path}"


def send_confirmation(user: User):
    tok = _serializer("confirm-email").dumps({"id": user.id, "email": user.email})
    send_mail(user.email, "Bitte bestätigen Sie Ihre E-Mail / Подтвердите e-mail",
              f"Link / Ссылка (3 Tage / 3 дня):\n{_platform_url('/confirm/' + tok)}\n")


def _unique_slug(name: str) -> str:
    base = make_slug(name, 40)
    if base in RESERVED_SLUGS or len(base) < 3:
        base = f"{base}-reisen" if len(base) >= 1 else "agentur"
    slug, n = base, 2
    while Agency.query.filter_by(slug=slug).first() or slug in RESERVED_SLUGS:
        slug, n = f"{base}-{n}", n + 1
    return slug


@bp.route("/register", methods=["GET", "POST"])
@limiter.limit("5 per hour", methods=["POST"])
def register():
    if not current_app.config["REGISTRATION_OPEN"]:
        return render_template("auth/message.html", title="Registrierung geschlossen",
                               text="Die Registrierung ist derzeit nur auf Einladung möglich."), 403
    if request.method == "POST":
        if request.form.get("website"):  # honeypot
            abort(400)
        name = (request.form.get("agency_name") or "").strip()[:120]
        password = request.form.get("password") or ""
        country = (request.form.get("country") or "DE").upper()[:2]
        errors = []
        try:
            email = validate_email(request.form.get("email", ""), check_deliverability=False).normalized.lower()
        except EmailNotValidError:
            email = ""
            errors.append("Некорректный e-mail")
        if len(name) < 2:
            errors.append("Укажите название агентства")
        if len(password) < MIN_PASSWORD:
            errors.append(f"Пароль — минимум {MIN_PASSWORD} символов")
        if email and password.lower() == email:
            errors.append("Пароль не должен совпадать с e-mail")
        if not request.form.get("accept_terms"):
            errors.append("Нужно принять условия платформы и AVV")
        if email and User.query.filter_by(email=email).first():
            errors.append("Этот e-mail уже зарегистрирован")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("auth/register.html", form=request.form), 400
        agency = Agency(
            slug=_unique_slug(name), name=name, country=country, status="sandbox",
            trial_ends_at=utcnow() + timedelta(days=14), accepted_terms_at=utcnow(),
            contact_email=email, legal_name=name,
            bot_greeting="Hallo! Wohin soll die Reise gehen?", bot_name="Reise-Assistent",
        )
        db.session.add(agency)
        db.session.flush()
        user = User(agency_id=agency.id, email=email, ui_language="ru")
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        audit("register", agency_id=agency.id, user_id=user.id)
        send_confirmation(user)
        login_user(user)
        flash("Регистрация завершена. Подтвердите e-mail по ссылке из письма.", "success")
        return redirect(url_for("dash.index"))
    return render_template("auth/register.html", form={})


@bp.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dash.index"))
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        user = User.query.filter_by(email=email).first()
        generic = "Неверный e-mail или пароль"
        if user and user.is_locked():
            flash("Аккаунт временно заблокирован из-за неудачных попыток. Попробуйте позже.", "error")
            return render_template("auth/login.html"), 429
        if not user or not user.active or not user.check_password(password):
            if user:
                user.failed_logins += 1
                if user.failed_logins >= MAX_FAILED:
                    user.locked_until = utcnow() + timedelta(minutes=LOCK_MINUTES)
                    user.failed_logins = 0
                db.session.commit()
                audit("login_failed", agency_id=user.agency_id, user_id=user.id)
            flash(generic, "error")
            return render_template("auth/login.html"), 401
        user.failed_logins = 0
        user.locked_until = None
        user.last_login_at = utcnow()
        db.session.commit()
        login_user(user)
        audit("login", agency_id=user.agency_id, user_id=user.id)
        dest = url_for("admin.index") if user.is_superadmin and not user.agency_id else url_for("dash.index")
        return redirect(safe_next_url(request.args.get("next"), dest))
    return render_template("auth/login.html")


@bp.route("/logout", methods=["POST"])
def logout():
    logout_user()
    return redirect(url_for("platform.index"))


@bp.route("/confirm/<token>")
def confirm(token):
    try:
        data = _serializer("confirm-email").loads(token, max_age=3 * 24 * 3600)
    except (BadSignature, SignatureExpired):
        return render_template("auth/message.html", title="Link ungültig", text="Der Link ist ungültig oder abgelaufen."), 400
    user = db.session.get(User, data["id"])
    if not user or user.email != data["email"]:
        abort(400)
    user.email_confirmed_at = user.email_confirmed_at or utcnow()
    db.session.commit()
    return render_template("auth/message.html", title="E-Mail bestätigt / e-mail подтверждён", text="OK", link=True)


@bp.route("/forgot", methods=["GET", "POST"])
@limiter.limit("5 per hour", methods=["POST"])
def forgot():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        user = User.query.filter_by(email=email).first()
        if user and user.active:
            tok = _serializer("reset-password").dumps({"id": user.id, "h": user.password_hash[-12:]})
            send_mail(user.email, "Passwort zurücksetzen / Сброс пароля",
                      f"Link (1 Stunde / 1 час):\n{_platform_url('/reset/' + tok)}\n")
        # одинаковый ответ — не раскрываем, есть ли такой аккаунт
        return render_template("auth/message.html", title="Prüfen Sie Ihr Postfach",
                               text="Falls ein Konto existiert, wurde eine E-Mail gesendet. / Если аккаунт существует, письмо отправлено.")
    return render_template("auth/forgot.html")


@bp.route("/reset/<token>", methods=["GET", "POST"])
@limiter.limit("10 per hour", methods=["POST"])
def reset(token):
    try:
        data = _serializer("reset-password").loads(token, max_age=3600)
    except (BadSignature, SignatureExpired):
        return render_template("auth/message.html", title="Link ungültig", text="Der Link ist ungültig oder abgelaufen."), 400
    user = db.session.get(User, data["id"])
    if not user or user.password_hash[-12:] != data["h"]:  # одноразовость: после смены пароля хеш другой
        return render_template("auth/message.html", title="Link ungültig", text="Der Link wurde bereits verwendet."), 400
    if request.method == "POST":
        password = request.form.get("password") or ""
        if len(password) < MIN_PASSWORD:
            flash(f"Пароль — минимум {MIN_PASSWORD} символов", "error")
            return render_template("auth/reset.html", token=token), 400
        user.set_password(password)
        user.session_version += 1
        user.failed_logins, user.locked_until = 0, None
        db.session.commit()
        audit("password_reset", agency_id=user.agency_id, user_id=user.id)
        return render_template("auth/message.html", title="Passwort geändert / Пароль изменён", text="OK", link=True)
    return render_template("auth/reset.html", token=token)
