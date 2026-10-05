r"""Главное окно anime-sort (start.vbs): проверяет конфиги, запускает пачки из run и показывает их журнал.

Окно — gui\main_window.py (слева журнал, справа статистика всех пачек, под ней кнопки окон); оно живёт в главном
потоке, а сам диспетчер (этот файл: конфиги, пачки, live.json) — в фоновом. start.vbs запускает pythonw —
консоли больше нет; всё, что раньше печаталось в консоль, идёт в журнал окна (и в logs\console.log).

- config.json и configN.json читаются только при старте; их копия — снимок logs\config-snapshots\<время>,
  пачки читают именно его, поэтому правки конфигов во время работы ни на что не влияют (и не теряются).
- Конфиг с ошибками не мешает остальным: правильные пачки запускаются сразу, а консоль ждёт, пока
  сломанный файл исправят и сохранят, проверяет его снова и запускает только эту пачку. То же с config.json:
  пока он с ошибками, не запускается ничего, после исправления — всё.
- configs\live.json — параметры на ходу: каждая правка проверяется, в консоли пишется, что на что поменялось;
  ошибка — выводится красным, работа идёт со старыми значениями.
- Журнал всех пачек: папка начата / закончена / ошибка (текст) / остановлена, кнопки окон наборов.
- Окна (engine\winlayout.py): при старте главное окно и все новые окна наборов получают сетап 1 и места по режиму
  из live.json; дальше размеры и места меняются только кнопками главного окна.
- Закрыли главное окно — останавливаются все пачки, конвейеры и окна наборов (job object Windows;
  запасной путь — пачки и окна сами следят, жив ли этот процесс).
- Любая ошибка выводится красным, окно не закрывается само.

python engine\main.py --check    только проверить конфиги и вывести отчёт (ничего не запускать).
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import argparse  # noqa: E402
import ctypes  # noqa: E402
import os  # noqa: E402
import queue  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import traceback  # noqa: E402

from engine import live, winlayout, winproc  # noqa: E402
from engine.batch import mutex_name  # noqa: E402
from engine.config import ConfigSpec, Report, Settings, check_all, check_one, load_settings  # noqa: E402

CHECK_FILE = PROJECT / "logs" / "config_check.txt"
LOGS = PROJECT / "logs"
SNAPSHOTS = PROJECT / "logs" / "config-snapshots"
KEEP_SNAPSHOTS = 50
RED, GREEN, YELLOW, GREY, WHITE, RESET = "\x1b[31m", "\x1b[32m", "\x1b[33m", "\x1b[90m", "\x1b[97m", "\x1b[0m"
# Строки журнала для главного окна: (текст, код цвета) — окно забирает их из очереди (gui\main_window.py).
GUI_LOG: queue.Queue | None = None


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def say(text: str, color: str = "") -> None:
    """Строка в журнал главного окна (цветом), в консоль, если она есть (--check), и в logs\\console.log."""
    if GUI_LOG is not None:
        code = color[2:-1] if color.startswith("\x1b[") else None
        for line in text.splitlines() or [""]:
            GUI_LOG.put((line, code))
    try:
        if sys.stdout is not None:
            print(f"{color}{text}{RESET}" if color else text, flush=True)
    except Exception:
        pass
    try:
        with (LOGS / "console.log").open("a", encoding="utf-8") as handle:
            handle.write(text + "\n")
    except OSError:
        pass


def report_text(report: Report) -> str:
    lines = [f"Проверка конфигов: {now()}", ""]
    if report.errors:
        lines.append(f"ОШИБКИ ({len(report.errors)}):")
        lines += [f"  {index}. {text}" for index, text in enumerate(report.errors, 1)]
        lines.append("")
    if report.warnings:
        lines.append(f"Предупреждения ({len(report.warnings)}):")
        lines += [f"  - {text}" for text in report.warnings]
        lines.append("")
    if not report.errors:
        lines.append("Ошибок нет.")
    return "\n".join(lines) + "\n"


def show_report(report: Report, title: str) -> None:
    """Ошибки — красным (подсказки — зелёным), предупреждения — жёлтым; копия — в logs\\config_check.txt."""
    try:
        CHECK_FILE.parent.mkdir(parents=True, exist_ok=True)
        CHECK_FILE.write_text(report_text(report), encoding="utf-8")
    except OSError:
        pass
    if report.errors:
        say(f"[{now()}] {title}: ошибок — {len(report.errors)}", RED)
        for text in report.errors:
            first, _, fix = text.partition("\n")
            say(f"  {first}", RED)
            if fix:
                say(f"  {fix.strip()}", GREEN)
    for text in report.warnings:
        first, _, tip = text.partition("\n")
        say(f"  {first}", YELLOW)
        if tip:
            say(f"  {tip.strip()}", YELLOW)


def batch_running(config: str) -> bool:
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenMutexW.restype = ctypes.c_void_p
    handle = kernel32.OpenMutexW(0x00100000, False, mutex_name(config))  # SYNCHRONIZE
    if handle:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return True
    return False


def single_instance():
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, "Local\\anime-sort-main")
    return None if kernel32.GetLastError() == 183 else handle


def color_for(line: str) -> str:
    text = line.split("] ", 2)[-1]
    if re.search(r"ошибка|упал|не хватило", text):
        return RED
    if re.search(r"остановлена", text):
        return RED if "баланс" in text or "конфиге" in text else YELLOW
    if re.search(r": закончена\b|закончена: все папки", text):
        return GREEN
    if re.search(r"начата|запущена|продолж|пауза", text):
        return WHITE
    return GREY


def mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


class Tail:
    """Новые строки журналов пачек (logs\\batch_<name>.log)."""

    def __init__(self):
        self.offsets = {path: path.stat().st_size for path in LOGS.glob("batch_*.log")}

    def lines(self) -> list[str]:
        new = []
        for path in LOGS.glob("batch_*.log"):
            try:
                size = path.stat().st_size
                offset = self.offsets.get(path, 0)
                if size < offset:
                    offset = 0
                if size > offset:
                    with path.open("rb") as handle:
                        handle.seek(offset)
                        data = handle.read(size - offset)
                    complete = data.rfind(b"\n") + 1
                    self.offsets[path] = offset + complete
                    new += [line for line in data[:complete].decode("utf-8", errors="replace").splitlines() if line.strip()]
            except OSError:
                continue
        return sorted(new, key=lambda line: line[:21])   # по времени; внутри секунды — порядок файла


class Dispatcher:
    def __init__(self, configs: Path):
        self.configs = configs                       # откуда читаются конфиги (обычно configs\)
        self.snapshot = SNAPSHOTS / time.strftime("%Y-%m-%d_%H-%M-%S")
        self.settings: Settings | None = None
        self.settings_mtime: float | None = -1.0     # -1 — ещё не проверяли
        self.started: list[ConfigSpec] = []
        self.waiting: dict[str, float | None] = {}   # configN с ошибками -> mtime, при котором проверяли
        self.live_path = Path(os.environ.get("ANIME_SORT_LIVE") or configs / "live.json")
        self.live_mtime: float | None = -1.0
        self.live_values = live.LiveSettings()
        self.windows_config = winlayout.read_config(self.live_path)[0]
        self.tail = Tail()

    # ---------- снимок конфигов

    def snap(self, name: str) -> None:
        self.snapshot.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.configs / name, self.snapshot / name)

    def prune_snapshots(self) -> None:
        old = sorted((path for path in SNAPSHOTS.iterdir() if path.is_dir()), key=lambda path: path.name)[:-KEEP_SNAPSHOTS]
        for path in old:
            shutil.rmtree(path, ignore_errors=True)

    # ---------- config.json

    def check_settings(self) -> None:
        stamp = mtime(self.configs / "config.json")
        if stamp == self.settings_mtime:
            return
        first = self.settings_mtime == -1.0
        self.settings_mtime = stamp
        report = Report()
        settings = load_settings(report, self.configs)
        if report.errors or settings is None:
            show_report(report, "config.json")
            say("  Ничего не запущено: исправьте config.json и сохраните — он будет проверен снова автоматически.", YELLOW)
            return
        if report.warnings:
            show_report(report, "config.json")
        if not first:
            say(f"[{now()}] config.json исправлен — запускаю пачки", GREEN)
        self.snap("config.json")
        self.settings = settings
        self.waiting = {file: -1.0 for file in settings.run}

    # ---------- configN.json

    def check_waiting(self) -> None:
        for file, checked in list(self.waiting.items()):
            stamp = mtime(self.configs / f"{file}.json")
            if stamp == checked:
                continue
            self.waiting[file] = stamp
            spec, report = check_one(file, self.settings, self.started, self.configs)
            if spec is None:
                show_report(report, f"{file}.json")
                say(f"  Пачка {file} не запущена: исправьте {file}.json и сохраните — он будет проверен снова, "
                    "остальные пачки работают дальше.", YELLOW)
                continue
            if report.warnings:
                show_report(report, f"{file}.json")
            if checked != -1.0:
                say(f"[{now()}] {file}.json исправлен", GREEN)
            del self.waiting[file]
            self.start(spec)

    def start(self, spec: ConfigSpec) -> None:
        self.snap(f"{spec.file}.json")
        self.started.append(spec)
        if batch_running(spec.file):
            say(f"[{now()}] пачка {spec.file} уже работает — второй раз не запускаю", YELLOW)
            return
        env = dict(os.environ, ANIME_SORT_CONFIGS=str(self.snapshot), ANIME_SORT_LIVE=str(self.live_path))
        subprocess.Popen([str(Path(sys.executable).with_name("pythonw.exe")), str(PROJECT / "engine" / "batch.py"),
                          "--config", spec.file], cwd=str(PROJECT), env=env, creationflags=winproc.NO_WINDOW)
        say(f"[{now()}] пачка запущена: {spec.file}, папок: {len(spec.folders)}", WHITE)

    # ---------- live.json

    def check_live(self) -> None:
        stamp = mtime(self.live_path)
        if stamp == self.live_mtime:
            return
        first = self.live_mtime == -1.0
        self.live_mtime = stamp
        values, errors = live.validate(self.live_path)
        if values is None:
            say(f"[{now()}] live.json: ошибка — " + ("работаю со значениями по умолчанию" if first else "работаю со старыми значениями"), RED)
            for text in errors:
                first_line, _, fix = text.partition("\n")
                say(f"  {first_line}", RED)
                if fix:
                    say(f"  {fix.strip()}", GREEN)
            if first:
                live.publish(self.live_values)
            return
        changed = live.changes(self.live_values, values)
        self.live_values = values
        live.publish(values)
        windows, _ = winlayout.read_config(self.live_path)
        if first:
            say(f"[{now()}] live.json:", GREY)
            for line in (f"свободных ядер: {values.free_cores} из {os.cpu_count()}",
                         f"нехватка памяти: пауза {values.memory_wait_seconds} с, до {values.memory_retries} раз подряд",
                         f"перезапусков после падения: {values.crash_retries}",
                         f"битый файл: после {values.broken_after_runs} неудачных запусков этапа или {values.broken_after_crashes} падений на нём",
                         f"смена API: после {values.failover_errors} сбоев подряд или {values.failover_minutes} мин без успеха; "
                         f"упавший API спит {values.failover_recheck_minutes} мин, без баланса — {values.failover_quota_recheck_minutes} мин",
                         *winlayout.describe(windows)):
                say(f"  {line}", GREY)
        else:
            if changed:
                say(f"[{now()}] live.json изменён: " + "; ".join(changed), WHITE)
            window_changes = winlayout.changes(self.windows_config, windows)
            if window_changes:
                say(f"[{now()}] live.json, окна (применится кнопкой в главном окне): " + "; ".join(window_changes), WHITE)
        self.windows_config = windows

    # ---------- цикл

    def step(self) -> None:
        self.check_live()
        if self.settings is None:
            self.check_settings()
        if self.settings is not None and self.waiting:
            self.check_waiting()
        for line in self.tail.lines():
            say(line, color_for(line))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="только проверить конфиги")
    args = parser.parse_args()
    if sys.stdout is not None:
        sys.stdout.reconfigure(encoding="utf-8")
    winproc.enable_ansi()
    configs = Path(os.environ.get("ANIME_SORT_CONFIGS") or PROJECT / "configs")
    if args.check:
        _, _, report = check_all(configs)
        values, live_errors = live.validate(Path(os.environ.get("ANIME_SORT_LIVE") or configs / "live.json"))
        report.errors += live_errors
        print(report_text(report))
        return 1 if report.errors else 0
    if single_instance() is None:
        # Второй запуск start.vbs: главное окно уже открыто — показать его.
        sys.path.insert(0, str(PROJECT / "gui"))
        from window import focus_existing_window

        focus_existing_window("anime-sort")
        return 0
    global GUI_LOG
    GUI_LOG = queue.Queue()
    say(f"anime-sort — {now()}", WHITE)
    # Всё, что запущено отсюда (пачки, конвейеры, окна наборов), живёт не дольше главного окна.
    job = winproc.kill_children_with_me()
    os.environ["ANIME_SORT_MAIN_PID"] = str(os.getpid())
    if job is None:
        say("Не удалось привязать пачки к главному окну (job object) — при его закрытии они остановятся по сторожу за пару секунд.", YELLOW)
    dispatcher = Dispatcher(configs)
    # Сетап 1 и первое место раскладки — главному окну; ошибки live.json покажет проверка в журнале.
    state, _ = winlayout.start_session(os.getpid(), configs)
    say(f"Окна anime-sort — монитор {winlayout.start_monitor(configs) or 'основной'} (\"monitor\" в config.json):", GREY)
    for line in winlayout.describe_monitors(winlayout.start_monitor(configs)):
        say(line, GREY)
    say(f"Снимок конфигов этого запуска: {dispatcher.snapshot}", GREY)
    say("Закрытие этого окна останавливает все пачки и окна наборов.", GREY)
    say("")
    try:
        dispatcher.prune_snapshots()
    except OSError:
        pass

    # Ширины лога и таблицы в символах — как у окон наборов (для "auto" в сетапах).
    sys.path.insert(0, str(PROJECT / "gui"))
    import tkinter as tk

    import logformat
    from main_window import MainWindow

    report = Report()
    settings = load_settings(report, configs, check_run_files=False)
    columns = logformat.Formatter({"log_columns": settings.log_columns if settings else {},
                                   "table_columns": settings.table_columns if settings else {}})
    window = MainWindow(tk, dispatcher, state, GUI_LOG, say, columns.log_width, columns.table_width)
    threading.Thread(target=dispatch_loop, args=(dispatcher,), daemon=True).start()
    window.run()
    return 0


def dispatch_loop(dispatcher: Dispatcher) -> None:
    """Фоновый поток: раз в секунду конфиги, live.json, журналы пачек. Ошибка не останавливает окно."""
    while True:
        try:
            dispatcher.step()
        except Exception:
            say(f"[{now()}] ошибка диспетчера (работа пачек продолжается):", RED)
            say(traceback.format_exc().rstrip(), RED)
            time.sleep(4)
        time.sleep(1)


def guarded() -> int:
    """Ошибка до появления окна: текст — в logs\\console.log и в окно сообщения Windows (консоли нет)."""
    try:
        return main()
    except KeyboardInterrupt:
        return 0
    except BaseException:
        text = traceback.format_exc().rstrip()
        say(f"[{now()}] anime-sort остановился из-за ошибки:", RED)
        say(text, RED)
    if "--check" in sys.argv:
        return 1
    try:
        ctypes.windll.user32.MessageBoxW(None, f"anime-sort остановился из-за ошибки:\n\n{text[-1500:]}\n\n"
                                               "Полный текст — logs\\console.log", "anime-sort", 0x10)
    except Exception:
        pass
    return 1


if __name__ == "__main__":
    raise SystemExit(guarded())
