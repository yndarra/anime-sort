"""Этап type = "camie": локальный теггер Camie (более новый словарь персонажей)."""
from stages.local import run_local


def run(ds, stage) -> None:
    run_local(ds, stage, "camie")
