r"""Вернуть на повторную проверку файлы, на которых AI-этап получил ответ «картинки нет» (шлюз потерял картинку).

Так было 01.10.2026: gemini-3.8-flash у beniclo отвечал «No image was provided» — конвейер принял это за
«не определил», ответ попал в кэш. Теперь клиент (engine\client.py, NO_IMAGE_RE) считает такой ответ сбоем API,
а этот скрипт чинит то, что уже записано:
    - в work\chunk_*\_state.json набора у таких файлов этап забывается (checked_stages / stage_results /
      stage_details) — при следующем запуске набора этап проверит файл снова (через рабочий API);
    - из cache\<модель>.json набора удаляются такие ответы, чтобы они не подставились из кэша.
Файлы, которые уже определил другой этап (result есть), не трогаются.

python tools\mark_broken\reset_no_image.py           — только показать
python tools\mark_broken\reset_no_image.py --apply   — исправить (наборы, над которыми сейчас работает конвейер, пропускаются)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))

from engine import winproc  # noqa: E402
from engine.client import NO_IMAGE_RE  # noqa: E402
from engine.config import Report, load_settings  # noqa: E402


def no_image(answer: dict) -> bool:
    if not isinstance(answer, dict) or answer.get("characters"):
        return False
    text = " ".join(str(answer.get(field) or "") for field in ("reason", "series", "title"))
    return bool(NO_IMAGE_RE.search(text))


def write(path: Path, data: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if sys.stdout is not None:
        sys.stdout.reconfigure(encoding="utf-8")
    settings = load_settings(Report(), check_run_files=False)
    if settings is None:
        raise SystemExit("configs/config.json с ошибками — проверьте: venv/Scripts/python engine/main.py --check")
    results = settings.results
    total_items = total_cache = 0
    for root in sorted(results.glob("test-data*")):
        pids = {}
        try:
            pids = json.loads((root / "logs" / "pids.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        active = winproc.pid_alive(pids.get("dataset"))
        found: dict[Path, list[tuple[str, str]]] = {}
        for state_path in sorted((root / "work").glob("chunk_*/_state.json")):
            state = json.loads(state_path.read_text(encoding="utf-8"))
            for key, item in state.get("items", {}).items():
                if item.get("result"):
                    continue
                for stage, detail in item.get("stage_details", {}).items():
                    if no_image(detail):
                        found.setdefault(state_path, []).append((key, stage))
        caches: dict[Path, list[str]] = {}
        for cache_path in (root / "cache").glob("*.json"):
            try:
                data = json.loads(cache_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            bad = [entry for entry, value in data.items() if isinstance(value, dict) and no_image(value.get("result"))]
            if bad:
                caches[cache_path] = bad
        count = sum(len(v) for v in found.values())
        cached = sum(len(v) for v in caches.values())
        if not count and not cached:
            continue
        print(f"== {root.name}: файлов {count}, ответов в кэше {cached}" + ("  — конвейер СЕЙЧАС работает, пропускаю" if active else ""))
        if not args.apply or active:
            continue
        for state_path, entries in found.items():
            state = json.loads(state_path.read_text(encoding="utf-8"))
            for key, stage in entries:
                item = state["items"][key]
                item["checked_stages"] = [s for s in item.get("checked_stages", []) if s != stage]
                for field in ("stage_results", "stage_details", "stage_errors"):
                    item.get(field, {}).pop(stage, None)
            write(state_path, state)
        for cache_path, entries in caches.items():
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            for entry in entries:
                data.pop(entry, None)
            write(cache_path, data)
        total_items += count
        total_cache += cached
        print(f"   исправлено: файлов {count}, удалено из кэша {cached}")
    if args.apply:
        print(f"\nвсего: файлов на повторную проверку {total_items}, удалено ответов из кэша {total_cache}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
