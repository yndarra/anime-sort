"""Этап type = "ai_image": модель смотрит на картинку (metadata файла идёт подсказкой).

Определение принимается, если модель назвала и тайтл, и персонажа (один тайтл) и уверенность >= accept.
"""
from __future__ import annotations

from engine.client import ApiClient, raw_image_result
from engine.config import StageSpec
from engine.core import Dataset, normalized_confidence, real_check, source_identity
from stages.common import candidates, dash, run_stage


def folder_console(ds: Dataset):
    """Строки о переключении API — в консоль start.vbs (имя папки «dataN:» добавляет сам ds.console)."""
    return ds.console


def character_index() -> dict:
    from engine import tagger

    return tagger.CHAR_BY_TAG


def ai_event(envelope: dict, stage: StageSpec) -> dict:
    metadata = str(envelope.get("metadata") or "")
    return {
        "model": stage.model,
        "confidence": envelope.get("confidence", 0.0),
        "title": dash(envelope.get("series")),
        "characters": dash(" / ".join(envelope.get("characters") or [])),
        "meta": len("".join(metadata.splitlines())),
        "metadata": "  ".join(metadata.splitlines()),
        "show_metadata": False,
    }


def reopen_for_threshold(ds: Dataset, stage: StageSpec) -> None:
    """Порог этапа снижен: не определённые этапом файлы, чей сохранённый ответ теперь проходит порог,
    снова становятся кандидатами. Ответ берётся из кэша — запросы не тратятся."""
    for _, _, key in ds.entries:
        item = ds.item(key)
        detail = item.get("stage_details", {}).get(stage.id)
        if item.get("result") is not None or not real_check(item, stage.id) or item["stage_results"].get(stage.id):
            continue
        candidate = detail.get("candidate") if isinstance(detail.get("candidate"), dict) else raw_image_result(detail, stage.model)
        if candidate and normalized_confidence(detail.get("confidence")) >= stage.accept:
            item["checked_stages"] = [name for name in item["checked_stages"] if name != stage.id]
            item["stage_results"].pop(stage.id, None)
            item["stage_details"].pop(stage.id, None)
            ds.save(key)


def run(ds: Dataset, stage: StageSpec) -> None:
    reopen_for_threshold(ds, stage)
    chosen = candidates(ds, stage)
    client = ApiClient(stage, ds.cache, ds.log, character_index=character_index, console=folder_console(ds)) if chosen else None

    def process(path, key, item):
        envelope = client.classify(path, key, item.get("source_id") or source_identity(path))
        return envelope, envelope.get("result"), ai_event(envelope, stage)

    run_stage(ds, stage, chosen, process)
