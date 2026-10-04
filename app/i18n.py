"""Мини-i18n.

Два типа ключей:
  * точечные (`l.hero_title`, `s.nav_blog`) — лендинг платформы и публичные сайты агентств;
  * исходный русский текст — кабинет (русский = ключ; переводы на de добавляются в TR['de']).
Нет перевода → показывается ключ (для кабинета это исходный русский текст).
"""
from flask import g, request
from flask_login import current_user

LANG_LABELS = {"de": "Deutsch", "en": "English", "ru": "Русский", "uk": "Українська"}
PLATFORM_LANGS = ("de", "ru", "en")
SITE_LANGS = ("de", "en", "ru", "uk")

from .i18n_data import TR  # noqa: E402


def t(key: str, **kw) -> str:
    lang = getattr(g, "lang", "de")
    text = TR.get(lang, {}).get(key)
    if text is None and lang != "ru":
        text = TR["de"].get(key)
    if text is None:
        text = key
    return text.format(**kw) if kw else text


def init_i18n(app):
    @app.before_request
    def _pick_lang():
        g.lang = "de"
        path = request.path
        if path.startswith("/static/"):
            return
        if getattr(g, "agency", None) is not None:
            agency = g.agency
            allowed = agency.languages or [agency.default_language]
            wanted = request.cookies.get("lang")
            g.lang = wanted if wanted in allowed else (agency.default_language if agency.default_language in SITE_LANGS else "de")
        elif path.startswith(("/app", "/admin")) and current_user.is_authenticated:
            g.lang = current_user.ui_language if current_user.ui_language in ("ru", "de") else "ru"
        else:
            wanted = request.args.get("lang") or request.cookies.get("lang")
            if wanted not in PLATFORM_LANGS:
                wanted = request.accept_languages.best_match(PLATFORM_LANGS) or "de"
            g.lang = wanted
            g.set_lang_cookie = request.args.get("lang") in PLATFORM_LANGS

    @app.after_request
    def _lang_cookie(resp):
        if getattr(g, "set_lang_cookie", False):
            resp.set_cookie("lang", g.lang, max_age=60 * 60 * 24 * 365, samesite="Lax", httponly=True,
                            secure=app.config["URL_SCHEME"] == "https")
        return resp

    app.jinja_env.globals["t"] = t
    app.jinja_env.globals["LANG_LABELS"] = LANG_LABELS
