r"""Пачка одного конфига: папки из folders по порядку, каждая — через engine\dataset.py.

- Одна пачка на конфиг: повторный запуск того же конфига, пока пачка работает, сразу завершается.
- Аварийное падение набора — до двух перезапусков подряд (работа продолжается с места падения);
  падение из-за нехватки памяти перезапускается после паузы и попыток не тратит;
  наборы, где остались файлы с ошибками, повторяются в конце (до двух проходов).
- Кончился баланс ключа или сломался конфиг — пачка останавливается; следующий запуск продолжит с того же места.
- Закрыли консоль start.vbs (engine\main.py) — пачка останавливается вместе со всем, что запустила.
Журнал: logs\batch_<name>.log — его строки показывает консоль main.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import argparse  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import subprocess  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

from engine import live, winproc  # noqa: E402
from engine.config import load_one  # noqa: E402
from engine.core import EXIT_CONFIG, EXIT_PENDING, EXIT_QUOTA, MEMORY_CODES  # noqa: E402

FINAL_RETRY_PASSES = 2
MEMORY_CRASH_WAIT = 120
MEMORY_CRASH_TRIES = 15
EXIT_NO_SOURCE = 2
KNOWN_CODES = {0, EXIT_NO_SOURCE, EXIT_QUOTA, EXIT_CONFIG, EXIT_PENDING}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SKIPPED = -1          # папка уже готова с тем же набором этапов — пропущена без строк в журнале


def mutex_name(config: str) -> str:
    return f"Local\\anime-sort-batch-{config.casefold()}"


def acquire_mutex(config: str):
    """Именованный мьютекс Windows: None — пачка этого конфига уже работает."""
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, False, mutex_name(config))
        if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            return None
        return handle  # держим до конца процесса
    except Exception:
        return True


def watch_main() -> None:
    """Консоль main.py закрыта — остановить пачку со всем, что она запустила (запасной путь к job object)."""
    main_pid = os.environ.get("ANIME_SORT_MAIN_PID")
    if not main_pid:
        return

    def loop():
        while winproc.pid_alive(main_pid):
            time.sleep(2)
        winproc.kill_tree(os.getpid())

    threading.Thread(target=loop, daemon=True).start()


def last_stop_reason(folder_root: Path) -> str:
    """Причина остановки набора — последняя строка «ОСТАНОВКА …» / «не завершено …» из его лога."""
    try:
        lines = (folder_root / "logs" / "pipeline_events.jsonl").read_text(encoding="utf-8").splitlines()[-40:]
    except OSError:
        return ""
    for line in reversed(lines):
        try:
            message = str(json.loads(line).get("message", ""))
        except ValueError:
            continue
        if message.startswith("Начало работы"):
            return ""
        if message.startswith(("ОСТАНОВКА", "не завершено")):
            return message.removeprefix("ОСТАНОВКА:").strip()[:400]
    return ""


class Batch:
    def __init__(self, config: str):
        self.config = config
        self.name = config
        self.log_path: Path | None = None
        self.results: Path | None = None
        self.skipped = 0

    def say(self, text: str) -> None:
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {text}"
        if sys.stdout is not None:
            try:
                print(line, flush=True)
            except (OSError, ValueError):
                pass
        if self.log_path is not None:
            try:
                with self.log_path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                pass

    def reason(self, folder: str) -> str:
        text = last_stop_reason(self.results / f"test-{folder}") if self.results else ""
        return f" — {text}" if text else ""

    def run_dataset(self, folder: str) -> int:
        return subprocess.run([sys.executable, str(PROJECT / "engine" / "dataset.py"), "--config", self.config, "--folder", folder],
                              check=False, creationflags=NO_WINDOW).returncode

    def already_done(self, folder: str) -> bool:
        try:
            from engine.dataset import DatasetRun

            settings, spec = load_one(self.config)
            return DatasetRun(settings, spec, folder).completed()
        except Exception:
            return False

    def run_dataset_safe(self, folder: str) -> int:
        if self.already_done(folder):
            self.skipped += 1
            return SKIPPED
        self.say(f"{folder}: начата")
        code = self.run_dataset(folder)
        crashes = memory_waits = 0
        while code not in KNOWN_CODES:
            if (code & 0xFFFFFFFF) in MEMORY_CODES and memory_waits < MEMORY_CRASH_TRIES:
                memory_waits += 1
                self.say(f"{folder}: не хватило оперативной памяти (код {code & 0xFFFFFFFF:#x}) — "
                         f"повтор через {MEMORY_CRASH_WAIT}с ({memory_waits}/{MEMORY_CRASH_TRIES})")
                time.sleep(MEMORY_CRASH_WAIT)
            elif crashes < live.current().crash_retries:
                crashes += 1
                self.say(f"{folder}: ошибка — конвейер упал (код {code}){self.reason(folder)}; перезапуск {crashes}/{live.current().crash_retries}")
                time.sleep(10)
            else:
                self.say(f"{folder}: ошибка — конвейер упал (код {code}) после {crashes} перезапусков{self.reason(folder)}; повторю в конце")
                return EXIT_PENDING
            code = self.run_dataset(folder)
        return code

    def handle(self, folder: str, code: int, incomplete: list[str]) -> int | None:
        """None — идти дальше; число — остановить пачку с этим кодом."""
        if code == SKIPPED:
            pass
        elif code == 0:
            self.say(f"{folder}: закончена")
        elif code == EXIT_PENDING:
            self.say(f"{folder}: закончена с ошибками на части файлов{self.reason(folder)} — повторю в конце")
            incomplete.append(folder)
        elif code == EXIT_NO_SOURCE:
            self.say(f"{folder}: пропущена — нет исходной папки")
        elif code == EXIT_QUOTA:
            self.say(f"{folder}: остановлена — баланс ключа исчерпан. Пополните и нажмите «Продолжить» в окне набора "
                     f"(или запустите start.vbs) — продолжит с {folder}")
            return code
        elif code == EXIT_CONFIG:
            self.say(f"{folder}: остановлена — ошибка в конфиге{self.reason(folder)}")
            return code
        return None

    def run(self) -> int:
        try:
            settings, spec = load_one(self.config)
        except RuntimeError as exc:
            self.log_path = PROJECT / "logs" / f"batch_{self.config}.log"
            self.say(f"пачка {self.config} остановлена — {exc}")
            return EXIT_CONFIG
        self.name = spec.name
        self.results = settings.results
        self.log_path = PROJECT / "logs" / f"batch_{spec.name}.log"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        incomplete: list[str] = []
        for folder in spec.folders:
            stop = self.handle(folder, self.run_dataset_safe(folder), incomplete)
            if stop is not None:
                return stop
        if self.skipped:
            self.say(f"пачка {self.config}: уже готовы и пропущены папок: {self.skipped}")
        for retry_pass in range(1, FINAL_RETRY_PASSES + 1):
            if not incomplete:
                break
            self.say(f"пачка {self.config}: повтор незавершённых папок ({retry_pass}/{FINAL_RETRY_PASSES}): {', '.join(incomplete)}")
            pending, incomplete = incomplete, []
            for folder in pending:
                stop = self.handle(folder, self.run_dataset_safe(folder), incomplete)
                if stop is not None:
                    return stop
        if incomplete:
            self.say(f"пачка {self.config} закончена, с ошибками остались: {', '.join(incomplete)} — запустите позже")
            return EXIT_PENDING
        self.say(f"пачка {self.config} закончена: все папки")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    instance = acquire_mutex(args.config)
    if instance is None:
        return 0
    os.environ["ANIME_SORT_BATCH_PID"] = str(os.getpid())
    watch_main()
    return Batch(args.config).run()


if __name__ == "__main__":
    raise SystemExit(main())
