"""Общее для всех этапов: какие файлы идут на этап (from / where), три раунда попыток, строки лога.

Правила отбора (подробно — configs\\README.txt):
- на этап идут только НЕ определённые файлы, которые этот этап ещё не проверял;
- файл с ошибкой на одном из предыдущих этапов ждёт повтора этого этапа и дальше не идёт;
- первый этап берёт все файлы;
- без from и where — все не определённые файлы;
- from без where — уверенность «опорного» этапа >= from; опорный — последний этап выше с уверенностью
  (wd14, camie, ai_image, json_meta), который реально проверял этот файл;
- where — выражение из [ЭТАП], and, or и скобок; [X] истинно, если этап X проверял файл, не определил его
  и его уверенность >= from (from по умолчанию 0). Уверенность ниже порога X получается сама:
  выше порога X файл был бы определён (кроме ответов, которые X отклонил — несколько тайтлов и т.п.).
"""
from __future__ import annotations

import gc
import re
import time
from pathlib import Path
from typing import Callable, Iterable

from engine.config import StageSpec
from engine.core import (MAX_ATTEMPTS, Dataset, QuotaExhaustedError, StageBack, StageSkipped, atomic_json, is_broken, pending_error, real_check,
                         stage_confidence)

Candidate = tuple[Path, Path, str]   # (chunk, файл, имя файла)


def reference_confidence(ds: Dataset, item: dict, stage: StageSpec) -> float | None:
    for previous in reversed(ds.stages[:stage.index - 1]):
        if previous.gives_confidence and real_check(item, previous.id):
            return stage_confidence(item, previous.id)
    return None


def where_term(item: dict, stage_id: str, low: float) -> bool:
    return (real_check(item, stage_id)
            and not item.get("stage_results", {}).get(stage_id)
            and stage_confidence(item, stage_id) >= low)


def eval_where(tree, item: dict, low: float) -> bool:
    kind = tree[0]
    if kind == "ref":
        return where_term(item, tree[1], low)
    if kind == "and":
        return eval_where(tree[1], item, low) and eval_where(tree[2], item, low)
    return eval_where(tree[1], item, low) or eval_where(tree[2], item, low)


def passes_filter(ds: Dataset, item: dict, stage: StageSpec) -> bool:
    if stage.index == 1:
        return True
    low = stage.from_ or 0.0
    if stage.where is not None:
        return eval_where(stage.where, item, low)
    if stage.from_ is None:
        return True
    confidence = reference_confidence(ds, item, stage)
    return confidence is not None and confidence >= low


def candidates(ds: Dataset, stage: StageSpec, extra: Callable[[Path, dict], bool] | None = None) -> list[Candidate]:
    earlier = ds.stages[:stage.index - 1]
    chosen = []
    for chunk, path, key in ds.entries:
        item = ds.item(key)
        if item.get("result") is not None or real_check(item, stage.id) or is_broken(item):
            continue
        # Возврат кнопкой «←»: этап доделывает только файлы, которых не касался этап, с которого вернулись.
        if any(real_check(item, ahead) for ahead in getattr(ds, "exclude_checked_by", ())):
            continue
        if any(pending_error(item, previous.id) for previous in earlier):
            continue
        if not passes_filter(ds, item, stage):
            continue
        if extra is not None and not extra(path, item):
            continue
        chosen.append((chunk, path, key))
    return chosen


def range_text(stage: StageSpec) -> str | None:
    """Пояснение к expected в строке «начало этапа»: откуда файлы пришли на этап."""
    if stage.index == 1:
        return None
    if stage.where is not None:
        return f"{stage.from_ or 0:.2f}+: {stage.where_text}"
    if stage.from_ is not None:
        return f"{stage.from_:.2f}+"
    return None


def stage_extra(stage: StageSpec) -> dict:
    extra = {}
    if stage.accept is not None:
        extra["k"] = stage.accept
    if stage.model:
        extra["model"] = stage.model
    text = range_text(stage)
    if text:
        extra["range"] = text
    return extra


def dash(value) -> str:
    text = str(value or "").strip()
    return text or "—"


MEMORY_RE = re.compile(r"out of memory|not enough memory|bad[_ ]alloc|failed to allocate|unable to allocate|"
                       r"paging file is too small|MemoryError|0xC0000017|ERROR_NOT_ENOUGH_MEMORY", re.I)


