r"""Слияние готовых наборов test-dataN в Waifu.

Какие наборы вливать — решает сам: в test-dataN (из anime-paths.json коллекции) находит наборы, которые конвейер
полностью обработал (status completed, нет work\) и которые ещё не влиты (нет DONE.txt), и дописывает их в конец
folders.json — журнала слияний (что, когда, сколько скопировано). Порядок — по номеру набора.
Затем: sort_cancel (снять номера папок Waifu) -> копирование -> fix_name -> honkai -> sort (номера обратно).

python tools\add\add.py              влить всё готовое
python tools\add\add.py --dry-run    только показать
python tools\add\add.py --only test-data82 test-data83   только эти наборы
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

DEV_DIR = Path(__file__).resolve().parent.parent
PROJECT = DEV_DIR.parent
sys.path.insert(1, str(PROJECT))
SORT_DIR = DEV_DIR / "sort"
FIX_NAME_DIR = DEV_DIR / "fix_name"
sys.path.insert(0, str(DEV_DIR))

from waifu_common import WAIFU_ROOT, announce, configure_console, run_guarded, run_streamed

configure_console()

MEDIA = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".avif"}
def pipeline_prefix() -> re.Pattern:
    """Префиксы этапов конвейера в именах файлов: метки из configs\\config.json (names) и старые имена этапов."""
    labels = {"WD-14", "Camie", "AI-K3", "AI-F5", "AI-F51", "AI-G6", "AI-GPT", "AI-G5", "JSON",
              "N", "N-L", "N-R", "N-LR", "N-RL"}
    try:
        config = json.loads((DEV_DIR.parent / "configs" / "config.json").read_text(encoding="utf-8-sig"))
        for stage_id, value in (config.get("names") or {}).items():
            labels.add(str(stage_id))
            first = value[0] if isinstance(value, list) and value else value
            if isinstance(first, str) and first.strip():
                labels.add(first.strip())
    except Exception:
        pass
    alternatives = "|".join(re.escape(label) for label in sorted(labels, key=len, reverse=True))
    return re.compile(rf"^(?:{alternatives})\s+")


PIPELINE_PREFIX = pipeline_prefix()
NUMBER_PREFIX = re.compile(r"^\d+\.\s+")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def media_files(root: Path) -> list[Path]:
    return sorted(
        (path for path in root.rglob("*") if path.is_file() and path.suffix.casefold() in MEDIA),
        key=lambda path: str(path.relative_to(root)).casefold(),
    )


def clean_name(name: str) -> str:
    return PIPELINE_PREFIX.sub("", name)


def load_hashes(root: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for path in media_files(root):
        try:
            result.setdefault(sha256(path), path)
        except OSError:
            continue
    return result


def target_for(source: Path, source_root: Path, waifu: Path) -> Path:
    relative = source.relative_to(source_root)
    parts = list(relative.parts)
    filename = clean_name(parts.pop())
    parts = [clean_name(part) for part in parts]
    if not parts:
        parts = ["Other"]
    elif parts[0] in ("Other", "Другое"):   # старые наборы: папка «Другое» до переименования в Other
        parts = ["Other"]
    elif len(parts) >= 2:
        title, character = parts[0], parts[1]
        is_group = " _ " in character or character.casefold() in {"неизвестный персонаж", "групповые фото"}
        if title.casefold() == "неизвестный тайтл" and character.casefold() not in {"неизвестный персонаж", "групповые фото"}:
            is_group = False
        parts = [title] if is_group else [title, character]
    return waifu.joinpath(*parts, filename)


def collision_target(target: Path, source_hash: str) -> Path:
    if not target.exists():
        return target
    if target.is_file():
        try:
            if sha256(target) == source_hash:
                return target
        except OSError:
            pass
    return target.with_name(f"{target.stem}__{source_hash[:12]}{target.suffix}")


def atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.part")
    try:
        with source.open("rb") as source_handle, temporary.open("wb") as target_handle:
            shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
            target_handle.flush()
            os.fsync(target_handle.fileno())
        os.replace(temporary, target)
        shutil.copystat(source, target)
    finally:
        temporary.unlink(missing_ok=True)


def append_journal(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.part")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def has_numbered_dirs(root: Path) -> bool:
    return any(
        path.is_dir()
        and "0. dev" not in path.relative_to(root).parts
        and NUMBER_PREFIX.match(path.name)
        for path in root.rglob("*")
    )


def run_script(name: str, waifu: Path, flags: list[str]) -> None:
    script = SORT_DIR / f"{name}.py"
    if not script.exists():
        script = FIX_NAME_DIR / f"{name}.py"
    if not script.exists():
        raise RuntimeError(f"не найден скрипт {name}.py")
    run_streamed(name, script, waifu, flags)


def process_root(root: Path, waifu: Path, journal: Path, dry_run: bool, existing: dict[str, Path]) -> tuple[int, int]:
    """Скопировать один набор в Waifu. existing — хэши файлов Waifu (считаются один раз на весь запуск и пополняются)."""
    done = root / "DONE.txt"
    if done.exists():
        print(f"skip DONE: {root}", flush=True)
        return 0, 0
    source_root = root / "out" / "0. ALL"
    if not source_root.is_dir():
        raise RuntimeError(f"missing output: {source_root}")
    announce(f"{root.name}:")
    copied = skipped = 0
    files = media_files(source_root)
    for source in files:
        source_hash = sha256(source)
        if source_hash in existing:
            skipped += 1
            if dry_run:
                print(f"skip duplicate: {source.name}")
            continue
        target = collision_target(target_for(source, source_root, waifu), source_hash)
        if dry_run:
            print(f"copy: {source} -> {target}")
            copied += 1
            continue
        atomic_copy(source, target)
        if sha256(target) != source_hash:
            raise IOError(f"copy verification failed: {target}")
        existing[source_hash] = target
        append_journal(journal, {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "root": str(root),
            "source": str(source),
            "source_sha256": source_hash,
            "destination": str(target),
            "operation": "copy",
        })
        copied += 1
        print(f"copied: {source.name} -> {target}")
    if not dry_run:
        atomic_text(
            done,
            json.dumps({"status": "completed", "root": str(root), "files": len(files), "copied": copied, "skipped": skipped, "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}, ensure_ascii=False, indent=2) + "\n",
        )
    return copied, skipped


def number_key(name: str) -> list:
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", name)]


def load_journal(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data.setdefault("//", "Журнал слияний в Waifu: ведёт add.py сам — готовые наборы test-dataN дописываются в конец")
    data.setdefault("merged", [])
    return data


def migrate_txt(journal: dict, txt: Path) -> None:
    """Старый folders.txt (список путей) — один раз перенести в folders.json как уже влитые."""
    if not txt.exists() or journal["merged"]:
        return
    for line in txt.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if value and not value.startswith("#"):
            journal["merged"].append({"folder": Path(value).name, "merged_at": None})
    txt.rename(txt.with_suffix(".txt.migrated"))


def pending_roots(results: Path, journal: dict, only: list[str]) -> list[Path]:
    """Готовые и ещё не влитые наборы (по номеру); with --only — только названные."""
    from engine import marks

    merged = {entry["folder"] for entry in journal["merged"]}
    roots = []
    for root in sorted(results.glob("test-*"), key=lambda path: number_key(path.name)):
        if not root.is_dir() or (only and root.name not in only):
            continue
        if (root / "DONE.txt").exists():
            if root.name not in merged:
                journal["merged"].append({"folder": root.name, "merged_at": None})
            continue
        if marks.completed(root):
            roots.append(root)
    return roots


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--waifu", type=Path, default=WAIFU_ROOT)
    parser.add_argument("--folders", type=Path, default=Path(__file__).with_name("folders.json"))
    parser.add_argument("--journal", type=Path, default=DEV_DIR / "merge_journal.jsonl")
    parser.add_argument("--only", nargs="*", default=[], help="только эти наборы (test-dataN)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-pause", action="store_true")
    args = parser.parse_args()
    waifu = args.waifu.resolve()
    if not waifu.is_dir():
        raise SystemExit(f"Waifu root does not exist: {waifu}")
    from engine.config import Report, load_settings

    settings = load_settings(Report(), check_run_files=False)
    if settings is None:
        raise SystemExit("configs/config.json с ошибками — проверьте: venv/Scripts/python engine/main.py --check")
    folders = args.folders.resolve()
    history = load_journal(folders)
    migrate_txt(history, folders.with_suffix(".txt"))
    journal = args.journal.resolve()
    roots = pending_roots(settings.results, history, args.only)
    if not roots:
        print("Нечего вливать: все готовые наборы уже в Waifu.", flush=True)
        if not args.dry_run:
            atomic_text(folders, json.dumps(history, ensure_ascii=False, indent=1) + "\n")
        return 0
    print("К слиянию: " + ", ".join(root.name for root in roots), flush=True)
    numbered = has_numbered_dirs(waifu)
    dry = ["--dry-run"] if args.dry_run else []
    run_script("sort_cancel", waifu, dry)
    print("Хэширую файлы Waifu (один раз на весь запуск)…", flush=True)
    existing = load_hashes(waifu)
    total_copied = total_skipped = 0
    for root in roots:
        copied, skipped = process_root(root, waifu, journal, args.dry_run, existing)
        total_copied += copied
        total_skipped += skipped
        if not args.dry_run:
            history["merged"].append({"folder": root.name, "merged_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                      "copied": copied, "skipped": skipped})
            atomic_text(folders, json.dumps(history, ensure_ascii=False, indent=1) + "\n")
    run_script("fix_name", waifu, dry)
    run_script("honkai", waifu, dry)
    if numbered:
        run_script("sort", waifu, ["--no-edit"] if args.dry_run else ["--no-cancel"])
    else:
        print("sort skipped: Waifu was unnumbered at start")
    print(f"completed roots={len(roots)} copied={total_copied} skipped={total_skipped} dry_run={args.dry_run}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_guarded(main, busy=True))
