"""Этап type = "neighbors_left": тайтл определённого соседа СЛЕВА (персонаж не переносится)."""
from stages.side_neighbor import run_side


def run(ds, stage) -> None:
    run_side(ds, stage, (-1,))
