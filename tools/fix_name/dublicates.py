from __future__ import annotations

import argparse
import os
import random
import string
import sys
import time
from pathlib import Path

DEV_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DEV_DIR))

from waifu_common import WAIFU_ROOT, announce, configure_console, run_guarded, yellow

configure_console()

# Эти имена считаются мусорными и переименовываются в случайную строку.
TARGET_NAMES = {"i.webp", "maxresdefault.jpg"}
DEV_DIR_NAME = "0. dev"
ALPHABET = string.ascii_lowercase


def random_name(extension: str, known: set[str], length: int = 10) -> str:
    while True:
        candidate = "".join(random.choices(ALPHABET, k=length))
        if candidate.casefold() not in known:
            return candidate + extension


def scan_files(root: Path):
    """Быстрый рекурсивный обход без stat там, где он не нужен."""
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name != DEV_DIR_NAME:
                                stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            yield Path(entry.path)
                    except OSError:
                        continue
        except OSError:
            continue


def collect_names(root: Path) -> set[str]:
    known: set[str] = set()
    for path in scan_files(root):
        known.add(path.name.casefold())
    return known


def rename_to_random(path: Path, known: set[str], dry_run: bool) -> Path | None:
    new_name = random_name(path.suffix, known)
    target = path.with_name(new_name)
    if target.exists():
        return None
    if dry_run:
        print(f"would rename: {path.name} -> {new_name}", flush=True)
        known.add(new_name.casefold())
        return target
    try:
        path.rename(target)
    except OSError as exc:
        print(f"cannot rename {path}: {exc}", flush=True)
        return None
    known.discard(path.name.casefold())
    known.add(new_name.casefold())
    print(f"{yellow('renamed')}: {path.name} -> {new_name}", flush=True)
    return target


def dedupe_names(root: Path, known: set[str], dry_run: bool) -> int:
    """Оставляет по одному файлу на имя, остальные переименовывает."""
    groups: dict[str, list[Path]] = {}
    for path in scan_files(root):
        groups.setdefault(path.name.casefold(), []).append(path)
    renamed = 0
    for name, paths in sorted(groups.items()):
        if len(paths) < 2:
            continue
        for path in sorted(paths, key=lambda item: str(item).casefold())[1:]:
            if rename_to_random(path, known, dry_run) is not None:
                renamed += 1
    return renamed


def startup_pass(root: Path, known: set[str], dry_run: bool) -> tuple[int, int]:
    """При запуске: переименовать мусорные имена и развести одинаковые имена."""
    targets = 0
    for path in scan_files(root):
        if path.name.casefold() in TARGET_NAMES:
            if rename_to_random(path, known, dry_run) is not None:
                targets += 1
    duplicates = dedupe_names(root, known, dry_run)
    return targets, duplicates


def monitor(root: Path, known: set[str], interval: float, dry_run: bool) -> None:
    print(f"monitoring {root} every {interval}s; Ctrl+C to stop", flush=True)
    while True:
        started = time.perf_counter()
        for path in scan_files(root):
            if path.name.casefold() in TARGET_NAMES:
                rename_to_random(path, known, dry_run)
        elapsed = time.perf_counter() - started
        time.sleep(max(0.05, interval - elapsed))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=WAIFU_ROOT)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--once", action="store_true", help="только стартовая проверка дублей, без мониторинга")
    parser.add_argument("--no-pause", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    if not root.is_dir():
        raise SystemExit(f"Waifu root does not exist: {root}")

    started = time.perf_counter()
    known = collect_names(root)
    print(f"indexed {len(known)} names in {time.perf_counter() - started:.2f}s", flush=True)

    announce("проверка мусорных имён и дубликатов:")
    targets, duplicates = startup_pass(root, known, args.dry_run)
    print(f"target names renamed={targets} duplicate names renamed={duplicates}", flush=True)

    if args.once:
        return 0
    try:
        monitor(root, known, max(0.05, args.interval), args.dry_run)
    except KeyboardInterrupt:
        print("\nmonitor stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(run_guarded(main))
