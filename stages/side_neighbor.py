r"""Общий код этапов «сосед даёт тайтл»: neighbors_left, neighbors_right, neighbors_lr, neighbors_rl.

Файл получает ТАЙТЛ (без персонажа) непосредственно соседнего файла, если тот определён.
sides — в каком порядке смотреть соседей: (-1,) только слева, (+1,) только справа,
(-1, +1) слева, а если слева не определён — справа (при разных тайтлах слева и справа побеждает левый),
(+1, -1) наоборот. Для наборов test-dataN-other соседи — по общему порядку всех dataN
(engine\global_order.py). Берётся состояние НА НАЧАЛО этапа (определения этапов выше), поэтому
определение не «течёт» по цепочке из нескольких файлов.
"""
from __future__ import annotations

from engine.config import StageSpec
from engine.core import Dataset, clean_series
from stages.common import candidates, run_stage
from stages.neighbors import file_order, neighbor_event


def run_side(ds: Dataset, stage: StageSpec, sides: tuple[int, ...]) -> None:
    chosen = candidates(ds, stage)
    files = file_order(ds)
    known = files.known

    def process(path, key, item):
        index = files.position(key)
        result, source_key, neighbors = None, None, {}
        for step in sides if index is not None else ():
            neighbor = index + step
            if files.name_at(neighbor):
                neighbors["left" if step < 0 else "right"] = files.name_at(neighbor)
            source = known.get(neighbor)
            if result is None and source and clean_series(source.get("series")):
                result = {"kind": "title_only", "series": clean_series(source.get("series")), "character": "",
                          "tags": {}, "model": stage.label}
                source_key = files.name_at(neighbor)
        details = {"confidence": 0.0, **neighbors, "source": source_key}
        return details, result, neighbor_event(result)

    run_stage(ds, stage, chosen, process)
