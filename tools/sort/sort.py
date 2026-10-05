from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sort_cancel import DEV_DIR_NAME, NEVER_NUMBERED, merge_dir, remove_empty_dirs
from waifu_common import WAIFU_ROOT, configure_console, run_guarded

configure_console()

MEDIA = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".avif"}
PREFIX = re.compile(r"^\d+\.\s+")
EXCLUDED = {DEV_DIR_NAME, "Other", "MIX", "NPC", "Групповые фото"}
FIX_NAME_SCRIPT = Path(__file__).resolve().parent.parent / "fix_name" / "fix_name.py"


def clean_title(name: str) -> str:
    return PREFIX.sub("", name)


def media_count(path: Path) -> int:
    return sum(1 for child in path.rglob("*") if child.is_file() and child.suffix.casefold() in MEDIA)


def number_dir(path: Path) -> Path:
    clean = clean_title(path.name)
    if clean in NEVER_NUMBERED:
        return path
    target = path.with_name(f"{media_count(path)}. {clean}")
    if str(target) == str(path):
        return path
    merge_dir(path, target)
    return target


def number_tree(parent: Path) -> None:
    """Нумерует все подпапки parent рекурсивно; сама parent не трогается."""
    for child in sorted((p for p in parent.iterdir() if p.is_dir()), key=lambda p: p.name.casefold()):
        if child.name.startswith(".") or child.name == DEV_DIR_NAME:
            continue
        number_tree(child)
        number_dir(child)


def ranked(root: Path) -> list[dict]:
    rows = []
    for title in root.iterdir():
        if not title.is_dir() or title.name in EXCLUDED or title.name.startswith("."):
            continue
        title_name = clean_title(title.name)
        characters = []
        for character in title.iterdir():
            if not character.is_dir() or character.name in EXCLUDED or character.name.startswith("."):
                continue
            characters.append({"name": clean_title(character.name), "count": media_count(character)})
        characters.sort(key=lambda row: (-row["count"], row["name"].casefold()))
        rows.append({"name": title_name, "count": media_count(title), "characters": characters})
    rows.sort(key=lambda row: (-row["count"], row["name"].casefold()))
    return rows


def run_fix_name(root: Path) -> None:
    if not FIX_NAME_SCRIPT.exists():
        return
    print("started fix_name")
    completed = subprocess.run(
        [sys.executable, str(FIX_NAME_SCRIPT), "--root", str(root), "--no-pause"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        details = (completed.stdout + "\n" + completed.stderr).strip() or "без дополнительного вывода"
        raise RuntimeError(f"fix_name.py завершился с кодом {completed.returncode}: {details}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=WAIFU_ROOT)
    parser.add_argument("--no-edit", action="store_true")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--no-cancel", action="store_true")
    parser.add_argument("--no-pause", action="store_true")
    parser.add_argument("--no-fix-name", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    if not root.is_dir():
        raise SystemExit(f"Waifu root does not exist: {root}")
    if not args.no_edit:
        # Сначала снимаем номера, потом fix_name (он сопоставляет чистые имена),
        # затем заново нумеруем. sort_cancel сам вызывает fix_name.
        if not args.no_cancel:
            cancel_command = [
                sys.executable,
                str(Path(__file__).with_name("sort_cancel.py")),
                "--root",
                str(root),
                "--no-pause",
            ]
            if args.no_fix_name:
                cancel_command.append("--no-fix-name")
            subprocess.run(cancel_command, check=True)
        elif not args.no_fix_name:
            run_fix_name(root)
        number_tree(root)
        remove_empty_dirs(root)
    rows = ranked(root)
    if args.as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        for row in rows:
            print(f"{row['count']}\t{row['name']}")
            for character in row["characters"]:
                print(f"  {character['count']}\t{character['name']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_guarded(main, busy=True))