SERVICE_ERROR_RE = re.compile(r"URLError|HTTPError|HTTP \d{3}|[Tt]imeout|timed out|Connection|SSL|RemoteDisconnected|"
                              r"IncompleteRead|empty model response|JSONDecodeError|QuotaExhausted")


def is_memory_error(exc: BaseException) -> bool:
    return isinstance(exc, MemoryError) or bool(MEMORY_RE.search(f"{type(exc).__name__}: {exc}"))


def process_with_memory_wait(ds: Dataset, stage: StageSpec, key: str, call):
    """Не хватило оперативной памяти — это не ошибка файла: подождать и повторить тот же файл,
    не расходуя попытки этапа."""
    from engine import live

    tries = 0
    while True:
        try:
            return call()
        except Exception as exc:
            settings = live.current()   # memory_wait_seconds / memory_retries можно менять на ходу
            tries += 1
            if not is_memory_error(exc) or tries >= settings.memory_retries:
                raise
            gc.collect()
            ds.log(stage.id, "повтор", file=key, retry=True, error=True,
                   reason=f"мало оперативной памяти, пауза {settings.memory_wait_seconds}с ({tries}/{settings.memory_retries})",
                   error_text=f"{type(exc).__name__}: {str(exc)[:150]}")
            time.sleep(settings.memory_wait_seconds)
            ds.checkpoint()


def run_rounds(ds: Dataset, stage: StageSpec, pending: Iterable[Candidate],
               process: Callable[[Path, str, dict], tuple[dict, dict | None, dict]]) -> None:
    """До MAX_ATTEMPTS раундов: упавшие файлы повторяются в следующем раунде.

    process(файл, имя, item) -> (подробности этапа, результат или None, поля строки лога).
    После последнего раунда ошибка записывается в состояние — файл повторится при следующем запуске.
    """
    pending = list(pending)
    current = ds.logs / "current.json"
    succeeded = 0
    failed: dict[str, str] = {}
    for attempt in range(1, MAX_ATTEMPTS + 1):
        retry = []
        for chunk, path, key in pending:
            ds.checkpoint()
            # Над каким файлом работаем: если процесс упадёт, dataset.py засчитает падение этому файлу.
            atomic_json(current, {"stage": stage.id, "file": key, "identity": ds.item(key).get("source_id")})
            try:
                details, result, event = process_with_memory_wait(ds, stage, key, lambda: process(path, key, ds.item(key)))
            except (QuotaExhaustedError, StageSkipped, StageBack):
                current.unlink(missing_ok=True)
                raise
            except Exception as exc:
                error_text = f"{type(exc).__name__}: {str(exc)[:300]}"
                payload = {"error": True, "error_text": error_text, "confidence": 0.0, "title": "ERROR",
                           "characters": "ERROR", "attempt": f"ошибка-{attempt}"}
                if attempt < MAX_ATTEMPTS:
                    retry.append((chunk, path, key))
                    ds.log(stage.id, f"ошибка-{attempt} {key}", file=key, **payload)
                    continue
                ds.record_error(key, stage, payload)
                ds.log(stage.id, f"ошибка-{attempt} {key}", file=key, **payload, final=True)
                failed[key] = error_text
                continue
            succeeded += 1
            ds.finish(key, path, stage, details, result)
            event = {"file": key, "attempt": "" if attempt == 1 else f"ошибка-{attempt - 1}", "error": False, **event}
            ds.log(stage.id, f"{'определил' if result else 'не определил'} {key}", **event)
        if not retry:
            break
        pending = retry
    current.unlink(missing_ok=True)
    # Засчитать файлу неудачный запуск этапа: ошибку самого файла — всегда, а сбой сервиса (сеть, HTTP,
    # неразборчивый ответ модели) — только если в этом запуске этап справился с другими файлами:
    # иначе это, скорее всего, лёг сервер, и файлы не виноваты.
    for key, error_text in failed.items():
        if succeeded or not SERVICE_ERROR_RE.search(error_text):
            ds.count_failed_run(key, stage, error_text)


def run_stage(ds: Dataset, stage: StageSpec, chosen: list[Candidate],
              process: Callable[[Path, str, dict], tuple[dict, dict | None, dict]]) -> None:
    """Начало этапа -> раунды -> конец этапа."""
    extra = stage_extra(stage)
    visible = ds.announce(stage, len(chosen), **extra)
    if chosen:
        run_rounds(ds, stage, chosen, process)
    ds.finish_log(stage, visible, **extra)
