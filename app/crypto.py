"""Шифрование секретов агентов (API-ключи).

Fernet (AES-128-CBC + HMAC). MultiFernet позволяет ротацию ключей:
первый ключ шифрует, все перечисленные — расшифровывают.
Шифртекст привязан к (agency_id, provider): подмена строки в БД другому агентству не пройдёт.
"""
import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from flask import current_app

log = logging.getLogger(__name__)


class DecryptionError(Exception):
    pass


def _fernet() -> MultiFernet:
    keys = list(current_app.config.get("ENCRYPTION_KEYS") or [])
    if not keys:
        if current_app.config.get("APP_ENV") == "production":
            raise RuntimeError("ENCRYPTION_KEYS не задан")
        # dev-режим: ключ выводится из SECRET_KEY. НЕ использовать в продакшене.
        digest = hashlib.sha256(current_app.config["SECRET_KEY"].encode()).digest()
        keys = [base64.urlsafe_b64encode(digest).decode()]
    return MultiFernet([Fernet(k.encode()) for k in keys])


def encrypt(value: str, *, agency_id: int, provider: str) -> str:
    payload = f"{agency_id}\x1f{provider}\x1f{value}".encode()
    return _fernet().encrypt(payload).decode()


def decrypt(token: str, *, agency_id: int, provider: str) -> str:
    try:
        raw = _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise DecryptionError("не удалось расшифровать секрет") from exc
    aid, prov, value = raw.split("\x1f", 2)
    if aid != str(agency_id) or prov != provider:
        log.error("secret binding mismatch agency=%s provider=%s", agency_id, provider)
        raise DecryptionError("секрет привязан к другой записи")
    return value


def generate_key() -> str:
    return Fernet.generate_key().decode()
