from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

DEV_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DEV_DIR))

from waifu_common import WAIFU_ROOT, configure_console, run_guarded

configure_console()

MEDIA = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".avif"}
PREFIX = re.compile(r"^\d+\.\s+")
DEV_DIR_NAME = "0. dev"
OTHER_NAME = "Other"
# Эти папки сами не нумеруются, но их подпапки нумеруются.
NEVER_NUMBERED = {"Other", "MIX", "NPC"}
FIX_NAME_SCRIPT = DEV_DIR / "fix_name" / "fix_name.py"


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def unique_file(target: Path, source: Path) -> Path:
    if not target.exists():
        return target
    if target.is_file() and file_hash(target) == file_hash(source):
        return target
    stem, suffix = target.stem, target.suffix
    digest = file_hash(source)[:12]
    candidate = target.with_name(f"{stem}__{digest}{suffix}")
    index = 2
    while candidate.exists():
        if candidate.is_file() and file_hash(candidate) == file_hash(source):
            return candidate
        candidate = target.with_name(f"{stem}__{digest}_{index}{suffix}")
        index += 1
    return candidate


def same_location(source: Path, target: Path) -> bool:
    return os.path.normcase(str(source)) == os.path.normcase(str(target))


def remove_empty_dirs(root: Path, dry_run: bool = False) -> int:
    removed = 0
    try:
        directories = sorted(
            (path for path in root.rglob("*") if path.is_dir() and DEV_DIR_NAME not in path.relative_to(root).parts),
            key=lambda path: (len(path.parts), str(path).casefold()),
            reverse=True,
        )
    except OSError as exc:
        print(f"cannot scan empty directories: {exc}")
        return 0
    for path in directories:
        try:
            if any(path.iterdir()):
                continue
            removed += 1
            print(f"{'would remove' if dry_run else 'removed'} empty: {path}")
            if not dry_run:
                path.rmdir()
        except (OSError, PermissionError) as exc:
            print(f"cannot remove empty {path}: {exc}")
    return removed


def merge_dir(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for child in sorted(source.iterdir(), key=lambda item: item.name.casefold()):
        destination = target / child.name
        if child.is_dir():
            merge_dir(child, destination)
            try:
                child.rmdir()
            except OSError:
                pass
        else:
            try:
                destination = unique_file(destination, child)
                if destination.exists() and destination != child:
                    child.unlink()
                elif destination != child:
                    child.replace(destination)
            except FileNotFoundError:
                # Файл уже перенесли (одноимённая папка слилась раньше в этом же проходе) — не падать.
                print(f"skip vanished: {child}")
    try:
        source.rmdir()
    except OSError:
        pass


def safe_rename(source: Path, target: Path) -> None:
    if same_location(source, target):
        if str(source) == str(target):
            return
        # Отличается только регистр: переименование в два шага, иначе merge_dir
        # попытается слить папку саму с собой.
        temporary = source.with_name(f"{source.name}.__rename__")
        source.replace(temporary)
        temporary.replace(target)
        return
    merge_dir(source, target)


def strip_prefix(source: Path) -> Path | None:
    match = PREFIX.match(source.name)
    if not match:
        return None
    clean = source.name[match.end():]
    if not clean:
        return None
    target = source.with_name(clean)
    return None if str(target) == str(source) else target


def strip_tree(parent: Path, dry_run: bool = False) -> int:
    """Рекурсивно снимает числовые префиксы со всех подпапок parent."""
    changed = 0
    for child in sorted((p for p in parent.iterdir() if p.is_dir()), key=lambda p: p.name.casefold()):
        if child.name.startswith(".") or child.name == DEV_DIR_NAME:
            continue
        changed += strip_tree(child, dry_run)
        target = strip_prefix(child)
        if target is None:
            continue
        changed += 1
        if dry_run:
            print(f"would rename: {child} -> {target}")
            continue
        try:
            safe_rename(child, target)
        except (OSError, PermissionError) as exc:
            print(f"cannot rename: {child} -> {target}: {exc}")
            continue
        print(f"renamed: {child.name} -> {target.name}")
    return changed


def run_fix_name(root: Path, dry_run: bool) -> None:
    if not FIX_NAME_SCRIPT.exists():
        return
    print("started fix_name")
    command = [sys.executable, str(FIX_NAME_SCRIPT), "--root", str(root), "--no-pause"]
    if dry_run:
        command.append("--dry-run")
    completed = subprocess.run(command, check=False, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if completed.returncode != 0:
        details = (completed.stdout + "\n" + completed.stderr).strip() or "без дополнительного вывода"
        raise RuntimeError(f"fix_name.py завершился с кодом {completed.returncode}: {details}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=WAIFU_ROOT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-pause", action="store_true")
    parser.add_argument("--no-fix-name", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    if not root.exists():
        raise SystemExit(f"Waifu root does not exist: {root}")
    removed = remove_empty_dirs(root, args.dry_run)
    changed = strip_tree(root, args.dry_run)
    if not args.no_fix_name:
        run_fix_name(root, args.dry_run)
    if not args.dry_run:
        removed += remove_empty_dirs(root)
    print("already unnumbered" if changed == 0 else f"changed={changed}")
    print(f"empty_removed={removed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_guarded(main, busy=True))
