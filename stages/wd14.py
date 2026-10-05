"""Этап type = "wd14": локальный теггер WD-14 (EVA02-Large)."""
from stages.local import run_local


def run(ds, stage) -> None:
    run_local(ds, stage, "wd14")
