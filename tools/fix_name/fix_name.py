from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

DEV_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DEV_DIR / "sort"))
sys.path.insert(0, str(DEV_DIR))

from sort_cancel import merge_dir, remove_empty_dirs, same_location
from waifu_common import WAIFU_ROOT, configure_console, run_guarded

configure_console()

DEFAULT_NAMES = Path(__file__).resolve().parent / "names.json"
MAX_PASSES = 4
LINE_RE = re.compile(r'^\s*"?(?P<source>.+?)"?\s+-\s+"?(?P<target>.+?)"?\s*$')
# Запись в кавычках: разделитель — именно '" - "'. Иначе правило для тайтла с « - » внутри
# (например, "Re Zero - Starting Life in Another World") резалось по первому « - ».
QUOTED_RE = re.compile(r'^\s*"(?P<source>[^"]*)"\s+-\s+"(?P<target>[^"]*)"\s*$')


def normalize_rule_path(value: str) -> str:
    """Двойные пробелы в именах папок больше не создаются (конвейер их схлопывает),
    поэтому и в правилах каждый сегмент пути приводится к одиночным пробелам —
    иначе старые правила снова создавали бы папку с двойным пробелом."""
    return "\\".join(" ".join(part.split()) for part in value.split("\\"))


def raw_rules(path: Path, content: str, malformed: list[str], unreadable: list[tuple[str, str]]):
    """(как показать правило, источник, цель) — из names.json (с 04.10) или из старого names.txt."""
    if path.suffix.lower() == ".json":
        import json

        try:
            data = json.loads(content)
        except ValueError as exc:
            raise RuntimeError(f"names.json не читается как JSON: {exc}") from exc
        for block in [data.get("new") or {}, *(data.get("titles") or {}).values()]:
            for source, target in block.items():
                yield f'"{source}": "{target}"', source.strip(), str(target).strip()
        return
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            match = QUOTED_RE.match(stripped) or LINE_RE.match(stripped)
        except Exception as exc:
            unreadable.append((stripped, f"{type(exc).__name__}: {exc}"))
            continue
        if not match:
            malformed.append(stripped)
            continue
        yield (stripped, match.group("source").strip().strip('"').replace("\\\\", "\\"),
               match.group("target").strip().strip('"').replace("\\\\", "\\"))


def read_rules(path: Path) -> list[tuple[str, str]]:
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(f"cannot read names file {path}: {exc}") from exc
    rules: list[tuple[str, str]] = []
    empty: list[str] = []
    invalid: list[str] = []
    malformed: list[str] = []
    unreadable: list[tuple[str, str]] = []
    for stripped, raw_source, raw_target in raw_rules(path, content, malformed, unreadable):
        source = normalize_rule_path(raw_source)
        target = normalize_rule_path(raw_target)
        if not source.strip() and not target.strip():
            empty.append(stripped)
            continue
        if not source.strip():
            invalid.append(f"пустой источник: {stripped}")
            continue
        if not target.strip():
            invalid.append(f"пустая цель: {stripped}")
            continue
        if source == target:
            invalid.append(f"источник и цель совпадают: {stripped}")
            continue
        rules.append((source, target))
    if empty:
        print(f"skip empty rules: {len(empty)}")
    for line in invalid:
        print(f"skip invalid rule ({line})")
    for line in malformed:
        print(f"skip malformed line: {line}")
    for line, error in unreadable:
        print(f"skip unreadable line {line!r}: {error}")
    return rules


def rename_pair(root: Path, source_rel: str, target_rel: str, dry_run: bool) -> bool:
    source = root.joinpath(*Path(source_rel).parts)
    target = root.joinpath(*Path(target_rel).parts)
    if not source.is_dir():
        return False
    if str(source) == str(target):
        return False
    if same_location(source, target):
        if dry_run:
            print(f"would rename case: {source_rel} -> {target_rel}")
            return True
        print(f"rename case: {source_rel} -> {target_rel}")
        temporary = source.with_name(f"{source.name}.__fixname__")
        source.replace(temporary)
        temporary.replace(target)
        return True
    if same_location(target, source) or str(target).startswith(str(source) + os.sep):
        print(f"skip unsafe rule (target inside source): {source_rel} -> {target_rel}")
        return False
    print(f"{'would merge' if dry_run else 'merge'}: {source_rel} -> {target_rel}")
    if dry_run:
        return True
    target.parent.mkdir(parents=True, exist_ok=True)
    merge_dir(source, target)
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=WAIFU_ROOT)
    parser.add_argument("--names", type=Path, default=DEFAULT_NAMES)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-pause", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    if not root.is_dir():
        raise SystemExit(f"Waifu root does not exist: {root}")
    names = args.names.resolve()
    if not names.is_file():
        raise SystemExit(f"names file does not exist: {names}")
    rules = read_rules(names)
    applied = 0
    # Несколько проходов: правило персонажа может оказаться выше правила тайтла, которое
    # приносит этого персонажа (слияние тайтлов). Повторяем, пока что-то меняется;
    # циклы в правилах (A -> B, B -> A) ограничены числом проходов.
    for _ in range(1 if args.dry_run else MAX_PASSES):
        applied_this_pass = 0
        for source_rel, target_rel in rules:
            try:
                if rename_pair(root, source_rel, target_rel, args.dry_run):
                    applied_this_pass += 1
            except Exception as exc:
                print(f"cannot apply rule {source_rel!r} -> {target_rel!r}: {type(exc).__name__}: {exc}")
        applied += applied_this_pass
        if not applied_this_pass:
            break
    if not args.dry_run:
        remove_empty_dirs(root)
    print(f"rules={len(rules)} applied={applied}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_guarded(main, busy=True))
