r"""Иконка «готово» (зелёная галочка) у папок dataN и test-dataN, которые конвейер полностью обработал.

Имя папки не меняется (anime-sort находит наборы по имени test-<папка>), меняется только значок в Проводнике:
в папку кладётся скрытый desktop.ini со ссылкой на assets\done.ico, папке ставится атрибут «системная»
(без него Windows desktop.ini не читает) и Проводнику сообщается, что значок изменился.

    mark_done(folder)    поставить значок
    clear_mark(folder)   убрать (папку снова будут обрабатывать)
    sync(batches, results) — значки у всех готовых наборов разом (tools\marks.py)
Готовый набор: в test-dataN\out\status_snapshot.json статус completed и нет папки work\.
"""
from __future__ import annotations

import ctypes
import json
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
ICON = PROJECT / "assets" / "done.ico"
DESKTOP_INI = "desktop.ini"
FILE_ATTRIBUTE_READONLY, FILE_ATTRIBUTE_HIDDEN, FILE_ATTRIBUTE_SYSTEM = 0x1, 0x2, 0x4


def ensure_icon() -> Path:
    """Нарисовать done.ico (зелёный круг с белой галочкой), если его ещё нет."""
    if ICON.exists():
        return ICON
    from PIL import Image, ImageDraw

    ICON.parent.mkdir(parents=True, exist_ok=True)
    size = 256
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((8, 8, size - 8, size - 8), fill=(22, 163, 74, 255))
    draw.line([(64, 134), (110, 182), (196, 82)], fill=(255, 255, 255, 255), width=28, joint="curve")
    image.save(ICON, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    return ICON


def _notify(folder: Path) -> None:
    """Проводник перечитает значок папки (SHChangeNotify: SHCNE_UPDATEITEM | SHCNF_PATHW)."""
    try:
        ctypes.windll.shell32.SHChangeNotify(0x00002000, 0x0005, ctypes.c_wchar_p(str(folder)), None)
    except (AttributeError, OSError):
        pass


def _set_attributes(path: Path, add: int = 0, remove: int = 0) -> None:
    kernel32 = ctypes.windll.kernel32
    current = kernel32.GetFileAttributesW(str(path))
    if current == 0xFFFFFFFF:
        return
    kernel32.SetFileAttributesW(str(path), (current | add) & ~remove)


def mark_done(folder: Path) -> bool:
    if not folder.is_dir():
        return False
    icon = ensure_icon()
    ini = folder / DESKTOP_INI
    if ini.exists():
        _set_attributes(ini, remove=FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM | FILE_ATTRIBUTE_READONLY)
    ini.write_text(f"[.ShellClassInfo]\r\nIconResource={icon},0\r\n[ViewState]\r\nMode=\r\nVid=\r\nFolderType=Generic\r\n",
                   encoding="utf-16")
    _set_attributes(ini, add=FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM)
    _set_attributes(folder, add=FILE_ATTRIBUTE_SYSTEM)
    _notify(folder)
    return True


def clear_mark(folder: Path) -> None:
    ini = folder / DESKTOP_INI
    if ini.exists():
        _set_attributes(ini, remove=FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM | FILE_ATTRIBUTE_READONLY)
        ini.unlink()
    _set_attributes(folder, remove=FILE_ATTRIBUTE_SYSTEM)
    _notify(folder)


def is_marked(folder: Path) -> bool:
    return (folder / DESKTOP_INI).exists()


def completed(result: Path) -> bool:
    """Набор test-dataN полностью обработан конвейером."""
    try:
        snapshot = json.loads((result / "out" / "status_snapshot.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return snapshot.get("status") == "completed" and not (result / "work").exists()


def sync(batches: Path, results: Path) -> tuple[int, int]:
    """Значки у всех готовых наборов и их папок dataN; у неготовых — снять. → (поставлено, снято)."""
    added = removed = 0
    for result in sorted(results.glob("test-*")):
        if not result.is_dir():
            continue
        source = batches / result.name.removeprefix("test-")
        done = completed(result)
        for folder in (result, source):
            if not folder.is_dir():
                continue
            if done and not is_marked(folder):
                added += mark_done(folder)
            elif not done and is_marked(folder):
                clear_mark(folder)
                removed += 1
    return added, removed


