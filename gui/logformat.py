r"""Строки лога и таблицы статуса для окна набора.

Всё, что зависит от конфига, приходит из logs\layout.json набора (его пишет engine\dataset.py):
этапы по порядку (id, метка в логе, название в таблице) и ширины столбцов лога и таблицы.
"""
from __future__ import annotations

import json
import textwrap
import time
from pathlib import Path

SEP = "  "
DASH = "—"
RUN_STARTED_PREFIX = "Начало работы"
MSG_STARTED = "этап начат"
MSG_RESUMED = "этап продолжен"
MSG_UNCHANGED = "этап без изменений"
MSG_FINISHED = "этап завершён"
MSG_DEFERRED = "этап отложен"
MSG_SKIPPED = "этап пропущен"
MSG_BACK = "возврат к этапу"
MSG_PAUSED = "пауза"
MSG_CONTINUED = "продолжение"
# Служебные события: несут итоги этапа для таблицы, но в лог не выводятся.
HIDDEN_MESSAGES = {MSG_RESUMED, MSG_UNCHANGED}
# Старые имена этапов в логах, записанных до переименования.
STAGE_ALIASES = {"AI-F51": "AI-F5", "AI-GPT": "AI-G6"}

LOG_COLUMNS = ("time", "stage", "status", "file", "confidence", "title", "character")
LOG_DEFAULTS = {"time": 21, "stage": 7, "status": 12, "file": 33, "confidence": 4, "title": 50, "character": 50}
TABLE_COLUMNS = ("stage", "determined", "nd", "error", "expected", "remained")
TABLE_DEFAULTS = {"stage": 12, "determined": 10, "nd": 5, "error": 6, "expected": 8, "remained": 8}
TABLE_HEADERS = ("Stage", "Determined", "N/D", "Error", "Expected", "Remained")
TABLE_ALIGNS = ("<", ">", ">", ">", ">", ">")
# Столбец этапа может «залезть» в разделитель (оставив хотя бы 1 пробел) — так [SUMMARY] не переносится.
SOFT_COLUMNS = (1,)

# Цвета: определил — зелёный, не определил — серый, любая ошибка — красный,
# запуск и начало этапа — жёлтый, конец этапа — белый, metadata — светло-серый.
COLOR_DETERMINED = "32"
COLOR_NOT_DETERMINED = "90"
COLOR_WHITE = "97"
COLOR_ERROR = "31"
COLOR_ACCENT = "33"
COLOR_PLAIN = "37"
FINAL_ATTEMPT = 3


def read_json(path: Path | None) -> dict:
    if not path or not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def wrap_cell(value, width: int) -> list[str]:
    text = " ".join(str(value if value is not None else "").split())
    if not text:
        return [""]
    return textwrap.wrap(text, width, break_long_words=True, break_on_hyphens=False) or [""]


def render_row(cells, widths, aligns=None, soft=SOFT_COLUMNS) -> list[str]:
    """Одна запись -> одна или несколько строк экрана (перенос внутри столбцов фиксированной ширины)."""
    aligns = aligns or ("<",) * len(widths)
    columns = []
    for index, (cell, width) in enumerate(zip(cells, widths)):
        text = " ".join(str(cell if cell is not None else "").split())
        if index in soft and width < len(text) <= width + len(SEP) - 1:
            columns.append([text])
        else:
            columns.append(wrap_cell(cell, width))
    starts, position = [], 0
    for width in widths:
        starts.append(position)
        position += width + len(SEP)
    lines = []
    for index in range(max(len(column) for column in columns)):
        line = ""
        for column, width, align, begin in zip(columns, widths, aligns, starts):
            text = column[index] if index < len(column) else ""
            if not text:
                continue
            if align == ">":
                text = text.rjust(width)
            if len(line) < begin:
                line = line.ljust(begin)
            elif line:
                line += " "
            line += text
        lines.append(line.rstrip())
    return lines


def is_final_error(event: dict, status: str) -> bool:
    if "final" in event:
        return bool(event.get("final"))
    try:
        return int(status.split("-", 1)[1]) >= FINAL_ATTEMPT
    except (IndexError, ValueError):
        return False


