from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

DEV_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DEV_DIR / "sort"))
sys.path.insert(0, str(DEV_DIR))

from sort_cancel import merge_dir, remove_empty_dirs
from waifu_common import WAIFU_ROOT, configure_console, run_guarded

configure_console()

SOURCE_NAME = "Honkai (series)"
TARGET_NAMES = ("Honkai Impact 3rd", "Honkai Star Rail")
PREFIX = re.compile(r"^\d+\.\s+")
SKIP_NAMES = {"MIX"}


def clean_name(name: str) -> str:
    return PREFIX.sub("", name).casefold().strip()


def character_dirs(title: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for entry in title.iterdir():
        if entry.is_dir() and not entry.name.startswith(".") and clean_name(entry.name) not in SKIP_NAMES:
            result.setdefault(clean_name(entry.name), entry)
    return result


def merge_characters(source_title: Path, target_title: Path, dry_run: bool) -> int:
    targets = character_dirs(target_title)
    moved = 0
    for entry in sorted((p for p in source_title.iterdir() if p.is_dir()), key=lambda p: p.name.casefold()):
        if entry.name.startswith(".") or clean_name(entry.name) in SKIP_NAMES:
            continue
        target = targets.get(clean_name(entry.name))
        if target is None:
            continue
        moved += 1
        print(f"{'would merge' if dry_run else 'merge'}: {entry} -> {target}")
        if not dry_run:
            merge_dir(entry, target)
    return moved


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=WAIFU_ROOT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-pause", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    if not root.is_dir():
        raise SystemExit(f"Waifu root does not exist: {root}")
    source = root / SOURCE_NAME
    if not source.is_dir():
        print(f"no {SOURCE_NAME} folder, nothing to do")
        return 0
    total = 0
    for name in TARGET_NAMES:
        target = root / name
        if not target.is_dir():
            print(f"skip missing target: {name}")
            continue
        total += merge_characters(source, target, args.dry_run)
    if not args.dry_run:
        remove_empty_dirs(root)
    print(f"merged_character_folders={total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_guarded(main, busy=True))
