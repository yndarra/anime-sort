r"""Статистика для таблицы главного окна anime-sort: что делают пачки, папки, модели и API за сегодня.

Источники (только чтение):
    results\test-dataN\logs\pipeline_events.jsonl   события наборов — читаются по кусочкам (только новые строки)
    results\test-dataN\logs\pids.json               какой набор сейчас в работе (жив процесс engine\dataset.py)
    results\test-dataN\work\chunk_*\_state.json     сколько файлов набора уже определено
    results\test-dataN\out\status_snapshot.json     набор завершён; DONE.txt — влит в Waifu (tools\add\add.py)
Сбор идёт в фоновом потоке главного окна (collect()), отрисовка — в окне; строки не длиннее ширины таблицы.
"""
from __future__ import annotations

import collections
import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

WAKE_RE = re.compile(r"проверю снова в (\d{2}):(\d{2})")
# Цвета строк — коды, как у ANSI в логах (gui\window.py COLORS).
WHITE, GREY, GREEN, YELLOW, RED, CYAN = "97", "90", "32", "33", "31", "36"


def short(text: str, width: int) -> str:
    text = str(text)
    return text if len(text) <= width else text[:width - 1] + "…"


def number(value: int) -> str:
    """1 240 — с пробелом между тысячами."""
    return f"{value:,}".replace(",", " ")


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


class EventFile:
    """Один pipeline_events.jsonl: смещение прочитанного и текущий API каждого этапа этого набора."""

    def __init__(self):
        self.offset = 0
        self.remainder = b""
        self.api: dict[str, str] = {}             # этап -> текущий API (из событий «API» / «переключение API»)
        self.last_file_time: dict[str, datetime] = {}