class Formatter:
    def __init__(self, layout: dict):
        stages = layout.get("stages") or []
        self.stages = [entry["id"] for entry in stages]
        self.labels = {entry["id"]: entry.get("label") or entry["id"] for entry in stages}
        self.tables = {entry["id"]: entry.get("table") or entry["id"] for entry in stages}
        self.providers = {entry["id"]: list(entry.get("providers") or []) for entry in stages}
        log = {**LOG_DEFAULTS, **(layout.get("log_columns") or {})}
        table = {**TABLE_DEFAULTS, **(layout.get("table_columns") or {})}
        self.widths = tuple(int(log[name]) for name in LOG_COLUMNS)
        self.table_widths = tuple(int(table[name]) for name in TABLE_COLUMNS)
        self.table_width = sum(self.table_widths) + len(SEP) * (len(self.table_widths) - 1)
        self.log_width = sum(self.widths) + len(SEP) * (len(self.widths) - 1)
        w = self.widths
        # Строка metadata у JSON: текст занимает всё от столбца файла до конца.
        self.span_widths = w[:3] + (sum(w[3:]) + len(SEP) * (len(w) - 4),)
        # Итоги начала/конца этапа: от столбца файла, могут занять ещё уверенность и тайтл.
        self.summary_widths = w[:3] + (w[3] + w[4] + w[5] + 2 * len(SEP),)
        # Строки без файла (API, смена модели, ошибки сервиса, возврат, пропуск …): суть — в столбцах «файл» и
        # «уверенность» (объединены), подробность — в «тайтл» и «персонаж» (объединены). У строк файлов столбцы
        # остаются отдельными.
        self.info_widths = w[:3] + (w[3] + w[4] + len(SEP), w[5] + w[6] + len(SEP))
        # Пустые поля заполняются прочерками на всю ширину столбца: имя файла — 33 «—», уверенность — 4 «—».
        # "empty_cells" в config.json: "blank" — пустые поля так и остаются пустыми; "dash" — прочерки «—».
        self.dash = "" if layout.get("empty_cells") == "blank" else DASH
        self.no_file = self.dash * w[3]
        self.no_confidence = self.dash * w[4]
        self.no_title = self.dash * w[5]
        self.no_character = self.dash * w[6]

    # ---------- лог

    def stage_id(self, stage) -> str:
        stage = str(stage or "")
        return STAGE_ALIASES.get(stage, stage)

    def stage_label(self, stage) -> str:
        stage = self.stage_id(stage)
        if stage == "PIPELINE":
            return "[LINE]"
        return f"[{self.labels.get(stage, stage)}]"

    def head(self, event: dict, stage=None) -> tuple[str, str]:
        return f"[{event.get('time', '')}]", self.stage_label(event.get("stage") if stage is None else stage)

    def rows(self, cells, code, widths=None, aligns=None, soft=SOFT_COLUMNS):
        # Столбец состояния лога (третий: ОПРЕДЕЛИЛ, НЕ ОПРЕДЕЛИЛ, ОШИБКА, API …) — заглавными. Таблица этапов
        # рисуется этой же функцией со своими ширинами — её не трогаем.
        if widths is not self.table_widths and len(cells) > 2:
            cells = (*cells[:2], str(cells[2] or "").upper(), *cells[3:])
        return [(line, code) for line in render_row(cells, widths or self.widths, aligns, soft)]

    def info_rows(self, event: dict, status: str, short, detail, code):
        """Строка без файла: суть — с места имени файла, подробность — с места тайтла (см. info_widths)."""
        if not detail:
            # Подробности нет — суть занимает всю ширину от столбца файла до конца, без раннего переноса.
            return self.rows((*self.head(event), status, short), code, self.span_widths)
        return self.rows((*self.head(event), status, short, detail), code, self.info_widths)

    def error_rows(self, event: dict, status: str, title: str, detail):
        if not event.get("file"):
            return self.info_rows(event, status, title, detail, COLOR_ERROR)
        return self.rows((*self.head(event), status, event.get("file") or self.no_file, self.no_confidence, title, detail or self.dash), COLOR_ERROR)

    def confidence_label(self, event: dict) -> str:
        # Нет уверенности (соседи; в старых логах у соседей писалось 0.0) — прочерки на всю ширину.
        if event.get("confidence") in (None, "", DASH) or event.get("model") == "Neighbors":
            return self.no_confidence
        try:
            return f"{float(event.get('confidence') or 0.0):.2f}"
        except (TypeError, ValueError):
            return self.no_confidence

    @staticmethod
    def stage_summary(event: dict, finished: bool) -> str:
        if finished:
            return f"determined={event.get('determined', 0)}; not_determined={event.get('not_determined', 0)}"
        expected = f"expected={event.get('expected', 0)}"
        if event.get("range"):
            expected += f" ({event['range']})"
        elif event.get("range_low") is not None and event.get("range_high") is not None:
            expected += f" ({float(event['range_low']):.2f} - {float(event['range_high']):.2f})"
        parts = [expected]
        if event.get("k") is not None:
            parts.append(f"k={event.get('k')}")
        return "; ".join(parts)

    def item_rows(self, event: dict):
        message = str(event.get("message", ""))
        if message.startswith("не определил"):
            status, color = "не определил", COLOR_NOT_DETERMINED
        else:
            status, color = "определил", COLOR_DETERMINED
        title = event.get("title") if event.get("title") not in (None, "", DASH) else self.no_title
        character = event.get("characters") if event.get("characters") not in (None, "", DASH) else self.no_character
        result = self.rows((*self.head(event), status, event.get("file") or self.no_file, self.confidence_label(event), title, character), color)
        # metadata (текст пина) — отдельной строкой под записью, если файл его содержит и модель его видела.
        if int(event.get("meta", 0) or 0) > 0 and event.get("show_metadata", self.is_json(event)):
            parts = [part.strip() for part in str(event.get("metadata", "")).replace("  ", "\n").splitlines() if part.strip()]
            metadata = "; ".join(parts).removeprefix("metadata: ")
            if metadata:
                result += self.rows(("", "", "", metadata), COLOR_PLAIN, self.span_widths)
        return result

    @staticmethod
    def is_json(event: dict) -> bool:
        # Старые логи: metadata показывалась только у этапа JSON; новые события несут show_metadata.
        return event.get("stage") == "JSON"

    def format_event(self, event: dict):
        """Событие -> строки [(текст, ANSI-код цвета)], включая пустые строки-разделители."""
        message = str(event.get("message", ""))
        stage = event.get("stage")
        if message in HIDDEN_MESSAGES:
            return []
        if stage == "PIPELINE" and message.startswith(RUN_STARTED_PREFIX):
            return [("", None)] + self.rows((*self.head(event), "запуск"), COLOR_ACCENT)
        if stage == "PIPELINE" and message in (MSG_PAUSED, MSG_CONTINUED):
            return self.rows((*self.head(event), message), COLOR_ACCENT)
        if message in (MSG_STARTED, MSG_FINISHED):
            finished = message == MSG_FINISHED
            return ([] if finished else [("", None)]) + self.rows(
                (*self.head(event), "конец этапа" if finished else "начало этапа", self.stage_summary(event, finished)),
                COLOR_WHITE if finished else COLOR_ACCENT, self.summary_widths)
        if event.get("retry") or message.startswith("повтор"):
            reason = event.get("reason") or message.removeprefix("повтор:").strip().removesuffix(", повтор") or "повтор"
            return self.error_rows(event, "ошибка", f"{reason} - повтор", event.get("error_text"))
        if message.startswith("ошибка") and event.get("file"):
            status = message.split()[0]
            final = is_final_error(event, status)
            return self.error_rows(event, status, "ошибка - не определено" if final else "ошибка - повтор", event.get("error_text"))
        if event.get("broken"):
            reason = message.split(":", 1)[0]
            return self.error_rows(event, "битый файл", f"{reason} - в «Other»", event.get("error_text"))
        if event.get("file"):
            return self.item_rows(event)
        if message.startswith("ОСТАНОВКА"):
            text = message.removeprefix("ОСТАНОВКА:").strip()
            if "\x00" in text or "\x1b" in text:
                text = text.split(" — ")[0] + " — без сообщения об ошибке (процесс мог быть остановлен извне)"
            return self.error_rows(event, "остановка", text, event.get("error_text"))
        if message == "API":
            return self.info_rows(event, "API", event.get("api", ""), "", COLOR_PLAIN)
        if message == "переключение API":
            return self.info_rows(event, "API", f"{event.get('previous')} → {event.get('api')}", event.get("reason"),
                                  COLOR_ACCENT)
        if message == MSG_BACK:
            target = self.labels.get(str(event.get("target")), event.get("target"))
            return self.info_rows(event, "возврат", f"назад к этапу [{target}]",
                                  "его файлы, которых этот этап не касался", COLOR_ACCENT)
        if message in (MSG_DEFERRED, MSG_SKIPPED):
            status = "отложен" if message == MSG_DEFERRED else "пропущен"
            if message == MSG_DEFERRED:
                text = "этап застрял — дальше следующие, потом возврат"
            elif event.get("manual"):
                text = "этап пропущен вручную"
            else:
                text = "этап так и не заработал"
            return self.info_rows(event, status, text, event.get("reason"),
                                  COLOR_ERROR if message == MSG_SKIPPED and not event.get("manual") else COLOR_ACCENT)
        if message == "запасная модель":
            return self.info_rows(event, "модель", f"{event.get('previous')} → {event.get('model')}", event.get("reason"),
                                  COLOR_ACCENT)
        if message == "API недоступен":
            return self.error_rows(event, "API", f"{event.get('api')} недоступен", event.get("reason"))
        if message.startswith("конвейер упал"):
            return self.error_rows(event, "ошибка", message, "засчитано этому файлу")
        if message.startswith("не завершено"):
            return self.info_rows(event, "не завершено", message.removeprefix("не завершено").strip(" :"), "", COLOR_ACCENT)
        if message.startswith("ошибка") or event.get("error"):
            return self.error_rows(event, "ошибка", message.removeprefix("ошибка").strip(" :") or "ошибка", event.get("error_text"))
        return self.info_rows(event, "", message, "", COLOR_PLAIN)

    # ---------- таблица

    @staticmethod
    def empty_record() -> dict:
        return {"expected": 0, "base_determined": 0, "base_not_determined": 0, "base_errors": 0,
                "determined_files": set(), "unresolved_files": set(), "error_files": set(),
                "determined": 0, "not_determined": 0, "errors": 0, "remained": 0, "started": False, "completed": False}

    def from_events(self, events: list[dict]) -> dict:
        """Таблица по событиям ТЕКУЩЕГО запуска (после последнего «Начало работы»).

        События начала/продолжения/завершения этапа несут полные итоги (с учётом прошлых запусков),
        строки файлов досчитываются поверх них.
        """
        records = {stage: self.empty_record() for stage in self.stages}
        # Этап перешёл на запасную модель — в таблице новое название (до следующего запуска).
        self.renamed: dict[str, str] = {}
        for event in events:
            stage = self.stage_id(event.get("stage"))
            message = str(event.get("message", ""))
            if stage == "PIPELINE" and message.startswith(RUN_STARTED_PREFIX):
                records = {name: self.empty_record() for name in self.stages}
                self.renamed = {}
                continue
            if stage not in records:
                continue
            record = records[stage]
            if message == MSG_DEFERRED:
                # Отложенный этап не держит подсветку «текущего» — конвейер ушёл к следующим.
                record["deferred"] = True
                continue
            if message == MSG_SKIPPED:
                record["deferred"] = False
                record["completed"] = True
                record["skipped"] = True    # к пропущенному этапу можно вернуться кнопкой «←»
                continue
            if message == MSG_BACK:
                continue
            if message == "запасная модель" and event.get("table"):
                self.renamed[stage] = str(event["table"])
                continue
            if message in ("API", "переключение API"):
                record["api"] = event.get("api")
                continue
            if message in (MSG_STARTED, MSG_RESUMED, MSG_FINISHED, MSG_UNCHANGED):
                done = message in (MSG_FINISHED, MSG_UNCHANGED)
                api = record.get("api")
                record.update(self.empty_record())
                if api and not done:
                    record["api"] = api
                record["expected"] = int(event.get("expected", 0) or 0)
                record["base_determined"] = int(event.get("determined", 0) or 0)
                record["base_not_determined"] = int(event.get("not_determined", 0) or 0)
                record["base_errors"] = int(event.get("errors", 0) or 0) if done else 0
                record["started"] = True
                record["completed"] = done
            elif event.get("file") and record["started"] and not record["completed"]:
                filename = str(event["file"])
                if message.startswith("определил"):
                    record["determined_files"].add(filename)
                    record["unresolved_files"].discard(filename)
                elif message.startswith("не определил"):
                    record["unresolved_files"].add(filename)
                elif message.startswith("ошибка") and is_final_error(event, message.split()[0]):
                    record["error_files"].add(filename)
        for record in records.values():
            record["determined"] = record["base_determined"] + len(record["determined_files"])
            record["not_determined"] = record["base_not_determined"] + len(record["unresolved_files"] - record["determined_files"])
            record["errors"] = record["base_errors"] + len(record["error_files"] - record["determined_files"] - record["unresolved_files"])
            record["expected"] = max(record["expected"], record["determined"] + record["not_determined"] + record["errors"])
            record["remained"] = max(0, record["expected"] - record["determined"] - record["not_determined"] - record["errors"])
        return records

    def current_stage(self, records: dict) -> str | None:
        current = self.current_index(records)
        return self.stages[current] if current < len(self.stages) else None

    def can_go_back(self, records: dict) -> bool:
        """Кнопка «←»: есть предыдущий этап и он пройден не полностью (не начат, отложен, пропущен или идёт)."""
        current = self.current_index(records)
        if current <= 0 or current >= len(self.stages):
            return False
        previous = records[self.stages[current - 1]]
        return not previous.get("completed") or bool(previous.get("skipped"))

    def current_index(self, records: dict) -> int:
        for index, stage in enumerate(self.stages):
            if records[stage].get("started") and not records[stage].get("completed") and not records[stage].get("deferred"):
                return index
        for index, stage in enumerate(self.stages):
            if not records[stage].get("completed") and not records[stage].get("deferred"):
                return index
        # Остались только отложенные — конвейер вернулся к ним (или вот-вот вернётся).
        for index, stage in enumerate(self.stages):
            if not records[stage].get("completed"):
                return index
        return len(self.stages)

    def table_lines(self, records: dict, total: int):
        current = self.current_index(records)
        lines = [("PINTEREST SORTER — LIVE STATUS", None), ("=" * self.table_width, None)]
        lines += self.rows(TABLE_HEADERS, None, self.table_widths, TABLE_ALIGNS, ())
        lines.append(("-" * self.table_width, None))
        for index, stage in enumerate(self.stages):
            record = records[stage]
            if not record.get("started"):
                values = ("~",) * 5
            else:
                values = tuple(record.get(name, 0) for name in ("determined", "not_determined", "errors", "expected", "remained"))
            lines += self.rows((getattr(self, "renamed", {}).get(stage) or self.tables[stage], *values), COLOR_ACCENT if index == current else None,
                               self.table_widths, TABLE_ALIGNS, ())
        lines.append(("-" * self.table_width, None))
        lines.append((f"Files: {total}  Updated: {time.strftime('%H:%M:%S')}", None))
        if current < len(self.stages) and records[self.stages[current]].get("api"):
            stage = self.stages[current]
            lines.append((f"API {self.labels.get(stage, stage)}: {records[stage]['api']}", COLOR_ACCENT))
        return lines
