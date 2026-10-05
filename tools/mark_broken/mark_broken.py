r"""Пометить «битыми» файлы, на которых этап стабильно падает, — чтобы набор завершился, а файлы ушли в «Other».

Движок сам помечает файл битым после broken_after_runs неудачных запусков этапа подряд (configs\live.json),
но если модель отвечает мусором только на один-два файла (обрезанный ответ, сломанный JSON), набор до этого
может много раз застревать «не завершено из-за ошибок» и открывать окно снова. Этот скрипт делает пометку сразу.

python tools\mark_broken\mark_broken.py              — только показать, что будет помечено
python tools\mark_broken\mark_broken.py --apply      — пометить (наборы, над которыми сейчас работает конвейер, пропускаются)
python tools\mark_broken\mark_broken.py --apply data22 data40   — только эти наборы

Пометка — то же поле item["broken"], что ставит движок (engine\core.py, mark_broken): файл больше не идёт по этапам,
остаётся не определённым и попадает в «Other»; список — out\broken.txt набора (пишется при следующем запуске набора).
Замена файла снимает пометку сама (новое содержимое — новое состояние).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))

from engine import winproc  # noqa: E402
from engine.config import load_settings, Report  # noqa: E402
from engine.core import pending_error  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="записать пометки (без него — только показать)")
    parser.add_argument("folders", nargs="*", help="dataN — только эти наборы")
    args = parser.parse_args()
    if sys.stdout is not None:
        sys.stdout.reconfigure(encoding="utf-8")
    settings = load_settings(Report(), check_run_files=False)
    if settings is None:
        raise SystemExit("configs/config.json с ошибками — проверьте: venv/Scripts/python engine/main.py --check")
    results = settings.results
    wanted = {f"test-{name}" for name in args.folders}
    total = 0
    for root in sorted(results.glob("test-data*"), key=lambda p: [int(x) if x.isdigit() else x for x in __import__("re").split(r"(\d+)", p.name)]):
        if wanted and root.name not in wanted:
            continue
        states = sorted((root / "work").glob("chunk_*/_state.json"))
        if not states:
            continue
        found = []
        for state_path in states:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            for key, item in state.get("items", {}).items():
                stages = [stage for stage in item.get("stage_errors", {}) if pending_error(item, stage)]
                if stages:
                    found.append((state_path, key, stages[0], str(item["stage_errors"][stages[0]])[:160]))
        if not found:
            continue
        try:
            pids = json.loads((root / "logs" / "pids.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pids = {}
        active = winproc.pid_alive(pids.get("dataset"))
        print(f"== {root.name}: файлов с ошибкой {len(found)}" + ("  — конвейер СЕЙЧАС работает, пропускаю" if active else ""))
        for _, key, stage, error in found:
            print(f"   {key}  {stage}: {error}")
        if not args.apply or active:
            continue
        by_state: dict[Path, list[tuple[str, str, str]]] = {}
        for state_path, key, stage, error in found:
            by_state.setdefault(state_path, []).append((key, stage, error))
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        events = []
        for state_path, entries in by_state.items():
            state = json.loads(state_path.read_text(encoding="utf-8"))
            for key, stage, error in entries:
                reason = f"помечен вручную: этап {stage} стабильно не обрабатывает файл ({error})"
                state["items"][key]["broken"] = {"stage": stage, "reason": reason, "time": stamp}
                events.append({"time": stamp, "stage": "PIPELINE", "message": f"битый файл: {reason}", "file": key,
                               "broken": True, "error": True, "error_text": reason})
            temporary = state_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
            temporary.replace(state_path)
        with (root / "logs" / "pipeline_events.jsonl").open("a", encoding="utf-8") as handle:
            for event in events:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        total += len(events)
        print(f"   помечено: {len(events)}")
    if args.apply:
        print(f"\nвсего помечено битыми: {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
