"""Безопасная загрузка изображений: проверка содержимого Pillow, перекодирование в WebP (метаданные/полиглоты не сохраняются)."""
import secrets
from io import BytesIO
from pathlib import Path

from flask import current_app
from PIL import Image, UnidentifiedImageError

Image.MAX_IMAGE_PIXELS = 40_000_000
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}
MAX_SIDE = 1920


class UploadError(Exception):
    pass


def agency_dir(agency_id: int) -> Path:
    d = Path(current_app.config["UPLOAD_DIR"]) / str(agency_id) / "img"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_image(file_storage, agency_id: int, max_side: int = MAX_SIDE) -> str:
    data = file_storage.read(10 * 1024 * 1024 + 1)
    if not data or len(data) > 10 * 1024 * 1024:
        raise UploadError("Файл пустой или больше 10 МБ")
    try:
        img = Image.open(BytesIO(data))
        if img.format not in ALLOWED_FORMATS:
            raise UploadError("Допустимы только JPG, PNG, WebP")
        img.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise UploadError("Не удалось прочитать изображение") from exc
    img.thumbnail((max_side, max_side))
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if "A" in img.getbands() else "RGB")
    name = secrets.token_hex(10) + ".webp"
    img.save(agency_dir(agency_id) / name, "WEBP", quality=85)
    return name


def delete_image(agency_id: int, name: str | None):
    if not name or "/" in name or "\\" in name or ".." in name:
        return
    path = agency_dir(agency_id) / name
    if path.is_file():
        path.unlink()


def image_path(agency_id: int, name: str) -> Path | None:
    if not name or "/" in name or "\\" in name or ".." in name or not name.endswith(".webp"):
        return None
    path = agency_dir(agency_id) / name
    return path if path.is_file() else None
