r"""Этап type = "json_meta": модель определяет по тексту пина, встроенному в файл (metadata), без картинки.

Идут только файлы, у которых есть metadata. Определение принимается, если его подтверждает сам текст
metadata (и каталог tools\ani\ani.txt — самые частые тайтлы/персонажи из Waifu) и уверенность >= accept.
"""
from __future__ import annotations

from engine.ani_catalog import load_catalog
from engine.client import ApiClient, metadata_info
from engine.config import PROJECT, StageSpec
from engine.core import Dataset, source_identity
from stages.ai_image import ai_event, folder_console
from stages.common import candidates, run_stage

ANI_CATALOG = PROJECT / "tools" / "ani" / "ani.txt"


def run(ds: Dataset, stage: StageSpec) -> None:
    chosen = candidates(ds, stage, extra=lambda path, item: bool(metadata_info(path)[0].strip()))
    client = None
    if chosen:
        catalog = load_catalog(ANI_CATALOG) if ANI_CATALOG.exists() else None
        client = ApiClient(stage, ds.cache, ds.log, metadata_only=True, catalog=catalog, console=folder_console(ds))

    def process(path, key, item):
        envelope = client.classify(path, key, item.get("source_id") or source_identity(path))
        # Текст пина выводится в логе отдельной строкой под записью.
        return envelope, envelope.get("result"), {**ai_event(envelope, stage), "show_metadata": True}

    run_stage(ds, stage, chosen, process)
