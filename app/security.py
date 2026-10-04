"""Слой безопасности: заголовки, SSRF-защита, аудит."""
import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import httpx
from flask import current_app, request

from .extensions import db
from .models import AuditLog


# ------------------------------------------------------------------ заголовки
def init_security(app):
    @app.after_request
    def _headers(resp):
        csp = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; style-src-attr 'unsafe-inline'; script-src 'self'; "
            "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'self'; "
            "form-action 'self'; frame-ancestors 'none'"
        )
        resp.headers.setdefault("Content-Security-Policy", csp)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
        resp.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if current_app.config["URL_SCHEME"] == "https":
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if request.path.startswith(("/app", "/admin", "/login", "/register", "/forgot", "/reset")):
            resp.headers["Cache-Control"] = "no-store"
        return resp


# ------------------------------------------------------------------- аудит
def audit(action: str, *, agency_id=None, user_id=None, detail: str = ""):
    try:
        db.session.add(AuditLog(
            agency_id=agency_id, user_id=user_id, action=action[:60], detail=(detail or "")[:300],
            ip=(request.remote_addr or "")[:45] if request else None,
        ))
        db.session.commit()
    except Exception:  # аудит не должен ломать запрос
        db.session.rollback()


# ------------------------------------------------------------------- SSRF
class UnsafeURL(Exception):
    pass


def _is_public_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return not (
        addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast
        or addr.is_reserved or addr.is_unspecified
    )


def assert_public_url(url: str):
    """Бросает UnsafeURL, если url ведёт не на публичный http(s)-хост."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeURL("разрешены только http/https")
    if not parsed.hostname:
        raise UnsafeURL("нет хоста")
    if parsed.username or parsed.password:
        raise UnsafeURL("креды в URL запрещены")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if port not in (80, 443):
        raise UnsafeURL("разрешены только порты 80/443")
    try:
        infos = socket.getaddrinfo(parsed.hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeURL("хост не резолвится") from exc
    for info in infos:
        if not _is_public_ip(info[4][0]):
            raise UnsafeURL("адрес не публичный")


def safe_fetch(url: str, *, max_bytes: int = 1_000_000, timeout: float = 8.0, max_redirects: int = 3) -> bytes:
    """GET с защитой от SSRF: проверка каждого хопа, лимит размера и времени.

    Остаточный риск: DNS-rebinding между проверкой и соединением — см. docs/03-SECURITY.md (пиннинг IP).
    """
    headers = {"User-Agent": "TravelPilot-FeedFetcher/1.0 (+https://example.invalid)"}
    with httpx.Client(timeout=timeout, follow_redirects=False, headers=headers) as client:
        current = url
        for _ in range(max_redirects + 1):
            assert_public_url(current)
            with client.stream("GET", current) as resp:
                if resp.is_redirect:
                    location = resp.headers.get("location")
                    if not location:
                        raise UnsafeURL("redirect без Location")
                    current = urljoin(current, location)
                    continue
                resp.raise_for_status()
                chunks, total = [], 0
                for chunk in resp.iter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise UnsafeURL("ответ слишком большой")
                    chunks.append(chunk)
                return b"".join(chunks)
    raise UnsafeURL("слишком много редиректов")
