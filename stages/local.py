"""Общий код локальных теггеров (этапы wd14 и camie): порог этапа (accept) — порог character-тегов."""
from __future__ import annotations

from engine.config import StageSpec
from engine.core import Dataset, is_unknown_title, pretty
from stages.common import candidates, dash, run_stage


def run_local(ds: Dataset, stage: StageSpec, model: str) -> None:
    chosen = candidates(ds, stage)

    def process(path, key, item):
        from engine import tagger

        tags = tagger.probe(str(path), model)
        accepted = {tag: float(value) for tag, value in tags.items() if value >= stage.accept}
        kind, series, character = tagger.decide_destination(accepted)
        result = None
        # Группа персонажей из разных тайтлов (mixed_group) — не определение: несколько тайтлов сразу.
        if kind in ("character", "series_group") and series and not is_unknown_title(series):
            result = {"kind": kind, "series": series, "character": character, "tags": accepted, "model": stage.label}
            confidence = max(accepted.values())
            title, characters = pretty(series), pretty(character) if character else "—"
        elif tags:
            # Не определил: в лог идёт реальная уверенность и лучший вариант модели (ниже порога
            # или не сопоставленный с тайтлом), а не 0.00 из-за того, что порог отсёк все теги.
            best_tag, best = max(tags.items(), key=lambda pair: pair[1])
            confidence = float(best)
            series_guess = tagger.get_series(best_tag)
            title = pretty(series_guess) if series_guess else "—"
            characters = pretty(tagger.base_character_name(best_tag))
        else:
            confidence, title, characters = 0.0, "—", "—"
        details = {"model": model, "confidence": confidence, "title": title, "characters": characters}
        return details, result, {"model": stage.label, "confidence": confidence, "title": dash(title), "characters": dash(characters)}

    run_stage(ds, stage, chosen, process)
