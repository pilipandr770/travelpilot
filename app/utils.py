"""Мелкие утилиты без зависимостей от моделей."""
import re
import secrets
from datetime import datetime, timezone
from urllib.parse import urlparse

import markdown as _markdown
import nh3
from slugify import slugify

RESERVED_SLUGS = {
    "www", "app", "api", "admin", "mail", "smtp", "imap", "ftp", "static", "assets", "cdn", "blog",
    "support", "help", "status", "dashboard", "login", "register", "billing", "docs", "demo", "test",
    "staging", "dev", "root", "ns1", "ns2", "autoconfig", "autodiscover", "_dmarc",
}

HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def utcnow() -> datetime:
    """Наивное UTC-время (единообразно для SQLite и Postgres)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def token(n: int = 24) -> str:
    return secrets.token_urlsafe(n)


def valid_color(value: str, default: str = "#0b6cff") -> str:
    value = (value or "").strip()
    return value if HEX_COLOR_RE.match(value) else default


def safe_next_url(target: str | None, fallback: str) -> str:
    """Разрешает только относительные пути (защита от open redirect)."""
    if not target:
        return fallback
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc or not target.startswith("/") or target.startswith("//"):
        return fallback
    return target


def make_slug(text: str, max_len: int = 60) -> str:
    return slugify(text or "", max_length=max_len, lowercase=True) or "item"


def mask_secret(value: str | None) -> str:
    if not value:
        return ""
    return "•" * 8 + value[-4:] if len(value) > 8 else "•" * 8


def clean_url(value: str | None) -> str | None:
    """Только http(s)-ссылки."""
    value = (value or "").strip()
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return value[:500]
    return None


_ALLOWED_TAGS = {
    "p", "h2", "h3", "h4", "ul", "ol", "li", "strong", "em", "a", "blockquote", "br", "hr",
    "table", "thead", "tbody", "tr", "th", "td", "code", "pre",
}
_ALLOWED_ATTRS = {"a": {"href", "title"}}


def render_markdown(md: str) -> str:
    """Markdown → безопасный HTML. Весь ИИ-контент проходит через эту функцию."""
    html = _markdown.markdown(md or "", extensions=["extra", "sane_lists"], output_format="html")
    return nh3.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRS,
        url_schemes={"http", "https", "mailto"},
        link_rel="noopener noreferrer nofollow",
    )


def strip_html(text: str) -> str:
    return nh3.clean(text or "", tags=set())


def truncate(text: str, n: int) -> str:
    text = text or ""
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"
