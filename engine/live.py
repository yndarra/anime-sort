r"""configs\live.json — параметры, которые можно менять на ходу.

Консоль main.py следит за файлом: правильная правка публикуется в logs\live_effective.json (и в консоли
пишется, что на что поменялось), ошибочная — только выводится красным, работа идёт со старыми значениями.
Конвейеры читают опубликованные значения (current()), поэтому до них доходят только проверенные правки.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from engine.jsonconf import load as load_json

PROJECT = Path(__file__).resolve().parent.parent
LIVE = Path(os.environ.get("ANIME_SORT_LIVE") or PROJECT / "configs" / "live.json")
EFFECTIVE = PROJECT / "logs" / "live_effective.json"


@dataclass
class LiveSettings:
    free_cores: int = 2                 # сколько ядер процессора не трогают локальные теггеры (WD-14, Camie)
    memory_wait_seconds: int = 60       # пауза перед повтором файла, если не хватило оперативной памяти
    memory_retries: int = 30            # сколько раз подряд ждать память, прежде чем считать это ошибкой файла
    crash_retries: int = 2              # сколько раз сразу перезапускать набор после аварийного падения
    broken_after_runs: int = 3          # файл не прошёл этап в стольких запусках (другие прошли) — битый
    broken_after_crashes: int = 2       # процесс конвейера падал на файле столько раз — битый
    failover_errors: int = 5            # столько сбоев API подряд — переход к следующему API этапа
    failover_minutes: int = 10          # ошибки идут столько минут без единого успеха — переход к следующему
    failover_recheck_minutes: int = 20  # упавший API «спит» столько минут, потом проверяется снова
    failover_quota_recheck_minutes: int = 120   # API с исчерпанным балансом проверяется снова через столько минут


BOOL_KEYS: set[str] = set()   # параметры true/false (сейчас таких нет)


def limits() -> dict[str, tuple[int, int, str]]:
    total = os.cpu_count() or 1
    return {
        "free_cores": (0, max(0, total - 1), f"сколько из {total} ядер оставить свободными (0 — занимать все)"),
        "memory_wait_seconds": (5, 3600, "секунд ждать при нехватке памяти"),
        "memory_retries": (1, 1000, "сколько раз ждать память"),
        "crash_retries": (0, 20, "перезапусков набора после падения"),
        "broken_after_runs": (1, 50, "после скольких неудачных запусков этапа файл считается битым"),
        "broken_after_crashes": (1, 20, "после скольких падений процесса на файле он считается битым"),
        "failover_errors": (1, 100, "сбоев API подряд до перехода к следующему API этапа"),
        "failover_minutes": (1, 1440, "минут ошибок без успеха до перехода к следующему API"),
        "failover_recheck_minutes": (1, 1440, "минут «сна» упавшего API до повторной проверки"),
        "failover_quota_recheck_minutes": (1, 10080, "минут до повторной проверки API с исчерпанным балансом"),
    }


def validate(path: Path = LIVE) -> tuple[LiveSettings | None, list[str]]:
    """(настройки, ошибки). Ошибки — готовые строки для консоли (где, что, как исправить)."""
    if not path.exists():
        return LiveSettings(), []
    root, problems = load_json(path)
    errors = [f"[live.json, строка {line}] {what}\n      Как исправить: {fix}" for line, what, fix in problems]
    if root is None:
        return None, errors
    values = asdict(LiveSettings())
    table = limits()
    # Окна (arrangements, setups) проверяет engine\winlayout.py: они применяются не на ходу, а по кнопкам.
    from engine import winlayout

    errors += winlayout.validate(root)[1]
    for key, node in root.value.items():
        line = root.keys.get(key)
        if key in winlayout.KEYS:
            continue
        if key not in table:
            errors.append(f"[live.json, строка {line}] неизвестный параметр «{key}»\n      Как исправить: допустимы: "
                          f"{', '.join([*table, *winlayout.KEYS])}")
            continue
        low, high, about = table[key]
        if key in BOOL_KEYS:
            if node.kind != "bool":
                errors.append(f"[live.json, строка {node.line}] {key} — нужно true или false без кавычек ({about})\n"
                              f'      Как исправить: "{key}": true')
            else:
                values[key] = bool(node.value)
            continue
        if node.kind != "number" or node.value != int(node.value):
            shown = f'"{node.value}"' if node.kind == "string" else str(node.value).lower()
            errors.append(f"[live.json, строка {node.line}] {key} = {shown} — нужно целое число без кавычек ({about})\n"
                          f'      Как исправить: "{key}": {values[key]}')
            continue
        number = int(node.value)
        if not low <= number <= high:
            errors.append(f"[live.json, строка {node.line}] {key} = {number} вне диапазона {low}…{high} ({about})\n"
                          f"      Как исправить: укажите число от {low} до {high}")
            continue
        values[key] = number
    return (None if errors else LiveSettings(**values)), errors


def publish(settings: LiveSettings) -> None:
    EFFECTIVE.parent.mkdir(parents=True, exist_ok=True)
    temporary = EFFECTIVE.with_suffix(".tmp")
    temporary.write_text(json.dumps({**asdict(settings), "published_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                                    ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(EFFECTIVE)


def current() -> LiveSettings:
    """Действующие значения: опубликованные консолью; без консоли — сам live.json, если он правильный."""
    try:
        data = json.loads(EFFECTIVE.read_text(encoding="utf-8"))
        return LiveSettings(**{key: (bool(data[key]) if key in BOOL_KEYS else int(data[key]))
                               for key in asdict(LiveSettings()) if key in data})
    except (OSError, ValueError, TypeError, KeyError):
        pass
    settings, _ = validate()
    return settings or LiveSettings()


def changes(old: LiveSettings, new: LiveSettings) -> list[str]:
    before, after = asdict(old), asdict(new)
    return [f"{key}: {before[key]} → {after[key]}" for key in after if before[key] != after[key]]
