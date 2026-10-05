"""Конвейер одного набора: этапы из configN.json по порядку, каждый — модуль stages\\<type>.py.

Запускается из engine\\dataset.py отдельным процессом (нативное падение onnxruntime не роняет обёртку).
Коды выхода: 0 — готово, 3 — кончился баланс ключа, 4 — ошибка в конфиге, 5 — остались файлы с ошибками.
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from engine import nowmi  # noqa: E402,F401  — до onnxruntime: иначе WMI-запрос platform роняет процесс (0xC000070A)

import argparse  # noqa: E402
import importlib  # noqa: E402
import time  # noqa: E402

from engine.config import load_one  # noqa: E402
from engine.core import (EXIT_CONFIG, EXIT_QUOTA, MSG_BACK, MSG_SKIPPED, RUN_STARTED,  # noqa: E402
                         Dataset, QuotaExhaustedError, StageBack, StageSkipped, clear_skip,
                         pending_error)


def skipped_by_button(ds: Dataset, stage, exc: Exception) -> None:
    clear_skip(ds.logs)
    (ds.logs / "current.json").unlink(missing_ok=True)
    ds.log(stage.id, MSG_SKIPPED, reason=str(exc), manual=True)
    ds.console(f"{stage.label}: этап пропущен — {exc}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, help="имя конфига: config1")
    parser.add_argument("--root", type=Path, required=True, help="папка набора test-dataN")
    args = parser.parse_args()
    try:
        settings, spec = load_one(args.config)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr, flush=True)
        return EXIT_CONFIG
    ds = Dataset(args.root, settings, spec)
    # Локальные теггеры (onnxruntime) иначе занимают все ядра: процесс привязывается к первым
    # (всего - free_cores из live.json) ядрам — одинаково во всех пачках, так что свободные ядра свободны всегда.
    ds.apply_cores()
    ds.prepare()
    ds.migrate_output()
    ds.log("PIPELINE", f"{RUN_STARTED}: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    ds.apply_broken_checks()
    ds.initialize_all()
    try:
        # Автоматического перехода через заглохший этап нет (убран 05.10 по просьбе пользователя): этап ждёт свои
        # API и запасную модель. Вручную — кнопками окна набора:
        # Кнопка «→» в окне набора (StageSkipped) — этап пропускается сразу и к нему не возвращаются.
        # Кнопка «←» (StageBack) — назад к предыдущему этапу: он доделывает файлы, которых не касался этап, с которого
        # вернулись (ds.exclude_checked_by), потом конвейер снова идёт вперёд и тот этап продолжается.
        clear_skip(ds.logs)   # запросы кнопок, оставшиеся от прошлого запуска, к этому не относятся
        stages = list(spec.stages)
        ds.exclude_checked_by = set()
        index = 0
        while index < len(stages):
            stage = stages[index]
            ds.exclude_checked_by.discard(stage.id)
            try:
                importlib.import_module(f"stages.{stage.type}").run(ds, stage)
            except StageSkipped as exc:
                skipped_by_button(ds, stage, exc)
            except StageBack as exc:
                clear_skip(ds.logs, "back")
                (ds.logs / "current.json").unlink(missing_ok=True)
                if index == 0:
                    ds.console(f"{stage.label}: возврат невозможен — это первый этап")
                    continue
                target = stages[index - 1]
                ds.exclude_checked_by.add(stage.id)
                ds.log(stage.id, MSG_BACK, target=target.id, reason=str(exc))
                ds.console(f"{stage.label}: возврат к этапу {target.label} — {exc}")
                index -= 1
                continue
            index += 1
        ds.exclude_checked_by = set()
    except QuotaExhaustedError as exc:
        ds.log("PIPELINE", f"ОСТАНОВКА: баланс ключа исчерпан — {str(exc)[:300]}")
        print(f"ОСТАНОВКА: баланс ключа исчерпан.\n{exc}", file=sys.stderr, flush=True)
        return EXIT_QUOTA
    ds.rebuild_all()
    ds.sync_other()
    pending = ds.pending()
    if pending:
        # Файлы, на которых этап так и не справился (модель отвечает мусором и т. п.), не держат набор
        # открытым: помечаются битыми и уходят в «Other», набор завершается (с 02.10 — по просьбе пользователя;
        # исчерпанный баланс сюда не попадает — он выше останавливает набор через EXIT_QUOTA).
        for key in pending:
            item = ds.item(key)
            stage = next(s for s in spec.stages if pending_error(item, s.id))
            error = str(item.get("stage_errors", {}).get(stage.id, ""))[:200]
            ds.mark_broken(key, stage, f"этап не справился с файлом: {error}")
        ds.rebuild_all()
        ds.sync_other()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
