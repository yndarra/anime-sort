from __future__ import annotations

import os
import subprocess
import sys
import traceback
from pathlib import Path


def _configured_waifu() -> Path:
    """Папка Waifu: переменная WAIFU_ROOT; иначе — из коллекции ("collection" в configs/config.json →
    <коллекция>/anime-paths.json → "waifu"); старый параметр "waifu" в config.json тоже понимается."""
    if os.environ.get("WAIFU_ROOT"):
        return Path(os.environ["WAIFU_ROOT"])
    project = Path(__file__).resolve().parent.parent
    try:
        import json

        config = json.loads((project / "configs" / "config.json").read_text(encoding="utf-8-sig"))
        collection = config.get("collection")
        if isinstance(collection, str) and collection.strip():
            paths = json.loads((Path(collection) / "anime-paths.json").read_text(encoding="utf-8-sig"))
            return Path(collection) / paths["waifu"]
        if isinstance(config.get("waifu"), str) and config["waifu"].strip():
            return Path(config["waifu"].strip())
    except Exception:
        pass
    raise RuntimeError("не найдена папка Waifu: укажите \"collection\" в configs/config.json "
                       "(папку, где лежит anime-paths.json) или переменную WAIFU_ROOT")


# Папка Waifu. Скрипты лежат в проекте anime-sort\tools, а работают с Waifu по этому пути.
WAIFU_ROOT = _configured_waifu()

RED = "\x1b[31m"
YELLOW = "\x1b[33m"
RESET = "\x1b[0m"


def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def yellow(text: str) -> str:
    return f"{YELLOW}{text}{RESET}"


def red(text: str) -> str:
    return f"{RED}{text}{RESET}"


def announce(title: str) -> None:
    """Жёлтый заголовок перед запуском дочернего алгоритма или папки."""
    print(f"\n\n{yellow(title)}", flush=True)


def child_python() -> str:
    """Из IDLE sys.executable — pythonw.exe без консоли; берём соседний python.exe."""
    from pathlib import Path

    executable = Path(sys.executable)
    if executable.name.casefold() == "pythonw.exe":
        console_executable = executable.with_name("python.exe")
        if console_executable.exists():
            return str(console_executable)
    return str(executable)


def run_streamed(name: str, script, waifu, flags: list[str]) -> None:
    """Запускает дочерний скрипт в этом же окне, печатая его вывод как свой."""
    import os

    child_env = os.environ.copy()
    child_env["PYTHONIOENCODING"] = "utf-8:replace"
    announce(f"{name}.py:")
    command = [child_python(), str(script), "--root", str(waifu), "--no-pause", *flags]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=child_env,
    )
    for stream in (completed.stdout, completed.stderr):
        if stream and stream.strip():
            print(stream.rstrip("\n"), flush=True)
    if completed.returncode != 0:
        raise RuntimeError(f"{name}.py завершился с кодом {completed.returncode}")


def report_error(exc: BaseException, with_traceback: bool = True) -> None:
    print(f"{RED}ОШИБКА: {type(exc).__name__}: {exc}{RESET}", file=sys.stderr, flush=True)
    if with_traceback:
        traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)
    sys.stderr.flush()


def pause_console() -> None:
    try:
        input("Нажмите Enter, чтобы закрыть...")
    except EOFError:
        pass


# ---------------------------------------------------------------- совместная работа с наблюдателями (tools\watch)

BUSY_DIR = Path(__file__).resolve().parent / ".busy"
RENAME_MUTEX = "Local\\anime-sort-waifu-rename"


def pid_alive(pid: int) -> bool:
    import ctypes

    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    code = ctypes.c_ulong()
    try:
        return bool(kernel32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))) and code.value == 259
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


class waifu_busy:
    """Пока идёт массовая правка Waifu (add, sort, sort_cancel, fix_name, honkai), наблюдатели tools\\watch
    ничего не переименовывают и не удаляют: иначе, например, вернули бы номера, которые снимает sort_cancel."""

    def __enter__(self):
        BUSY_DIR.mkdir(exist_ok=True)
        self.path = BUSY_DIR / str(os.getpid())
        self.path.write_text(" ".join(sys.argv), encoding="utf-8")
        return self

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)
        return False


def waifu_is_busy() -> bool:
    if not BUSY_DIR.is_dir():
        return False
    busy = False
    for path in BUSY_DIR.iterdir():
        if path.name.isdigit() and pid_alive(int(path.name)):
            busy = True
        else:
            path.unlink(missing_ok=True)   # процесс завершился, не убрав за собой отметку
    return busy


class rename_lock:
    """Общая блокировка наблюдателей на время одного переименования/удаления папки (именованный мьютекс Windows):
    нумерация тайтлов, нумерация персонажей и удаление пустых папок не меняют дерево одновременно."""

    def __init__(self, timeout_ms: int = 30000):
        self.timeout_ms = timeout_ms

    def __enter__(self):
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        self.handle = ctypes.c_void_p(kernel32.CreateMutexW(None, False, RENAME_MUTEX))
        result = kernel32.WaitForSingleObject(self.handle, self.timeout_ms)
        if result not in (0, 0x80):   # WAIT_OBJECT_0 / WAIT_ABANDONED
            kernel32.CloseHandle(self.handle)
            raise TimeoutError("другой наблюдатель слишком долго держит блокировку")
        return self

    def __exit__(self, *exc):
        import ctypes

        ctypes.windll.kernel32.ReleaseMutex(self.handle)
        ctypes.windll.kernel32.CloseHandle(self.handle)
        return False


def run_guarded(main, busy: bool = False) -> int:
    """Запускает main() так, чтобы любая ошибка не закрывала окно молча.
    busy=True — скрипт массово правит Waifu: наблюдатели tools\\watch на это время замирают."""
    pause = "--no-pause" not in sys.argv
    code = 0
    try:
        if busy:
            with waifu_busy():
                code = int(main() or 0)
        else:
            code = int(main() or 0)
    except SystemExit as exc:
        if exc.code is None:
            code = 0
        elif isinstance(exc.code, int):
            code = exc.code
            if code:
                report_error(exc, with_traceback=False)
        else:
            report_error(RuntimeError(str(exc.code)), with_traceback=False)
            code = 1
    except Exception as exc:
        report_error(exc)
        code = 1
    if pause:
        pause_console()
    return code
