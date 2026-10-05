"""Удаление пустых папок Waifu на ходу (как dublicates.py — стартовый проход + наблюдение).

Пустая папка — без единого файла внутри (в том числе состоящая только из пустых папок). Удаляется, только если
пробыла пустой не меньше --grace секунд (по умолчанию 15): только что созданную в Проводнике «Новую папку»,
в которую вы как раз переносите файлы, наблюдатель не тронет. Корень Waifu и «0. dev» не удаляются никогда.
Флаги: --interval 2, --grace 15, --once (только стартовый проход, без ожидания grace), --dry-run, --no-pause.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from watchlib import GREEN, RED, say, scan, wait_if_busy  # noqa: E402
from waifu_common import WAIFU_ROOT, configure_console, rename_lock, run_guarded  # noqa: E402

configure_console()


def remove_tree_of_empties(path: Path) -> bool:
    """Удалить папку, в которой только пустые папки (снизу вверх). Появился файл — останавливаемся."""
    for current, dirs, files in os.walk(path, topdown=False):
        if files:
            return False
        os.rmdir(current)
    return True


def empty_tops(root: Path) -> list[Path]:
    """Самые верхние пустые папки (их вложенные пустые папки удаляются вместе с ними)."""
    empty = {folder.path for folder in scan(root) if folder.files == 0}
    return sorted((path for path in empty if path.parent not in empty), key=lambda path: str(path).casefold())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=WAIFU_ROOT)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--grace", type=float, default=15.0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--no-pause", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    if not root.is_dir():
        raise SystemExit(f"Waifu root does not exist: {root}")
    say(f"удаление пустых папок в {root}: пустая дольше {args.grace:g} с — удаляется")
    first_seen: dict[Path, float] = {}
    state: dict = {}
    first = True
    try:
        while True:
            started = time.perf_counter()
            if not wait_if_busy(state):
                now = time.time()
                tops = empty_tops(root)
                first_seen = {path: first_seen.get(path, now) for path in tops}
                removed = 0
                for path in tops:
                    # На стартовом проходе ждать нечего: папки уже были пустыми до запуска.
                    if not first and now - first_seen[path] < args.grace:
                        continue
                    relative = path.relative_to(root)
                    if args.dry_run:
                        say(f"удалил бы пустую: {relative}")
                        removed += 1
                        continue
                    try:
                        with rename_lock():
                            if path.is_dir() and remove_tree_of_empties(path):
                                say(f"удалена пустая: {relative}", GREEN)
                                removed += 1
                                first_seen.pop(path, None)
                    except OSError as exc:
                        say(f"не удалось удалить {relative}: {exc.strerror or exc}", RED)
                if first:
                    say(f"стартовый проход: удалено пустых папок — {removed}")
                    first = False
                    if args.once:
                        return 0
            time.sleep(max(0.2, args.interval - (time.perf_counter() - started)))
    except KeyboardInterrupt:
        print("\nнаблюдение остановлено", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(run_guarded(main))
