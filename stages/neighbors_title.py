"""Этап type = "neighbors_title" (N-L&R): ближайшие определённые файлы слева и справа (по порядку сохранения)
из ОДНОГО ТАЙТЛА (персонажи не важны) — файл получает этот тайтл (без папки персонажа).

Отличие от neighbors (N): там соседи должны совпадать и по персонажу. Соседи — по состоянию на начало этапа.
"""
from stages.neighbors import run_both


def run(ds, stage) -> None:
    run_both(ds, stage, same_character=False)
