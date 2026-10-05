"""Этап type = "neighbors_lr": тайтл определённого соседа слева ИЛИ справа (персонаж не переносится).
Если определены оба соседа и тайтлы у них разные — берётся тайтл ЛЕВОГО."""
from stages.side_neighbor import run_side


def run(ds, stage) -> None:
    run_side(ds, stage, (-1, +1))
