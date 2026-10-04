"""Обёртка над Anthropic API. Всегда используется КЛЮЧ АГЕНТА (BYOK)."""
import logging

import anthropic
from flask import current_app

from ..extensions import db
from ..models import UsageEvent
from ..utils import truncate, utcnow

log = logging.getLogger(__name__)


class LLMError(Exception):
    """Ошибка, текст которой безопасно показать агенту."""


class LLMNotConfigured(LLMError):
    pass


def _client(agency) -> anthropic.Anthropic:
    key = agency.get_secret("anthropic")
    if not key:
        raise LLMNotConfigured("Ключ Anthropic не задан (Кабинет → Интеграции).")
    return anthropic.Anthropic(api_key=key, timeout=60.0, max_retries=1)


def resolve_model(agency, purpose: str) -> str:
    cfg = current_app.config
    chosen = agency.bot_model if purpose == "chat" else agency.blog_model
    default = cfg["DEFAULT_CHAT_MODEL"] if purpose == "chat" else cfg["DEFAULT_BLOG_MODEL"]
    return chosen if chosen in cfg["ALLOWED_CLAUDE_MODELS"] else default


def _note_error(agency, message: str | None):
    agency.llm_last_error = truncate(message, 290) if message else None
    agency.llm_last_error_at = utcnow() if message else None
    db.session.commit()


def call(agency, *, model: str, system: str, messages: list, kind: str, tools=None,
         tool_choice=None, max_tokens: int = 1024):
    """Один вызов Messages API с учётом токенов и человекочитаемыми ошибками."""
    client = _client(agency)
    params = {"model": model, "system": system, "messages": messages, "max_tokens": max_tokens}
    if tools:
        params["tools"] = tools
    if tool_choice:
        params["tool_choice"] = tool_choice
    try:
        resp = client.messages.create(**params)
    except anthropic.AuthenticationError as exc:
        _note_error(agency, "Ключ Anthropic недействителен.")
        raise LLMError("Ключ Anthropic недействителен.") from exc
    except anthropic.PermissionDeniedError as exc:
        _note_error(agency, "У ключа Anthropic нет доступа к этой модели.")
        raise LLMError("У ключа Anthropic нет доступа к этой модели.") from exc
    except anthropic.RateLimitError as exc:
        raise LLMError("Превышен лимит запросов Anthropic. Попробуйте позже.") from exc
    except anthropic.APIConnectionError as exc:
        raise LLMError("Нет соединения с Anthropic API.") from exc
    except anthropic.APIStatusError as exc:
        log.warning("anthropic status error %s", exc.status_code)
        detail = ""
        if exc.status_code == 400:
            body = exc.body if isinstance(exc.body, dict) else {}
            detail = truncate(str((body.get("error") or {}).get("message") or ""), 200)
        msg = f"Ошибка Anthropic API ({exc.status_code})." + (f" {detail}" if detail else "")
        _note_error(agency, msg)
        raise LLMError(msg) from exc
    if agency.llm_last_error:
        _note_error(agency, None)
    usage = getattr(resp, "usage", None)
    db.session.add(UsageEvent(
        agency_id=agency.id, kind=kind, model=model,
        tokens_in=getattr(usage, "input_tokens", 0) or 0,
        tokens_out=getattr(usage, "output_tokens", 0) or 0,
    ))
    db.session.commit()
    return resp


def text_of(resp) -> str:
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()


def tool_input(resp, name: str) -> dict | None:
    for block in resp.content:
        if getattr(block, "type", "") == "tool_use" and block.name == name:
            return dict(block.input)
    return None


def test_key(agency) -> tuple[bool, str]:
    try:
        call(agency, model=current_app.config["DEFAULT_CHAT_MODEL"], system="Reply with: ok",
             messages=[{"role": "user", "content": "ping"}], kind="chat", max_tokens=8)
        return True, "Ключ работает."
    except LLMError as exc:
        return False, str(exc)
