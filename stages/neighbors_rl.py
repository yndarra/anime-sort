"""Этап type = "neighbors_rl": тайтл определённого соседа справа ИЛИ слева (персонаж не переносится).
Если определены оба соседа и тайтлы у них разные — берётся тайтл ПРАВОГО."""
from stages.side_neighbor import run_side


def run(ds, stage) -> None:
    run_side(ds, stage, (+1, -1))