class Stats:
    def __init__(self, dispatcher, width: int = 58):
        self.dispatcher = dispatcher
        self.width = width
        self.files: dict[Path, EventFile] = {}
        self.day = ""
        self.reset_day()

    def reset_day(self) -> None:
        """Счётчики «за сегодня» — с полуночи."""
        self.day = time.strftime("%Y-%m-%d")
        self.models: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
        self.gaps: dict[str, collections.deque] = collections.defaultdict(lambda: collections.deque(maxlen=300))
        self.apis: dict[str, dict] = collections.defaultdict(lambda: {"files": 0, "errors": 0, "sleep_until": None,
                                                                       "sleep_kind": "", "last_ok": None})
        self.recent: collections.deque = collections.deque()       # (время, определил) за последний час
        self.broken = 0
        self.determined_today = 0
        self.last_error: tuple[str, str, str, str] | None = None   # (время, набор, этап, текст)
        for state in self.files.values():
            state.last_file_time.clear()

    # ---------- события ----------

    def read_events(self, results: Path) -> None:
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        for path in results.glob("test-data*/logs/pipeline_events.jsonl"):
            try:
                stat = path.stat()
            except OSError:
                continue
            state = self.files.get(path)
            if state is None:
                state = self.files[path] = EventFile()
                if stat.st_mtime < today:
                    state.offset = stat.st_size      # сегодня не менялся — читать только то, что появится
                    continue
            if stat.st_size < state.offset:
                state.offset, state.remainder = 0, b""
            if stat.st_size == state.offset:
                continue
            try:
                with path.open("rb") as handle:
                    handle.seek(state.offset)
                    chunk = handle.read(stat.st_size - state.offset)
            except OSError:
                continue
            state.offset = stat.st_size
            *lines, state.remainder = (state.remainder + chunk).split(b"\n")
            dataset = path.parent.parent.name.removeprefix("test-")
            for raw in lines:
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                self.handle(event, state, dataset)

    def handle(self, event: dict, state: EventFile, dataset: str) -> None:
        stamp = str(event.get("time", ""))
        stage = str(event.get("stage", ""))
        message = str(event.get("message", ""))
        api = event.get("api")
        # API этапа запоминается и из вчерашних событий — иначе сегодняшние файлы не к чему отнести.
        if message in ("API", "переключение API") and api:
            state.api[stage] = api
            if stamp.startswith(self.day):
                self.apis[api]["last_ok"] = stamp
            return
        if not stamp.startswith(self.day):
            return
        if message == "API недоступен" and api:
            reason = str(event.get("reason", ""))
            wake = WAKE_RE.search(reason)
            entry = self.apis[api]
            entry["sleep_kind"] = "нет баланса" if ("баланс" in reason or "402" in reason) else "спит"
            if wake:
                moment = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
                until = moment.replace(hour=int(wake.group(1)), minute=int(wake.group(2)), second=0)
                if until < moment:
                    until += timedelta(days=1)
                entry["sleep_until"] = until
            return
        if event.get("broken"):
            self.broken += 1
            return
        if not event.get("file"):
            return
        if event.get("error") or event.get("retry"):
            if event.get("model") and stage.startswith(("AI-", "JSON")):
                self.models[str(event["model"])]["errors"] += 1
            text = str(event.get("error_text", ""))
            prefix = text.split(":", 1)[0]
            if "/" in prefix:
                self.apis[prefix]["errors"] += 1
            self.last_error = (stamp[11:16], dataset, stage, text.split(": ", 1)[-1])
            return
        determined = message.startswith("определил")
        if not (determined or message.startswith("не определил")):
            return
        moment = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
        self.recent.append((moment, determined))
        if determined:
            self.determined_today += 1
        if not stage.startswith(("AI-", "JSON")):
            return
        model = str(event.get("model") or stage)
        self.models[model]["checked"] += 1
        self.models[model]["determined"] += determined
        previous = state.last_file_time.get(stage)
        if previous is not None and 0 < (moment - previous).total_seconds() < 600:
            self.gaps[model].append((moment - previous).total_seconds())
        state.last_file_time[stage] = moment
        current = state.api.get(stage)
        if current:
            self.apis[current]["files"] += 1
            self.apis[current]["last_ok"] = stamp

    # ---------- наборы и папки ----------

    @staticmethod
    def dataset_status(root: Path) -> str:
        """done — влит в Waifu; completed — завершён, не влит; active — сейчас в работе; started — прерван; new — не начат."""
        if not root.exists():
            return "new"
        pids = read_json(root / "logs" / "pids.json")
        from engine import winproc

        if winproc.pid_alive(pids.get("dataset")):
            return "active"
        if (root / "DONE.txt").exists():
            return "done"
        if read_json(root / "out" / "status_snapshot.json").get("status") == "completed" and not (root / "work").exists():
            return "completed"
        return "started"

    @staticmethod
    def progress(root: Path) -> tuple[int, int]:
        items: dict = {}
        for state in (root / "work").glob("chunk_*/_state.json"):
            items.update(read_json(state).get("items", {}))
        return sum(1 for item in items.values() if item.get("result")), len(items)

    @staticmethod
    def running_for(root: Path) -> str:
        """Сколько идёт текущий запуск набора (pids.json пишется при его старте): «42 мин», «1 ч 05 мин»."""
        try:
            minutes = int((time.time() - (root / "logs" / "pids.json").stat().st_mtime) // 60)
        except OSError:
            return "—"
        return f"{minutes // 60} ч {minutes % 60:02d} мин" if minutes >= 60 else f"{minutes} мин"

    @staticmethod
    def current_stage(root: Path) -> str:
        """Этап последнего события набора (по хвосту лога)."""
        path = root / "logs" / "pipeline_events.jsonl"
        try:
            with path.open("rb") as handle:
                handle.seek(max(0, path.stat().st_size - 4000))
                tail = handle.read().decode("utf-8", errors="replace").splitlines()
        except OSError:
            return "—"
        for line in reversed(tail):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            stage = str(event.get("stage", ""))
            if stage and stage not in ("PIPELINE", "GUI"):
                return stage
        return "—"

    # ---------- таблица ----------

    def collect(self) -> list[tuple[str, str | None]]:
        if time.strftime("%Y-%m-%d") != self.day:
            self.reset_day()
        settings = self.dispatcher.settings
        width = self.width
        lines: list[tuple[str, str | None]] = []
        add = lambda text="", code=None: lines.append((short(text, width), code))  # noqa: E731
        add(f"{'ВСЕ ПАЧКИ':<{width - 8}}{time.strftime('%H:%M:%S')}", WHITE)
        add("=" * width, GREY)
        if settings is None:
            add("config.json с ошибками — пачки не запущены", RED)
            return lines
        results = settings.results
        self.read_events(results)

        # Пачки: набор в работе, этап, сколько определено, сколько папок ещё впереди, сколько идёт набор.
        # Столбцы: 9 + 9 + 9 + 13 + 7 + 12 = 59 символов (вся ширина таблицы).
        add(f"{'Пачка':<9}{'Набор':<9}{'Этап':<9}{'Определено':>13}{'Папок':>7}{'Идёт':>12}", GREY)
        seen_folders = set()
        for spec in list(self.dispatcher.started):
            current, left = None, 0
            for folder in spec.folders:
                if folder in seen_folders:
                    continue
                seen_folders.add(folder)
                status = self.dataset_status(results / f"test-{folder}")
                if status == "active":
                    current = folder
                if status not in ("done", "completed"):
                    left += 1
            if current:
                root = results / f"test-{current}"
                resolved, total = self.progress(root)
                share = f"{resolved}/{total} {100 * resolved // total:>3}%" if total else "—"
                # Длинные имена (data90-other) не влезают в столбец — сокращаются многоточием, строка не ломается.
                add(f"{short(spec.file, 8):<9}{short(current, 8):<9}{short(self.current_stage(root), 8):<9}{share:>13}{left:>7}"
                    f"{self.running_for(root):>12}", GREEN)
            else:
                add(f"{short(spec.file, 8):<9}{'—':<9}{'готово' if not left else 'ждёт':<9}{'':>13}{left:>7}{'':>12}",
                    GREY if not left else YELLOW)
        add()

        # Модели за сегодня: 28 + 8 + 8 + 8 + 7 = 59.
        add(f"{'Модель (сегодня)':<28}{'файлов':>8}{'опред.':>8}{'с/файл':>8}{'сбоев':>7}", GREY)
        for model, counter in sorted(self.models.items(), key=lambda item: -item[1]["checked"]):
            gaps = sorted(self.gaps[model])
            median = f"{gaps[len(gaps) // 2]:.1f}" if gaps else "—"
            share = f"{100 * counter['determined'] // counter['checked']}%" if counter["checked"] else "—"
            add(f"{short(model, 27):<28}{number(counter['checked']):>8}{share:>8}{median:>8}{counter['errors']:>7}")
        if not self.models:
            add("  сегодня ещё ни одного ответа модели", GREY)
        add()

        # API: файлов и сбоев за сегодня, время последнего ответа, состояние сейчас: 16 + 7 + 7 + 7 + 2 + 20 = 59.
        add(f"{'API (сегодня)':<16}{'файлов':>7}{'сбоев':>7}{'ответ':>7}  {'сейчас':<20}", GREY)
        now = datetime.now()
        configured = {route.label for spec in self.dispatcher.started for stage in spec.stages for route in stage.apis}
        for api, entry in sorted(self.apis.items(), key=lambda item: (-item[1]["files"], item[0])):
            if configured and api not in configured:
                continue
            until = entry["sleep_until"]
            # Уснувший API до времени повторной проверки не вызывается — значит, спит, пока это время не настало.
            if until is not None and until > now:
                state, code = f"{entry['sleep_kind']} до {until.strftime('%H:%M')}", RED if entry["sleep_kind"] == "нет баланса" else YELLOW
            else:
                state, code = "работает", GREEN
            last = str(entry["last_ok"] or "")[11:16] or "—"
            add(f"{short(api, 15):<16}{number(entry['files']):>7}{entry['errors']:>7}{last:>7}  {state:<20}", code)
        if not self.apis:
            add("  сегодня API ещё не вызывались", GREY)
        if self.last_error:
            moment, dataset, stage, text = self.last_error
            add()
            add(f"Последний сбой: {moment} {dataset} {stage}", GREY)
            add(f"  {text}", GREY)
        return lines
