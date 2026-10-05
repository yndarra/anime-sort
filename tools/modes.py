r"""Режимы работы anime-sort и их конфиги.

    main   — обычные пачки dataN (свежие картинки):        configs\main\template.json
    other  — второй круг: Waifu\Other → временные dataN-other: configs\other\template.json
                                                             configs\other\settings.json (размер dataN-other,
                                                             порог мелких тайтлов)
Общие для обоих режимов: configs\config.json, live.json, agent.json; config1…N.json пишет prepare.

Старая раскладка (до 05.10: configs\template.json, template-other.json, tools.json) читается как запасная —
migrate() переносит её в новую при первом запуске prepare или Пульта.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
CONFIGS = PROJECT / "configs"
MODES = ("main", "other")
TITLES = {"main": "Обычный режим (dataN)", "other": "Второй круг (Other → dataN-other)"}
LEGACY_TEMPLATES = {"main": "template.json", "other": "template-other.json"}
OTHER_DEFAULTS = {"other_batch_size": 500, "small_title_max_files": 15}


def mode_of(folder: str) -> str:
    """Режим папки набора: dataN-other — второй круг, остальные — обычный."""
    return "other" if folder.casefold().endswith("-other") else "main"


def template_path(mode: str) -> Path:
    path = CONFIGS / mode / "template.json"
    legacy = CONFIGS / LEGACY_TEMPLATES[mode]
    return path if path.exists() or not legacy.exists() else legacy


def load_template(mode: str) -> dict:
    return json.loads(template_path(mode).read_text(encoding="utf-8"))


def other_settings() -> dict:
    """configs\\other\\settings.json (по умолчанию — OTHER_DEFAULTS; запасной вариант — старый tools.json)."""
    for path in (CONFIGS / "other" / "settings.json", CONFIGS / "tools.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        return {**OTHER_DEFAULTS, **{key: value for key, value in data.items() if not key.startswith("//")}}
    return dict(OTHER_DEFAULTS)


def migrate() -> list[str]:
    """Старая раскладка configs\\ → configs\\main|other\\. → что перенесено (для журнала)."""
    moved = []
    for mode, name in LEGACY_TEMPLATES.items():
        old, new = CONFIGS / name, CONFIGS / mode / "template.json"
        if old.exists() and not new.exists():
            new.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old), str(new))
            moved.append(f"{name} → {mode}\\template.json")
    old, new = CONFIGS / "tools.json", CONFIGS / "other" / "settings.json"
    if old.exists() and not new.exists():
        data = json.loads(old.read_text(encoding="utf-8"))
        data["//"] = "Настройки второго круга (Other → dataN-other): размер временных папок и порог мелких тайтлов"
        new.parent.mkdir(parents=True, exist_ok=True)
        new.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        old.unlink()
        moved.append("tools.json → other\\settings.json")
    return moved
