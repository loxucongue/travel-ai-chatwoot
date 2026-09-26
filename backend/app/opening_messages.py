"""Typed, operator-authored opening messages; no model-owned media selection."""
from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
from typing import Literal

from PIL import Image
from pydantic import BaseModel, Field, model_validator

from app.models import StoredMedia

SELECTION_QUESTION = "您想先了解哪一條行程呢？"
MAX_BYTES = 20 * 1024 * 1024


class OpeningItem(BaseModel):
    key: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    content_type: Literal["text", "image", "video"] = "text"
    content: str = Field(default="", max_length=200)
    media_id: int | None = Field(default=None, gt=0)
    media_hash: str = Field(default="", max_length=64, pattern=r"^(?:[a-f0-9]{64})?$")
    media_name: str = Field(default="", max_length=255)

    @model_validator(mode="after")
    def validate_content(self):
        self.content = self.content.strip()
        if self.content_type == "text":
            if not self.content or self.media_id or self.media_hash:
                raise ValueError("opening_text_invalid")
        elif not self.media_id:
            raise ValueError("opening_media_required")
        return self


def delivery_items(items: list[dict] | None, texts: list[str]) -> list[dict]:
    result = [dict(item) for item in items] if items else [
        {"key": f"opening-{i}", "content_type": "text", "content": text}
        for i, text in enumerate(texts)
    ]
    if result and result[-1]["content_type"] != "text":
        result.append({"key": "selection-question", "content_type": "text", "content": SELECTION_QUESTION})
    return result


def opening_media_info(db, item: dict, tenant_id: int | None) -> dict:
    media = db.get(StoredMedia, item.get("media_id")) if item.get("media_id") else None
    if not tenant_id or not media or media.tenant_id != tenant_id:
        raise ValueError("opening_media_unavailable")
    if media.media_type != item.get("content_type"):
        raise ValueError("opening_media_type_mismatch")
    path = Path(media.storage_path)
    if not path.is_file() or not 0 < path.stat().st_size <= MAX_BYTES:
        raise ValueError("opening_media_unavailable")
    data = path.read_bytes()
    if media.media_type == "image":
        try:
            with Image.open(BytesIO(data)) as picture:
                if picture.format not in {"JPEG", "PNG", "GIF", "WEBP"}:
                    raise ValueError("opening_image_format_invalid")
                expected = Image.MIME.get(picture.format)
                picture.verify()
            if media.mime_type != expected:
                raise ValueError("opening_image_mime_mismatch")
        except (OSError, SyntaxError, Image.DecompressionBombError) as exc:
            raise ValueError("opening_image_invalid") from exc
    elif media.media_type == "video":
        valid = ((media.mime_type == "video/mp4" and len(data) >= 16 and data[4:8] == b"ftyp")
                 or (media.mime_type == "video/webm" and data.startswith(b"\x1a\x45\xdf\xa3") and b"webm" in data[:4096]))
        if not valid:
            raise ValueError("opening_video_format_invalid")
    else:
        raise ValueError("opening_media_type_invalid")
    digest = hashlib.sha256(data).hexdigest()
    if item.get("media_hash") and digest != item["media_hash"]:
        raise ValueError("opening_media_revision_changed")
    return {"media_id": media.id, "media_hash": digest, "media_name": media.original_name,
            "name": media.original_name, "content_type": media.media_type,
            "asset_key": None, "content_family": f"file:{digest}", "content_group_key": "",
            "route_variant": "", "opening_media": True}
