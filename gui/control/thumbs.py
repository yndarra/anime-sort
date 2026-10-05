r"""Миниатюры наборов для Пульта: несколько картинок набора, уменьшенных до 96 px (JPEG), как data-URL.

Берутся из test-dataN\in (или dataN, если набор ещё не начат), первые по имени. Готовые миниатюры кэшируются в
logs\thumbs\<набор>\ — повторно картинки не открываются. Битая картинка просто пропускается.
"""
from __future__ import annotations

import base64
import io
from pathlib import Path

from control import context

SIZE = 96
MEDIA = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
CACHE = context.LOGS / "thumbs"


def thumbnail(source: Path, target: Path) -> bytes | None:
    if target.exists():
        return target.read_bytes()
    try:
        from PIL import Image

        with Image.open(source) as image:
            image.thumbnail((SIZE, SIZE))
            buffer = io.BytesIO()
            image.convert("RGB").save(buffer, "JPEG", quality=80)
    except Exception:
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(buffer.getvalue())
    return buffer.getvalue()


def for_dataset(name: str, count: int = 3) -> list[str]:
    """data-URL нескольких миниатюр набора name (data12 / data90-other)."""
    current = context.settings()
    if current is None:
        return []
    folders = [current.results / f"test-{name}" / "in", current.source / name]
    files = []
    for folder in folders:
        if folder.is_dir():
            files = sorted(p for p in folder.iterdir() if p.suffix.lower() in MEDIA)[:count * 2]
            if files:
                break
    urls = []
    for path in files:
        data = thumbnail(path, CACHE / name / f"{path.stem}.jpg")
        if data:
            urls.append("data:image/jpeg;base64," + base64.b64encode(data).decode("ascii"))
        if len(urls) == count:
            break
    return urls
