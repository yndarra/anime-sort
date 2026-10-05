r"""Задачи Пульта: инструменты tools\*.py подпроцессом, их вывод и вопросы.

Инструмент запускается консольным python из venv с env ANIME_SORT_GUI=1 и ANIME_SORT_NO_PAUSE=1: tools\console.py
тогда шлёт вопросы служебной строкой «\x1eASK\t<вопрос>\t<варианты>» (или «\x1eWAIT\t<текст>»). Страница Пульта
показывает их кнопками и присылает ответ (TaskManager.answer → stdin). Цвета вывода (ANSI) превращаются в пары
(кусок текста, код цвета) — страница рисует их классами. Задачи, массово меняющие Waifu (waifu=True), идут по
одной: следующая ждёт в очереди.

Окно здесь ни при чём: TaskManager работает и без интерфейса (так его и проверяют тесты).
"""
from __future__ import annotations

import itertools
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
GUI_MARK = "\x1e"
ANSI_RE = re.compile(r"\x1b\[([0-9;]*)m")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
WORDS = {"д": "Да", "н": "Нет", "р": "Повторить", "п": "Пропустить", "в": "Выйти", "с": "Сохранить"}
KEEP_LINES = 5000   # строк вывода на задачу (старые отбрасываются)


def python_exe() -> str:
    """Консольный python того же venv (Пульт сам идёт под pythonw)."""
    exe = Path(sys.executable)
    console = exe.with_name("python.exe")
    return str(console if console.exists() else exe)


def option_labels(prompt: str, options: str) -> list[tuple[str, str]]:
    """Варианты ответа → [(буква, надпись)]. «р — повторить, п — пропустить шаг» даёт надписи из самого вопроса."""
    labels = []
    for letter in options:
        match = re.search(rf"(?:^|[\s,(]){re.escape(letter)}\s*[—-]\s*([^,;)\]]+)", prompt)
        text = match.group(1).strip() if match else WORDS.get(letter, letter.upper())
        labels.append((letter, text[:1].upper() + text[1:]))
    return labels


def split_ansi(line: str) -> list[tuple[str, str | None]]:
    """Строка с ANSI-цветами → [(кусок текста, код цвета или None)]."""
    parts, tag, position = [], None, 0
    for match in ANSI_RE.finditer(line):
        if match.start() > position:
            parts.append((line[position:match.start()], tag))
        codes = [code for code in match.group(1).split(";") if code]
        tag = None if not codes or codes[-1] == "0" else codes[-1]
        position = match.end()
    if position < len(line):
        parts.append((line[position:], tag))
    return parts


class Task:
    def __init__(self, number: int, title: str, command: list[str], waifu: bool, on_done=None):
        self.id = number
        self.title = title
        self.command = command
        self.waifu = waifu
        self.on_done = on_done
        self.status = "queued"          # queued / running / waiting / ok / failed / stopped
        self.started = time.strftime("%H:%M:%S")
        self.lines: list[list[tuple[str, str | None]]] = []
        self.dropped = 0                # сколько первых строк уже отброшено (KEEP_LINES)
        self.question: dict | None = None
        self.code: int | None = None
        self.process: subprocess.Popen | None = None
        self.lock = threading.Lock()

    def add(self, parts) -> None:
        with self.lock:
            self.lines.append(parts)
            if len(self.lines) > KEEP_LINES:
                extra = len(self.lines) - KEEP_LINES
                del self.lines[:extra]
                self.dropped += extra

    def start(self) -> None:
        env = dict(os.environ, ANIME_SORT_GUI="1", ANIME_SORT_NO_PAUSE="1", PYTHONIOENCODING="utf-8",
                   PYTHONUNBUFFERED="1")
        try:
            self.process = subprocess.Popen(self.command, cwd=str(PROJECT), env=env, stdin=subprocess.PIPE,
                                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
        except OSError as exc:
            self.status = "failed"
            self.add([(f"Не запустилось: {exc}", "31")])
            return
        self.status = "running"
        threading.Thread(target=self.read, daemon=True).start()

    def read(self) -> None:
        assert self.process and self.process.stdout
        for raw in iter(self.process.stdout.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if line.startswith(GUI_MARK):
                kind, _, rest = line[1:].partition("\t")
                prompt, _, options = rest.partition("\t")
                options = options if kind == "ASK" else ""
                self.question = {"prompt": prompt, "options": [{"key": k, "label": label}
                                                               for k, label in option_labels(prompt, options)]}
                self.status = "waiting"
                self.add([(prompt, "33")])
            else:
                self.add(split_ansi(line))
        code = self.process.wait()
        self.code = code
        self.question = None
        if self.status != "stopped":
            self.status = "ok" if code == 0 else "failed"
        if self.on_done:
            try:
                self.on_done(self)
            except Exception as exc:  # обработчик не должен ронять поток чтения
                self.add([(f"обработчик завершения: {exc}", "31")])

    def answer(self, text: str) -> None:
        self.question = None
        self.status = "running"
        self.add([(f"→ {text or 'Enter'}", "90")])
        if self.process and self.process.stdin and self.process.poll() is None:
            try:
                self.process.stdin.write((text + "\n").encode("utf-8"))
                self.process.stdin.flush()
            except OSError:
                pass

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.status = "stopped"
            subprocess.run(["taskkill", "/PID", str(self.process.pid), "/T", "/F"], capture_output=True,
                           creationflags=NO_WINDOW)

    def summary(self) -> dict:
        return {"id": self.id, "title": self.title, "status": self.status, "started": self.started,
                "question": self.question, "total": self.dropped + len(self.lines)}

    def lines_since(self, cursor: int) -> tuple[int, list]:
        """Строки после курсора (номер строки от начала задачи) → (новый курсор, строки)."""
        with self.lock:
            start = max(0, cursor - self.dropped)
            return self.dropped + len(self.lines), self.lines[start:]


class TaskManager:
    def __init__(self):
        self.tasks: list[Task] = []
        self.numbers = itertools.count(1)
        self.lock = threading.Lock()

    def run(self, title: str, script: str | Path, *args: str, waifu: bool = False, on_done=None) -> Task:
        """Запустить <проект>\\<script> с аргументами (Waifu-задачи — по одной, остальные — сразу)."""
        command = [python_exe(), str(PROJECT / script), *map(str, args)]
        task = Task(next(self.numbers), title, command, waifu, on_done)
        with self.lock:
            self.tasks.append(task)
        self.tick()
        return task

    def busy_with_waifu(self) -> bool:
        return any(t.waifu and t.status in ("running", "waiting") for t in self.tasks)

    def tick(self) -> None:
        """Запустить задачи из очереди, если можно (зовётся при каждом опросе страницы)."""
        with self.lock:
            for task in self.tasks:
                if task.status != "queued":
                    continue
                if task.waifu and self.busy_with_waifu():
                    if not task.lines:
                        task.add([("Ждёт: другая задача сейчас меняет Waifu — начнётся, когда она закончится.", "33")])
                    continue
                task.start()

    def get(self, number: int) -> Task | None:
        return next((task for task in self.tasks if task.id == number), None)

    def poll(self, cursors: dict) -> dict:
        """{id задачи: курсор} → сводка всех задач + новые строки тех, что страница показывает."""
        self.tick()
        lines = {}
        for key, cursor in (cursors or {}).items():
            task = self.get(int(key))
            if task is not None:
                new_cursor, new_lines = task.lines_since(int(cursor))
                lines[str(task.id)] = {"cursor": new_cursor, "lines": new_lines}
        return {"tasks": [task.summary() for task in reversed(self.tasks)], "lines": lines}
