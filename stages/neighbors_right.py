"""Этап type = "neighbors_right": тайтл определённого соседа СПРАВА (персонаж не переносится)."""
from stages.side_neighbor import run_side


def run(ds, stage) -> None:
    run_side(ds, stage, (+1,))
