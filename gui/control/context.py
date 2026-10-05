r"""Общее для вкладок Пульта: пути проекта, настройки коллекции, наборы, запуск/остановка конвейеров,
провайдеры и ключи. Ничего из движка не переписывается — всё через engine\config.py, tools\modes.py и т. п.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
CONFIGS = PROJECT / "configs"
LOGS = PROJECT / "logs"
PROVIDERS = PROJECT / "providers"
KEYS = PROJECT / "secrets" / "providers"
AGENT_KEY = PROJECT / "secrets" / "agent" / "openrouter.txt"
FOLDER_RE = re.compile(r"^data(\d+)(-other)?$")


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {} if default is None else default


def write_json(path: Path, data) -> None:
    """Атомарная запись (как engine\\core.atomic_json): сначала .tmp, потом замена."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def settings():
    """Настройки config.json (engine\\config.load_settings) или None, если в config.json ошибки."""
    from engine.config import Report, load_settings

    return load_settings(Report(), check_run_files=False)


# ---------------------------------------------------------------- наборы

@dataclass
class Dataset:
    name: str            # data12 / data90-other
    mode: str            # main / other
    status: str          # new / active / started / completed / done
    resolved: int
    total: int
    broken: int
    source: Path | None
    result: Path


STATUS_TEXT = {"new": "Не начат", "active": "В работе", "started": "Прерван", "completed": "Готов",
               "done": "Влит в Waifu"}


def number_key(name: str) -> tuple:
    match = FOLDER_RE.match(name)
    return (int(match.group(1)), bool(match.group(2))) if match else (10 ** 9, name)


def datasets() -> list[Dataset]:
    """Все наборы коллекции: папки dataN и результаты test-dataN (у dataN-other источник после переноса удалён)."""
    from stats import Stats

    current = settings()
    if current is None:
        return []
    names = {p.name for p in current.source.iterdir() if p.is_dir() and FOLDER_RE.match(p.name)} if current.source.is_dir() else set()
    if current.results.is_dir():
        names |= {p.name.removeprefix("test-") for p in current.results.iterdir()
                  if p.is_dir() and FOLDER_RE.match(p.name.removeprefix("test-"))}
    found = []
    for name in sorted(names, key=number_key):
        result = current.results / f"test-{name}"
        status = Stats.dataset_status(result)
        resolved, total = Stats.progress(result) if status in ("active", "started") else (0, 0)
        snapshot = read_json(result / "out" / "status_snapshot.json")
        if not total and snapshot.get("items"):
            items = snapshot["items"]
            total, resolved = len(items), sum(1 for item in items.values() if item.get("result"))
        try:   # out\broken.txt — заголовок + по строке на битый файл (engine\core.py: write_broken_list)
            broken = max(0, len((result / "out" / "broken.txt").read_text(encoding="utf-8").splitlines()) - 1)
        except OSError:
            broken = 0
        source = current.source / name
        found.append(Dataset(name, "other" if name.endswith("-other") else "main", status, resolved, total, broken,
                             source if source.exists() else None, result))
    return found


# ---------------------------------------------------------------- конвейеры

def pipelines_running() -> bool:
    """Открыто главное окно anime-sort (engine\\main.py держит mutex Local\\anime-sort-main)."""
    import ctypes

    kernel32 = ctypes.windll.kernel32
    kernel32.OpenMutexW.restype = ctypes.c_void_p
    handle = kernel32.OpenMutexW(0x00100000, False, "Local\\anime-sort-main")
    if handle:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return True
    return False


def start_pipelines() -> None:
    """Запуск конвейеров — как двойной щелчок по start.vbs (через explorer, не дочерним процессом Пульта)."""
    subprocess.Popen(["explorer.exe", str(PROJECT / "start.vbs")])


def stop_pipelines() -> None:
    """Попросить главное окно закрыться (engine\\main.py: Dispatcher.check_control) — вместе с пачками и окнами наборов."""
    write_json(LOGS / "main_control.json", {"quit": time.strftime("%Y-%m-%d %H:%M:%S")})


def open_path(path: Path) -> None:
    os.startfile(str(path))  # noqa: S606 — открыть папку/файл в проводнике


class DispatcherView:
    """Замена диспетчера для gui\\stats.py: настройки и запущенные конфиги (по "run" из config.json)."""

    def __init__(self):
        from engine.config import Report, load_config

        self.settings = settings()
        self.started = []
        if self.settings is not None:
            for name in self.settings.run:
                spec = load_config(name, self.settings, Report())
                if spec is not None:
                    self.started.append(spec)


# ---------------------------------------------------------------- провайдеры и ключи

def providers() -> dict[str, dict]:
    """Провайдер → provider.json (только папки, где он есть)."""
    found = {}
    for folder in sorted(p for p in PROVIDERS.iterdir() if p.is_dir()) if PROVIDERS.is_dir() else []:
        data = read_json(folder / "provider.json")
        if data:
            found[folder.name] = data
    return found


def key_routes() -> list[tuple[str, bool]]:
    """Все «провайдер/ключ» из secrets\\providers → [(маршрут, есть ли непустой ключ)]. Сами ключи не читаются наружу."""
    routes = []
    for folder in sorted(p for p in KEYS.iterdir() if p.is_dir()) if KEYS.is_dir() else []:
        for key in sorted(folder.glob("*.txt")):
            if key.name.casefold() == "readme.txt":
                continue
            try:
                filled = bool(key.read_text(encoding="utf-8-sig").strip())
            except OSError:
                filled = False
            routes.append((f"{folder.name}/{key.stem}", filled))
    return routes


def models() -> list[str]:
    return sorted({model for data in providers().values() for model in (data.get("models") or {})})


def stage_names() -> dict[str, tuple[str, str]]:
    """id этапа → (метка, название в таблице) из config.json "names"."""
    data = read_json(CONFIGS / "config.json")
    names = {}
    for stage_id, value in (data.get("names") or {}).items():
        if isinstance(value, list) and value:
            names[stage_id] = (value[0], value[-1])
    return names
