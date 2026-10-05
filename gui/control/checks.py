r"""Проверка конфигов из Пульта — тем же кодом, что движок.

Шаблон пачки собирается в настоящую пачку (tools\prepare.py: build_configs, все API считаются рабочими) и
проверяется engine\config.py: check_one; live.json — engine\live.py: validate; config.json — load_settings.
Проверяемые копии пишутся в logs\gui-check (настоящие конфиги не трогаются). Ответ — список строк: ошибки,
а «совет: …» — предупреждения, которые сохранению не мешают.
"""
from __future__ import annotations

import copy
import json
import shutil

from control import context

CHECK_DIR = context.LOGS / "gui-check"
STAGE_TYPES = ["wd14", "camie", "ai_image", "json_meta", "neighbors", "neighbors_title", "neighbors_left",
               "neighbors_right", "neighbors_lr", "neighbors_rl"]


def check_template(template: dict, mode: str) -> list[str]:
    """Шаблон → пачка (prepare.build_configs, все API считаются рабочими) → engine.config.check_one."""
    import prepare
    import probe
    from engine.config import check_one

    class AllWork(dict):
        def __missing__(self, pair):
            return probe.Probe(pair[0], pair[1], pair[2], "ok")

    current = context.settings()
    if current is None:
        return ["config.json с ошибками — проверьте его сначала"]
    folders = sorted(p.name for p in current.source.iterdir() if p.is_dir())[:1] if current.source.is_dir() else []
    if not folders:
        return ["в коллекции нет ни одной папки dataN — проверить шаблон не на чем"]
    try:
        config = prepare.build_configs(copy.deepcopy(template), AllWork(), folders)[0]
    except Exception as exc:
        return [f"шаблон не собирается в пачку: {type(exc).__name__}: {exc}"]
    shutil.rmtree(CHECK_DIR, ignore_errors=True)
    CHECK_DIR.mkdir(parents=True)
    (CHECK_DIR / "check.json").write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    _, report = check_one("check", current, [], CHECK_DIR)
    problems = [text.replace("\n", " ") for text in report.errors]
    problems += ["совет: " + text.splitlines()[0] for text in report.warnings][:10]
    if mode == "other":
        ai = [stage for stage in template.get("stages", []) if stage.get("type") == "ai_image" and stage.get("enabled", True)]
        if len(ai) < 5:
            problems.append(f"совет: во втором круге AI-этапов {len(ai)} — договорились не меньше 5")
    return problems


def check_live(data: dict) -> list[str]:
    from engine import live

    path = CHECK_DIR / "live.json"
    CHECK_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return [text.replace("\n", " ") for text in live.validate(path)[1]]


def check_main_config(data: dict) -> list[str]:
    from engine.config import Report, load_settings

    CHECK_DIR.mkdir(parents=True, exist_ok=True)
    (CHECK_DIR / "config.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    report = Report()
    load_settings(report, CHECK_DIR, check_run_files=False)
    return [text.replace("\n", " ") for text in report.errors]


def check_where(text: str) -> str:
    """Ошибка выражения where ("" — всё хорошо)."""
    from engine.config import WhereError, parse_where

    try:
        if text.strip():
            parse_where(text)
        return ""
    except WhereError as exc:
        return str(exc)


def check_file(relative: str, data: dict) -> list[str]:
    """Проверка конфига по его пути относительно configs\\."""
    if relative.endswith("template.json"):
        return check_template(data, "other" if relative.startswith("other/") else "main")
    if relative == "live.json":
        return check_live(data)
    if relative == "config.json":
        return check_main_config(data)
    return []
