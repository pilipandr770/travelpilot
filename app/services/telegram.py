import logging

import httpx

log = logging.getLogger(__name__)


def send_message(bot_token: str, chat_id: str, text: str) -> tuple[bool, str]:
    try:
        r = httpx.post(f"https://api.telegram.org/bot{bot_token}/sendMessage",
                       json={"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": False},
                       timeout=15)
        if r.status_code == 200:
            return True, "ok"
        return False, f"Telegram: HTTP {r.status_code}"
    except httpx.HTTPError as exc:
        log.warning("telegram error: %s", exc)
        return False, "Telegram недоступен"
