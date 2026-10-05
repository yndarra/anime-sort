"""Нумерация папок-персонажей Waifu на ходу: «<картинок>. <персонаж>» внутри всех тайтлов, как sort.py, но постоянно.

Переложили файл из одного персонажа в другого — номера обоих обновятся в течение пары секунд.
Папки-тайтлы не трогает (для них — number_titles.py; вместе не конфликтуют). Нумерует и более глубокие
папки, и папки персонажей внутри «Other», «MIX», «NPC», «Other» (сами эти папки без номера — как у sort.py).
Флаги: --interval 2, --young 10, --once, --dry-run, --no-pause (как у number_titles.py).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from watchlib import run_numbering  # noqa: E402
from waifu_common import WAIFU_ROOT, configure_console, run_guarded  # noqa: E402

configure_console()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=WAIFU_ROOT)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--young", type=float, default=10.0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--no-pause", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    if not root.is_dir():
        raise SystemExit(f"Waifu root does not exist: {root}")
    try:
        run_numbering(root, lambda folder: folder.depth >= 2, "папок-персонажей", args.interval, args.young, args.dry_run, args.once)
    except KeyboardInterrupt:
        print("\nнаблюдение остановлено", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(run_guarded(main))
