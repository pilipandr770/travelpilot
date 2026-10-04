"""Проверка USt-IdNr. через EU VIES (best-effort). Можно заменить вызовом вашего vat-verifizierung.de."""
import re

import httpx


def check_vat(vat_id: str) -> tuple[bool | None, str]:
    vat = re.sub(r"[\s.\-]", "", (vat_id or "")).upper()
    m = re.match(r"^([A-Z]{2})([A-Z0-9]{2,14})$", vat)
    if not m:
        return None, "Неверный формат (пример: DE123456789)"
    try:
        r = httpx.get(f"https://ec.europa.eu/taxation_customs/vies/rest-service/ms/{m.group(1)}/vat/{m.group(2)}", timeout=12)
        r.raise_for_status()
        data = r.json()
        if "isValid" in data:
            return bool(data["isValid"]), "VIES: номер действителен" if data["isValid"] else "VIES: номер не найден"
    except Exception:  # noqa: BLE001
        pass
    return None, "Не удалось проверить в VIES (сервис недоступен). Повторите позже."
