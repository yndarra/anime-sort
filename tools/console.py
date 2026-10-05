r"""Консоль для пользователя: цветные строки, понятные ошибки и окно, которое не закрывается само.

Все команды запускаются .bat-файлами двойным щелчком, поэтому:
    - любая ошибка выводится красным с подсказкой «что сделать» и окно ждёт Enter;
    - в конце работы окно тоже ждёт Enter — результат можно прочитать.
"""
from __future__ import annotations

import ctypes
import os
import sys
import time
import traceback

RED, GREEN, YELLOW, CYAN, GREY, WHITE, RESET = "\x1b[91m", "\x1b[92m", "\x1b[93m", "\x1b[96m", "\x1b[90m", "\x1b[97m", "\x1b[0m"


class UserError(Exception):
    """Ошибка, которую может исправить пользователь: текст «что случилось» и подсказка «что сделать»."""

    def __init__(self, what: str, fix: str = ""):
        super().__init__(what)
        self.what = what
        self.fix = fix


def enable_colors() -> None:
    """ANSI-цвета в обычной консоли Windows + вывод в UTF-8 (кириллица, ✓)."""
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except (AttributeError, OSError):
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def say(text: str = "", color: str = "") -> None:
    print(f"{color}{text}{RESET}" if color else text, flush=True)


def step(text: str) -> None:
    say(f"[{time.strftime('%H:%M:%S')}] {text}", WHITE)


def info(text: str) -> None:
    say(f"[{time.strftime('%H:%M:%S')}] {text}", GREY)


def ok(text: str) -> None:
    say(f"[{time.strftime('%H:%M:%S')}] {text}", GREEN)


def warn(text: str) -> None:
    say(f"[{time.strftime('%H:%M:%S')}] {text}", YELLOW)


def error(what: str, fix: str = "") -> None:
    say(f"[{time.strftime('%H:%M:%S')}] ОШИБКА: {what}", RED)
    if fix:
        say(f"           Что сделать: {fix}", YELLOW)


def title(text: str) -> None:
    say()
    say(text, CYAN)
    say("─" * len(text), CYAN)


def wait(prompt: str = "Нажмите Enter, чтобы продолжить…") -> str:
    try:
        return input(f"{YELLOW}{prompt}{RESET} ")
    except EOFError:
        return ""


def ask(prompt: str, options: str = "дн") -> str:
    """Вопрос с вариантами по первой букве (д/н, п/с/в …). Повторяет, пока не ответят правильно."""
    # Латинские y/n — тоже «да/нет» (английская раскладка, ввод через pipe).
    aliases = {"y": "д", "n": "н"}
    while True:
        answer = wait(f"{prompt} [{'/'.join(options)}]:").strip().lstrip("﻿").casefold()[:1]
        answer = aliases.get(answer, answer)
        if answer in options:
            return answer
        warn(f"Ответьте одной буквой: {', '.join(options)}")


def guarded(main) -> int:
    """Запуск команды: ошибки — понятным текстом, окно ждёт Enter в любом исходе."""
    enable_colors()
    code = 1
    try:
        code = main() or 0
    except UserError as exc:
        error(exc.what, exc.fix)
    except KeyboardInterrupt:
        warn("Остановлено (Ctrl+C). Всё сделанное сохранено — можно запустить снова.")
    except Exception:
        error("непредвиденный сбой программы (подробности ниже)",
              "пришлите текст ниже разработчику; запуск ещё раз безопасен — сделанное не теряется")
        say(traceback.format_exc().rstrip(), RED)
    # Шаг внутри finish.bat (ANIME_SORT_NO_PAUSE=1) — окно общее, ждать Enter не нужно.
    if not os.environ.get("ANIME_SORT_NO_PAUSE"):
        say()
        wait("Готово. Нажмите Enter, чтобы закрыть окно…")
    return code
