"""Этап type = "neighbors" (N): ближайшие определённые файлы слева и справа (по порядку сохранения)
указывают на одного и того же персонажа того же тайтла — файл получает этот ТАЙТЛ (без папки персонажа).

Соседи берутся по состоянию НА НАЧАЛО этапа: определения, сделанные этим же этапом, дальше не распространяются.
Общий код с neighbors_title (N-L&R) — run_both().
"""
from __future__ import annotations

import bisect

from engine.config import StageSpec
from engine.core import Dataset, clean_series, pretty
from stages.common import candidates, run_stage


def known_before(ds: Dataset) -> dict[int, dict]:
    """Позиция файла -> его определение на момент начала этапа."""
    return {index: dict(ds.item(key)["result"]) for index, (_, _, key) in enumerate(ds.entries) if ds.item(key).get("result")}


def neighbor_event(result: dict | None) -> dict:
    return {
        "model": "Neighbors",
        "confidence": None,   # у соседей уверенности нет — в логе 4 «—»
        "title": pretty(result["series"]) if result and result.get("series") else "—",
        "characters": pretty(result["character"]) if result and result.get("character") else "—",
    }


def title_key(result: dict) -> str:
    return " ".join(clean_series(result.get("series")).casefold().split())


class LocalOrder:
    """Порядок внутри самого набора (обычные наборы test-dataN). Тот же интерфейс, что у GlobalOrder."""

    def __init__(self, ds: Dataset, known: dict[int, dict]):
        self.ds = ds
        self.known = known
        self.order = sorted(known)
        self.positions = {key: index for index, (_, _, key) in enumerate(ds.entries)}

    def position(self, key: str) -> int | None:
        return self.positions.get(key)

    def name_at(self, index: int | None) -> str | None:
        return self.ds.entries[index][2] if index is not None and 0 <= index < len(self.ds.entries) else None


def file_order(ds: Dataset):
    r"""Наборы второго круга (test-dataN-other) — соседи по общему порядку всех dataN (engine\global_order.py),
    остальные — по порядку внутри набора."""
    from engine.global_order import GlobalOrder, is_other

    known = known_before(ds)
    return GlobalOrder(ds, known) if is_other(ds) else LocalOrder(ds, known)


def run_both(ds: Dataset, stage: StageSpec, same_character: bool) -> None:
    """Ближайшие определённые соседи слева и справа из одного тайтла (и, если same_character, с одним
    персонажем) — файл получает этот тайтл, без персонажа."""
    chosen = candidates(ds, stage)
    files = file_order(ds)
    known, order = files.known, files.order

    def process(path, key, item):
        index = files.position(key)
        if index is None:
            return {"confidence": 0.0, "left": None, "right": None}, None, neighbor_event(None)
        at = bisect.bisect_left(order, index)   # сам файл не определён, поэтому его в order нет
        left = order[at - 1] if at > 0 else None
        right = order[at] if at < len(order) else None
        result = None
        if left is not None and right is not None:
            left_result, right_result = known[left], known[right]
            same = title_key(left_result) and title_key(left_result) == title_key(right_result)
            if same and same_character:
                same = (str(left_result.get("character") or "").casefold() == str(right_result.get("character") or "").casefold()
                        and bool(left_result.get("character")))
            if same:
                result = {"kind": "title_only", "series": clean_series(left_result.get("series")), "character": "",
                          "tags": {}, "model": stage.label}
        details = {"confidence": 0.0,
                   "left": files.name_at(left),
                   "right": files.name_at(right)}
        return details, result, neighbor_event(result)

    run_stage(ds, stage, chosen, process)


def run(ds: Dataset, stage: StageSpec) -> None:
    run_both(ds, stage, same_character=True)
