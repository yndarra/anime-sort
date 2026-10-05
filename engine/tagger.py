"""Локальные теггеры WD-14 (EVA02-Large) и Camie: character-теги картинки -> тайтл/персонаж.

Модели и индекс персонажей берутся из локального кэша HuggingFace (HF_HUB_OFFLINE=1 ставит dataset.py).
Модуль тяжёлый (onnxruntime), поэтому импортируется только когда локальному этапу есть что делать.
"""
from engine import nowmi  # noqa: F401  — до onnxruntime/imgutils, см. nowmi.py

import json
import os
import re

import imgutils.utils.onnxruntime as _ort_utils
import pillow_heif
from huggingface_hub import hf_hub_download
from imgutils.tagging import get_camie_tags, get_wd14_tags


def _open_limited(ckpt: str, provider: str, use_cpu: bool = True):
    """Копия imgutils.utils.onnxruntime._open_onnx_model, но число потоков — не os.cpu_count() (все ядра),
    а столько, сколько ядер разрешено процессу (free_cores в configs/live.json, применяет Dataset.apply_cores)."""
    options = _ort_utils.SessionOptions()
    options.graph_optimization_level = _ort_utils.GraphOptimizationLevel.ORT_ENABLE_ALL
    if provider == "CPUExecutionProvider":
        options.intra_op_num_threads = int(os.environ.get("ANIME_SORT_CORES") or 0) or os.cpu_count()
        options.inter_op_num_threads = 1
    providers = [provider]
    if use_cpu and "CPUExecutionProvider" not in providers:
        providers.append("CPUExecutionProvider")
    return _ort_utils.InferenceSession(ckpt, options, providers=providers)


_ort_utils._open_onnx_model = _open_limited

pillow_heif.register_heif_opener()

WD14_MODEL = "EVA02_Large"
CAMIE_MODE = "high_precision"
# Нижняя граница, с которой теги вообще возвращаются: так видна реальная уверенность модели
# и её лучший вариант, даже когда до порога этапа (accept) он не дотягивает.
PROBE_THRESHOLD = 0.01

with open(hf_hub_download("deepghs/character_index", "characters.json", repo_type="dataset"), encoding="utf-8") as handle:
    CHAR_BY_TAG = {item["tag"]: item for item in json.load(handle)}

_PAREN_RE = re.compile(r"^(?P<name>.+)_\((?P<series>[^)]+)\)$")


def base_character_name(tag: str) -> str:
    match = _PAREN_RE.match(tag)
    return match.group("name") if match else tag


def get_series(tag: str):
    """Тайтл (slug) для character-тега или None."""
    item = CHAR_BY_TAG.get(tag)
    if item and item.get("copyright"):
        return item["copyright"]
    match = _PAREN_RE.match(tag)
    return match.group("series") if match else None


def probe(path: str, model: str) -> dict:
    """Все character-теги от PROBE_THRESHOLD: {тег: уверенность}."""
    if model == "wd14":
        _, _, chars = get_wd14_tags(path, model_name=WD14_MODEL, character_threshold=PROBE_THRESHOLD)
    else:
        _, _, chars = get_camie_tags(path, mode=CAMIE_MODE, thresholds={"character": PROBE_THRESHOLD})
    return chars


def decide_destination(char_tags: dict):
    """(kind, series, character); kind: character / series_group / mixed_group / other."""
    if not char_tags:
        return "other", None, None
    resolved = [(tag, get_series(tag)) for tag in char_tags]
    resolved = [(tag, series) for tag, series in resolved if series is not None]
    if not resolved:
        return "other", None, None
    if len(char_tags) == 1 and len(resolved) == 1:
        tag, series = resolved[0]
        return "character", series, base_character_name(tag)
    series_set = {series for _, series in resolved}
    if len(series_set) == 1:
        return "series_group", next(iter(series_set)), None
    return "mixed_group", None, None
