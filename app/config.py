"""Конфигурация приложения. Всё берётся из переменных окружения (см. .env.example)."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _bool(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _list(value, default=""):
    raw = value if value is not None else default
    return [x.strip() for x in raw.split(",") if x.strip()]


def _db_url():
    url = os.environ.get("DATABASE_URL", f"sqlite:///{BASE_DIR / 'dev.db'}")
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


class Config:
    APP_ENV = os.environ.get("APP_ENV", "development")  # development | production
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-insecure-secret-key-change-me")
    # Ключи Fernet для шифрования секретов агентов. Первый — для записи, остальные — для чтения (ротация).
    ENCRYPTION_KEYS = _list(os.environ.get("ENCRYPTION_KEYS"))
    # Токен для внутренних эндпоинтов (Caddy on-demand TLS "ask")
    INTERNAL_TOKEN = os.environ.get("INTERNAL_TOKEN", "")

    SQLALCHEMY_DATABASE_URI = _db_url()
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # --- Домены / мультитенантность ---
    BASE_DOMAIN = os.environ.get("BASE_DOMAIN", "localhost").lower()
    PLATFORM_EXTRA_HOSTS = _list(os.environ.get("PLATFORM_EXTRA_HOSTS"), "127.0.0.1")
    URL_SCHEME = os.environ.get("URL_SCHEME", "http")
    PUBLIC_PORT = os.environ.get("PUBLIC_PORT", "8000")  # пусто в продакшене (443)
    TRUSTED_PROXY_COUNT = int(os.environ.get("TRUSTED_PROXY_COUNT", "0"))

    # --- Сессии / cookies ---
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.environ.get("URL_SCHEME", "http") == "https"
    SESSION_COOKIE_NAME = "tp_session"
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SECURE = SESSION_COOKIE_SECURE
    PERMANENT_SESSION_LIFETIME = 60 * 60 * 24 * 14

    # --- Лимиты ---
    MAX_CONTENT_LENGTH = 12 * 1024 * 1024
    RATELIMIT_STORAGE_URI = os.environ.get("RATELIMIT_STORAGE_URI", "memory://")
    RATELIMIT_ENABLED = _bool(os.environ.get("RATELIMIT_ENABLED"), True)
    RATELIMIT_HEADERS_ENABLED = True
    CHAT_MAX_USER_MESSAGES_PER_SESSION = int(os.environ.get("CHAT_MAX_USER_MESSAGES_PER_SESSION", "40"))
    CHAT_DAILY_CAP_PER_AGENCY = int(os.environ.get("CHAT_DAILY_CAP_PER_AGENCY", "300"))

    # --- Файлы ---
    UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", str(BASE_DIR / "uploads")))
    PDF_FONT_PATH = os.environ.get("PDF_FONT_PATH", "")

    # --- ИИ ---
    ALLOWED_CLAUDE_MODELS = _list(
        os.environ.get("ALLOWED_CLAUDE_MODELS"),
        "claude-haiku-4-5-20251001,claude-sonnet-5-5,claude-opus-5-5",
    )
    DEFAULT_CHAT_MODEL = os.environ.get("DEFAULT_CHAT_MODEL", "claude-haiku-4-5-20251001")
    DEFAULT_BLOG_MODEL = os.environ.get("DEFAULT_BLOG_MODEL", "claude-sonnet-5-5")

    # --- Почта платформы (системные письма: подтверждение e-mail, сброс пароля) ---
    SMTP_HOST = os.environ.get("SMTP_HOST", "")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
    SMTP_USER = os.environ.get("SMTP_USER", "")
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
    SMTP_STARTTLS = _bool(os.environ.get("SMTP_STARTTLS"), True)
    MAIL_FROM = os.environ.get("MAIL_FROM", "TravelPilot <no-reply@localhost>")

    # --- Данные оператора платформы (Impressum платформы) ---
    PLATFORM_NAME = os.environ.get("PLATFORM_NAME", "TravelPilot")
    PLATFORM_OPERATOR_NAME = os.environ.get("PLATFORM_OPERATOR_NAME", "CHANGE_ME Name / Firma")
    PLATFORM_OPERATOR_ADDRESS = os.environ.get("PLATFORM_OPERATOR_ADDRESS", "CHANGE_ME Straße 1, 00000 Stadt")
    PLATFORM_OPERATOR_EMAIL = os.environ.get("PLATFORM_OPERATOR_EMAIL", "info@example.com")
    PLATFORM_OPERATOR_VAT = os.environ.get("PLATFORM_OPERATOR_VAT", "")

    # --- Регистрация ---
    REQUIRE_MANUAL_APPROVAL = _bool(os.environ.get("REQUIRE_MANUAL_APPROVAL"), True)
    REGISTRATION_OPEN = _bool(os.environ.get("REGISTRATION_OPEN"), True)
    CUSTOM_DOMAIN_TARGET = os.environ.get("CUSTOM_DOMAIN_TARGET", "")  # CNAME-цель для своих доменов, напр. sites.example.com

    TESTING = False
    WTF_CSRF_TIME_LIMIT = 60 * 60 * 8
