"""Persist hosted image results and expose unguessable, image-only download URLs."""

from __future__ import annotations

import base64
import os
import re
import uuid
from pathlib import Path


def image_output_dir() -> Path:
    configured = os.environ.get("LAN_BRIDGE_IMAGE_OUTPUT_DIR")
    if configured:
        return Path(configured)
    if os.name == "nt" and Path("G:/").is_dir():
        return Path("G:/LAN-BRIDGE/agent/output/images")
    return Path.home() / ".lan-bridge" / "agent" / "output" / "images"


def generated_image_path(filename: str) -> Path | None:
    if not re.fullmatch(r"[0-9a-f]{32}\.(png|jpeg|webp)", filename):
        return None
    path = image_output_dir() / filename
    return path if path.is_file() else None


def render_generated_image(item: dict, base_url: str) -> str:
    """Only completed results are published; filenames contain no user input."""
    result = item.get("result")
    if item.get("status") not in (None, "completed") or not isinstance(result, str) or not result:
        raise ValueError("图片生成未返回完整图片数据")
    data = base64.b64decode(result, validate=True)
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        extension = "png"
    elif data.startswith(b"\xff\xd8\xff"):
        extension = "jpeg"
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        extension = "webp"
    else:
        raise ValueError("图片生成返回了无法识别的图片格式")
    directory = image_output_dir()
    directory.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.{extension}"
    (directory / filename).write_bytes(data)
    url = f"{base_url.rstrip('/')}/generated-images/{filename}"
    return f"\n\n![生成的图片]({url})\n\n[下载图片]({url})\n"
