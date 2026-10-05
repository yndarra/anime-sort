r"""Обёртка одного набора: source\dataN -> results\test-dataN.

1. Набор уже завершён с тем же набором этапов — пропускается. Если конфиг изменился (новые этапы,
   другие from/where/модели), завершённый набор открывается снова: состояние берётся из итогового
   снапшота, повторно работают только этапы, которые файлы ещё не проходили.
2. Копирует исходные файлы в in\ (источник не трогается), обновляет каталог tools\ani\ani.txt.
3. Открывает окно набора и запускает engine\run.py без консоли; падение пишется в лог окна.
4. При успехе пишет out\status_snapshot.json (итоги и состояние всех файлов) и удаляет work\.
Коды выхода — как у run.py, плюс 2 — нет исходной папки.
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import argparse  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402

from engine.config import IMAGE_EXTENSIONS, ConfigSpec, Settings, load_one  # noqa: E402
from engine.core import (EXIT_CONFIG, EXIT_PENDING, EXIT_QUOTA, MEMORY_CODES, atomic_json, config_signature,  # noqa: E402
                         file_digest, read_json, stage_stats)

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# Колонки окон: первая пачка слева, вторая справа; по вертикали — 4 слота по номеру набора.
ANI_SCRIPT = PROJECT / "tools" / "ani" / "ani.py"
ANI_CATALOG = PROJECT / "tools" / "ani" / "ani.txt"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# Служебный шум в stderr, который не объясняет падение.
STDERR_NOISE = ("=====", "Warning: You are sending unauthenticated requests", "onnxruntime::", "[E:onnxruntime",
                "[W:onnxruntime", "Please install all dependencies")


def console_python() -> Path:
    candidate = Path(sys.executable).with_name("python.exe")
    return candidate if candidate.exists() else Path(sys.executable)


def window_python() -> Path:
    candidate = Path(sys.executable).with_name("pythonw.exe")
    return candidate if candidate.exists() else Path(sys.executable)


class DatasetRun:
    def __init__(self, settings: Settings, spec: ConfigSpec, folder: str):
        self.settings = settings
        self.spec = spec
        self.folder = folder
        self.source = settings.source / folder
        self.root = settings.results / f"test-{folder}"
        self.input = self.root / "in"
        self.output = self.root / "out"
        self.work = self.root / "work"
        self.logs = self.root / "logs"
        self.log = self.logs / "pipeline_events.jsonl"
        self.snapshot = self.output / "status_snapshot.json"

    def append_event(self, message: str, **data) -> None:
        self.logs.mkdir(parents=True, exist_ok=True)
        event = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "stage": "PIPELINE", "message": message, **data}
        with self.log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def completed(self) -> bool:
        # Уже влит в Waifu (DONE.txt пишет tools\add\add.py) — заново не обрабатывается, даже если остался work\
        # или поменялись этапы конфига. Старые наборы, обработанные частями (test-data80_1 … _5), — тоже.
        if (self.root / "DONE.txt").exists():
            return True
        parts = list(self.root.parent.glob(f"{self.root.name}_*"))
        if not self.root.exists() and parts and all((part / "DONE.txt").exists() for part in parts):
            return True
        snapshot = read_json(self.snapshot)
        return (snapshot.get("status") == "completed" and not self.work.exists()
                and snapshot.get("signature") == config_signature(self.spec))

    @property
    def second_round(self) -> bool:
        """dataN-other — «Other» на второй круг (tools\\other.py): файлы не копируются, а ПЕРЕНОСЯТСЯ в in\\,
        пустая dataN-other потом удаляется. Итог всё равно КОПИРУЕТСЯ в Waifu (tools\add)."""
        return self.folder.casefold().endswith("-other")

    def prepare_input(self) -> int:
        if self.second_round:
            return self.move_input()
        files = sorted((path for path in self.source.iterdir() if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS),
                       key=lambda path: (path.stat().st_mtime_ns, path.name.casefold()))
        if not files:
            raise RuntimeError(f"в {self.source} нет картинок")
        self.input.mkdir(parents=True, exist_ok=True)
        digests = []
        for path in files:
            target = self.input / path.name
            digest = file_digest(path)
            if not target.exists() or file_digest(target) != digest:
                shutil.copy2(path, target)
            digests.append(digest)
        manifest = {"source": str(self.source), "count": len(files), "files": [path.name for path in files], "sha256": digests}
        atomic_json(self.root / "manifest.json", manifest)
        return len(files)

    def move_input(self) -> int:
        r"""Набор второго круга: перенести картинки из dataN-other в in\ (если папки уже нет — всё перенесено
        прошлым запуском), удалить опустевшую dataN-other, manifest — по всему in\."""
        self.input.mkdir(parents=True, exist_ok=True)
        if self.source.is_dir():
            for path in [p for p in self.source.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS]:
                os.replace(path, self.input / path.name)
            (self.source / "desktop.ini").unlink(missing_ok=True)
            try:
                self.source.rmdir()
                self.append_event(f"{self.source.name}: файлы перенесены в {self.input}, папка удалена")
            except OSError:
                self.append_event(f"{self.source.name}: картинки перенесены, но в папке осталось что-то ещё — не удалена")
        files = sorted((path for path in self.input.iterdir() if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS),
                       key=lambda path: (path.stat().st_mtime_ns, path.name.casefold()))
        if not files:
            raise RuntimeError(f"в {self.input} нет картинок")
        manifest = {"source": str(self.source), "moved": True, "count": len(files),
                    "files": [path.name for path in files], "sha256": [file_digest(path) for path in files]}
        atomic_json(self.root / "manifest.json", manifest)
        return len(files)

    def write_layout(self) -> Path:
        layout = self.logs / "layout.json"
        atomic_json(layout, {
            "title": self.root.name,
            "config": self.spec.file,
            # providers — провайдеры этапа (основные и запасной модели): кнопка «↔» серая, если он один.
            "stages": [{"id": stage.id, "label": stage.label, "table": stage.table,
                        "providers": sorted({route.provider.name for route in stage.apis}
                                            | {route.provider.name for route in (stage.fallback.apis if stage.fallback else [])})}
                       for stage in self.spec.stages],
            "log_columns": self.settings.log_columns,
            "table_columns": self.settings.table_columns,
            "empty_cells": self.settings.empty_cells,
        })
        return layout

    def count_crash(self, code: int) -> None:
        """Процесс конвейера упал (не штатный код и не нехватка памяти) — засчитать падение файлу, над которым
        он работал (logs\\current.json). Нужное число падений — и run.py пометит файл битым."""
        current_path = self.logs / "current.json"
        current = read_json(current_path)
        current_path.unlink(missing_ok=True)
        if code in (0, EXIT_QUOTA, EXIT_PENDING, EXIT_CONFIG) or (code & 0xFFFFFFFF) in MEMORY_CODES or not current.get("file"):
            return
        crashes_path = self.logs / "crashes.json"
        crashes = read_json(crashes_path)
        record = crashes.get(current["file"], {})
        if record.get("identity") != current.get("identity"):
            record = {}
        crashes[current["file"]] = {"stage": current.get("stage"), "identity": current.get("identity"),
                                    "count": record.get("count", 0) + 1, "code": f"{code & 0xFFFFFFFF:#x}",
                                    "time": time.strftime("%Y-%m-%d %H:%M:%S")}
        atomic_json(crashes_path, crashes)
        self.append_event(f"конвейер упал на файле {current['file']} (этап {current.get('stage')}) — падение №{crashes[current['file']]['count']}")

    def write_pids(self, run: int | None = None) -> None:
        """logs\\pids.json — кто сейчас работает над набором. По нему окно набора понимает, активен ли конвейер,
        и останавливает его (кнопка «Завершить», закрытие окна) или запускает пачку снова («Продолжить»)."""
        atomic_json(self.logs / "pids.json", {
            "config": self.spec.file,
            "name": self.spec.name,
            "batch": int(os.environ.get("ANIME_SORT_BATCH_PID") or 0),
            "batch_log": str(PROJECT / "logs" / f"batch_{self.spec.name}.log"),
            "dataset": os.getpid(),
            "run": run,
            "folder": self.folder,
        })

    def launch_gui(self, layout: Path, env: dict) -> None:
        # Размер и место окно берёт само из logs\windows.json (engine\winlayout.py): сетап и режим расстановки.
        try:
            subprocess.Popen([
                str(window_python()), str(PROJECT / "gui" / "window.py"),
                "--log", str(self.log), "--snapshot", str(self.snapshot), "--layout", str(layout),
                "--title", self.root.name,
                "--capture-log", str(self.logs / "pipeline_log_viewer.txt"),
                "--capture-table", str(self.logs / "monitor_100_status.txt"),
            ], env=env, creationflags=NO_WINDOW)
        except OSError as exc:
            self.append_event(f"ошибка: не удалось открыть окно — {exc}")

    def update_catalog(self, env: dict) -> None:
        """Каталог ani.txt (частые тайтлы/персонажи Waifu) — подсказка для json_meta; сбой не критичен."""
        if not ANI_SCRIPT.exists() or not self.settings.waifu.is_dir():
            return
        result = subprocess.run(
            [str(console_python()), str(ANI_SCRIPT), "--root", str(self.settings.waifu), "--output", str(ANI_CATALOG),
             "--J", "30", "--K", "15", "--no-pause"],
            env=env, check=False, capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
        if result.returncode != 0:
            self.append_event("ошибка: ani.py не обновил каталог, используется прежний ani.txt — " + tail(result.stdout + result.stderr))

    def write_snapshot(self) -> int:
        items: dict = {}
        for state_file in self.work.glob("chunk_*/_state.json"):
            items.update(read_json(state_file).get("items", {}))
        stages = {}
        if self.spec.keep_previous:
            # Итоги этапов прошлых прогонов набора остаются в снапшоте рядом с этапами этого конфига.
            stages.update(read_json(self.snapshot).get("stages") or {})
        for stage in self.spec.stages:
            stats = stage_stats(items, stage.id)
            stages[stage.id] = {"label": stage.label, "table": stage.table, "expected": stats["expected"],
                                "determined": stats["determined"], "not_determined": stats["not_determined"],
                                "errors": stats["errors"], "remained": 0, "started": True, "completed": True, "k": stage.accept}
        resolved = sum(1 for item in items.values() if item.get("result"))
        atomic_json(self.snapshot, {
            "status": "completed", "completed_at": time.strftime("%Y-%m-%d %H:%M:%S"), "config": self.spec.file,
            "signature": config_signature(self.spec), "total": len(items), "resolved": resolved,
            "stages": stages, "items": items,
        })
        return len(items)

    def run(self) -> int:
        if self.completed():
            print(f"already_completed={self.root}")
            return 0
        if not self.source.is_dir() and not (self.second_round and self.input.is_dir()):
            return 2
        # Набор снова в работе (новые этапы в конфиге и т. п.) — значок «готово» снимается до конца прогона.
        try:
            from engine import marks

            marks.clear_mark(self.root)
            marks.clear_mark(self.source)
        except Exception:
            pass
        self.prepare_input()
        for folder in (self.output, self.work, self.logs, self.root / "cache"):
            folder.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        # Модели WD-14/Camie уже в локальном кэше HuggingFace: без сети не было нативных падений (0xC000070A).
        env.setdefault("HF_HUB_OFFLINE", "1")
        # CUDA-библиотек для onnxruntime нет: без этого каждая загрузка модели сначала пробует GPU.
        env.setdefault("ONNX_MODE", "cpu")
        env["WAIFU_ROOT"] = str(self.settings.waifu)
        # Новый запуск набора не наследует паузу прошлого (кнопка «Остановить» в окне).
        atomic_json(self.logs / "control.json", {"paused": False})
        self.write_pids()
        self.launch_gui(self.write_layout(), env)
        self.update_catalog(env)
        stderr_path = self.logs / "pipeline_stderr.log"
        with stderr_path.open("a", encoding="utf-8") as stderr_handle:
            stderr_handle.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
            stderr_handle.flush()
            process = subprocess.Popen(
                [str(console_python()), str(PROJECT / "engine" / "run.py"), "--config", self.spec.file, "--root", str(self.root)],
                env=env, stdout=subprocess.DEVNULL, stderr=stderr_handle, creationflags=NO_WINDOW)
            self.write_pids(run=process.pid)
            code = process.wait()
        self.count_crash(code)
        if code == EXIT_CONFIG:
            self.append_event("ОСТАНОВКА: ошибка в конфиге — " + tail(read_tail(stderr_path), 6))
        elif code not in (0, EXIT_QUOTA, EXIT_PENDING):
            self.append_event(f"ОСТАНОВКА: конвейер упал (код {code}) — " + tail(read_tail(stderr_path)))
        if code == 0:
            total = self.write_snapshot()
            shutil.rmtree(self.work)
            # Значок «готово» у test-dataN и у исходной dataN (engine\marks.py); сбой значка не мешает набору.
            try:
                from engine import marks

                marks.mark_done(self.root)
                marks.mark_done(self.source)
            except Exception as exc:
                self.append_event(f"значок «готово» не поставлен: {exc}")
            print(f"completed={total} output={self.output}")
        return code


def read_tail(path: Path, limit: int = 8000) -> str:
    """Хвост stderr текстом. onnxruntime пишет туда в UTF-16 — такие строки декодируются отдельно."""
    try:
        data = path.read_bytes()[-limit:]
    except OSError:
        return ""
    lines = data.split(b"\n")[1:] if len(data) >= limit else data.split(b"\n")
    decoded = []
    for line in lines:
        if line.count(b"\x00") > len(line) // 4:
            chunk = line.strip(b"\x00\r")
            if len(chunk) % 2:
                chunk += b"\x00"
            decoded.append(chunk.decode("utf-16-le", errors="replace"))
        else:
            decoded.append(line.decode("utf-8", errors="replace"))
    return "\n".join(decoded)


def tail(text: str, lines: int = 3) -> str:
    """Последние осмысленные строки stderr: без NUL, цветовых кодов и шума."""
    clean = ANSI_RE.sub("", text.replace("\x00", ""))
    parts = [line.strip() for line in clean.splitlines() if line.strip()]
    parts = [line for line in parts if not any(marker in line for marker in STDERR_NOISE)]
    return " | ".join(parts[-lines:])[:600] or "без сообщения об ошибке (процесс мог быть остановлен извне)"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--folder", required=True)
    args = parser.parse_args()
    try:
        settings, spec = load_one(args.config)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr, flush=True)
        return EXIT_CONFIG
    return DatasetRun(settings, spec, args.folder).run()


if __name__ == "__main__":
    raise SystemExit(main())
