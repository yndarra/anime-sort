"""Общее для наблюдателей Waifu (empty_dirs.py, number_titles.py, number_characters.py).

Каждый наблюдатель: стартовый проход, затем проверка раз в --interval секунд (по умолчанию 2).
Полный обход Waifu (~27 тыс. файлов) занимает ~0.2 с, поэтому постоянная проверка почти не нагружает систему.

Чтобы наблюдатели не мешали друг другу и остальным скриптам:
- каждое переименование/удаление папки — под общей блокировкой rename_lock (именованный мьютекс Windows);
- пока работают add / sort / sort_cancel / fix_name / honkai, наблюдатели ничего не делают (waifu_is_busy);
- папки, созданные меньше --young секунд назад, не трогаются: в Проводнике их как раз переименовывают.
"""
from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

DEV_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DEV_DIR / "sort"))
sys.path.insert(0, str(DEV_DIR))

from sort_cancel import DEV_DIR_NAME, NEVER_NUMBERED, merge_dir, same_location  # noqa: E402
from waifu_common import rename_lock, waifu_is_busy  # noqa: E402

MEDIA = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".avif"}
PREFIX = re.compile(r"^\d+\.\s+")
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
RED = "\x1b[31m"
GREY = "\x1b[90m"
RESET = "\x1b[0m"


@dataclass
class Folder:
    path: Path
    depth: int            # 1 — папка-тайтл, 2 и глубже — папки внутри тайтла
    media: int            # картинок внутри (рекурсивно) — это число и ставится в номер
    files: int            # файлов любых внутри (рекурсивно): 0 — папка пустая (или только из пустых папок)
    created: float


def say(text: str, color: str = "") -> None:
    stamp = time.strftime("%H:%M:%S")
    print(f"{GREY}[{stamp}]{RESET} {color}{text}{RESET}" if color else f"{GREY}[{stamp}]{RESET} {text}", flush=True)


def scan(root: Path) -> list[Folder]:
    """Все папки Waifu (кроме «0. dev» и скрытых) с количеством файлов внутри."""
    folders: list[Folder] = []

    def walk(path: Path, depth: int) -> tuple[int, int]:
        media = files = 0
        try:
            with os.scandir(path) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name == DEV_DIR_NAME or entry.name.startswith("."):
                                continue
                            child_media, child_files = walk(Path(entry.path), depth + 1)
                            media += child_media
                            files += child_files
                            try:
                                created = entry.stat(follow_symlinks=False).st_ctime
                            except OSError:
                                created = time.time()
                            folders.append(Folder(Path(entry.path), depth + 1, child_media, child_files, created))
                        else:
                            files += 1
                            if os.path.splitext(entry.name)[1].casefold() in MEDIA:
                                media += 1
                    except OSError:
                        continue
        except OSError:
            pass
        return media, files

    walk(root, 0)
    return folders


def clean_name(name: str) -> str:
    return PREFIX.sub("", name)


def young(folder: Folder, seconds: float) -> bool:
    return time.time() - folder.created < seconds


def wait_if_busy(state: dict) -> bool:
    """True — идёт массовая правка Waifu, этот цикл пропускается (сообщение — один раз)."""
    if waifu_is_busy():
        if not state.get("busy"):
            say("пауза: идёт add / sort / fix_name — жду, пока закончится", YELLOW)
            state["busy"] = True
        return True
    if state.get("busy"):
        say("продолжаю наблюдение")
        state["busy"] = False
    return False


# ---------------------------------------------------------------- нумерация

def number_level(root: Path, folders: list[Folder], wanted, young_seconds: float, dry_run: bool) -> int:
    """Нумерует папки, для которых wanted(folder) истинно: «<картинок>. <имя>», как sort.py.
    Одноимённые (без номера) папки одного уровня сливаются в ту, где картинок больше — как sort_cancel."""
    changed = 0
    level = [folder for folder in folders if wanted(folder)]
    by_parent: dict[tuple[Path, str], list[Folder]] = {}
    for folder in level:
        by_parent.setdefault((folder.path.parent, clean_name(folder.path.name).casefold()), []).append(folder)
    for (_, _), group in by_parent.items():
        if len(group) < 2 or any(young(folder, young_seconds) for folder in group):
            continue
        group.sort(key=lambda folder: -folder.media)
        keep = group[0]
        for other in group[1:]:
            if not other.path.is_dir() or not keep.path.is_dir():
                continue
            say(f"{'слил бы' if dry_run else 'слияние'}: {other.path.relative_to(root)} -> {keep.path.relative_to(root)}", YELLOW)
            if not dry_run:
                with rename_lock():
                    if other.path.is_dir() and keep.path.is_dir():
                        merge_dir(other.path, keep.path)
            changed += 1
        if changed:
            return changed   # дерево поменялось — пересчитать на следующем цикле
    for folder in level:
        clean = clean_name(folder.path.name)
        if clean in NEVER_NUMBERED or folder.media == 0 or young(folder, young_seconds):
            continue
        target = folder.path.with_name(f"{folder.media}. {clean}")
        if folder.path.name == target.name:
            continue
        relative = folder.path.relative_to(root)
        if dry_run:
            say(f"переименовал бы: {relative} -> {target.name}")
            changed += 1
            continue
        try:
            with rename_lock():
                if not folder.path.is_dir():
                    continue   # папку уже переместили/переименовали — разберёмся на следующем цикле
                if target.exists() and not same_location(folder.path, target):
                    merge_dir(folder.path, target)
                    say(f"слияние: {relative} -> {target.name}", YELLOW)
                else:
                    folder.path.rename(target)
                    say(f"{relative} -> {target.name}", GREEN)
            changed += 1
        except OSError as exc:
            # Чаще всего файл внутри открыт в другой программе; повторится на следующем цикле.
            say(f"не удалось переименовать {relative}: {exc.strerror or exc}", RED)
    return changed


def run_numbering(root: Path, wanted, what: str, interval: float, young_seconds: float, dry_run: bool, once: bool) -> None:
    say(f"нумерация {what} в {root}: число картинок в начале имени, как у sort.py")
    state: dict = {}
    first = True
    while True:
        started = time.perf_counter()
        if not wait_if_busy(state):
            changed = number_level(root, scan(root), wanted, young_seconds, dry_run)
            if first:
                say(f"стартовый проход: изменено папок — {changed}")
                first = False
                if once:
                    return
        time.sleep(max(0.2, interval - (time.perf_counter() - started)))
