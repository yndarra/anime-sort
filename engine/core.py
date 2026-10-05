r"""Ядро одного набора: состояние файлов, лог событий, папки результатов.

Набор (test-dataN) устроен так:
    in\        копии исходных картинок (источник не трогается)
    work\      chunk_XXXX\ с картинками и _state.json — состояние каждого файла; _stages.json — какие этапы уже прошли
    out\       результаты: «N. <метка этапа>», «0. ALL», «<n+1>. Other», layout.json, status_snapshot.json
    logs\      pipeline_events.jsonl (события для окна), pipeline_stderr.log, layout.json для окна
    cache\     ответы моделей (повторный запуск не тратит запросы)

Состояние файла (item):
    checked_stages  этапы, которые файл прошёл; stage_results — результат этапа (None — не определил);
    stage_details   подробности этапа (уверенность, ответ модели); {"status": "skipped"} — пометка старого
                    конвейера «пропущен маршрутизацией», новым движком такая пометка считается «не проверялся»;
    stage_errors    этапы, упавшие с ошибкой (файл повторяется при следующем запуске);
    result / resolved_by — итоговое определение и этап, который его дал.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path

from engine.config import IMAGE_EXTENSIONS, ConfigSpec, Settings, StageSpec

MAX_ATTEMPTS = 3
CHUNK_SIZE = 100
EXIT_QUOTA = 3
EXIT_CONFIG = 4
EXIT_PENDING = 5
# Коды нативных падений из-за нехватки памяти (STATUS_NO_MEMORY, STATUS_COMMITMENT_LIMIT): это не вина файла.
MEMORY_CODES = {0xC0000017, 0xC000012D}
OTHER = "Other"
ALL_DIR = "0. ALL"
RUN_STARTED = "Начало работы"
MSG_STARTED = "этап начат"
MSG_RESUMED = "этап продолжен"
MSG_UNCHANGED = "этап без изменений"
MSG_FINISHED = "этап завершён"
# «этап отложен» — автоматический переход через заглохший этап (до 05.10, убран); остался в старых логах
MSG_DEFERRED = "этап отложен"
MSG_SKIPPED = "этап пропущен"
MSG_BACK = "возврат к этапу"       # кнопка «←»: назад к предыдущему этапу (engine\\run.py)      # вернулись, а этап так и не заработал — в этом запуске пропущен
MSG_PAUSED = "пауза"
MSG_CONTINUED = "продолжение"
# Старые имена этапов в состоянии и папках наборов, обработанных до переименования.
STAGE_ALIASES = {"AI-F51": "AI-F5", "AI-GPT": "AI-G6"}


class StageSkipped(RuntimeError):
    """Нажата кнопка «→» (пропустить этап) в окне набора: текущий этап бросается, конвейер идёт к следующему."""


class StageBack(RuntimeError):
    """Нажата кнопка «←» (вернуться к предыдущему этапу) в окне набора: текущий этап прерывается, предыдущий
    доделывает файлы, которых не касались ни он, ни текущий; потом текущий продолжается (engine\\run.py)."""


def skip_requested(logs: Path) -> bool:
    """В logs\\control.json есть запрос «пропустить этап» (его пишет окно набора, gui\\window.py)."""
    return bool(read_json(logs / "control.json").get("skip"))


def check_requests(logs: Path, where: str = "") -> None:
    """Запросы кнопок окна набора из logs\\control.json: «→» — StageSkipped, «←» — StageBack."""
    control = read_json(logs / "control.json")
    if control.get("skip"):
        raise StageSkipped("кнопка «→» в окне набора" + where)
    if control.get("back"):
        raise StageBack("кнопка «←» в окне набора" + where)


def clear_skip(logs: Path, *names: str) -> None:
    """Снять запросы кнопок (по умолчанию — оба)."""
    path = logs / "control.json"
    control = read_json(path)
    removed = [control.pop(name, None) for name in (names or ("skip", "back"))]
    if any(value is not None for value in removed):
        atomic_json(path, control)


class QuotaExhaustedError(RuntimeError):
    """Баланс ключа исчерпан: повторять бессмысленно, конвейер останавливается (код 3)."""


# ---------------------------------------------------------------- мелкие утилиты

def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    for _ in range(200):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            time.sleep(0.1)
    tmp.replace(path)


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {} if default is None else default


def sanitize(value: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", str(value)).strip(" .") or "_"


def pretty(value: str) -> str:
    joined = " ".join(word[:1].upper() + word[1:] for word in re.split(r"[_:]+", str(value)) if word)
    # Двоеточие/подчёркивание рядом с пробелом давали двойной пробел в имени папки — схлопываем.
    return " ".join(joined.split())


def clean_series(value) -> str:
    """Модель иногда отдаёт тайтл списком (["Blue Archive"]) или строкой "['Blue Archive']" — приводим к тексту."""
    if isinstance(value, (list, tuple)):
        return " / ".join(str(part).strip() for part in value if str(part).strip())
    text = str(value or "").strip()
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = ast.literal_eval(text)
        except (ValueError, SyntaxError):
            return text
        if isinstance(parsed, (list, tuple)):
            return clean_series(parsed)
    return text


def is_multi_title(value) -> bool:
    """Модель назвала несколько тайтлов сразу (["A", "B"] или "A / B") — это не определение, а сомнение."""
    return " / " in clean_series(value)


def is_unknown_title(value) -> bool:
    normalized = re.sub(r"[^a-zа-яё]+", " ", str(value or "").casefold()).strip()
    return normalized in {"неизвестный тайтл", "unknown", "unknown title", "unknown anime"}


def normalized_confidence(value) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def original_name(name: str) -> str:
    return name.lstrip("~")


def is_image(path: Path, include_marked: bool = False) -> bool:
    return path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS and (include_marked or not path.name.startswith("~"))


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_identity(path: Path) -> str:
    return f"{path.name}|{file_digest(path)}"


def new_item() -> dict:
    return {"checked_stages": [], "stage_results": {}, "stage_details": {}, "result": None}


def real_check(item: dict, stage: str) -> bool:
    """Файл реально проверен этапом (а не помечен старым конвейером «пропущен»)."""
    detail = item.get("stage_details", {}).get(stage)
    return stage in item.get("checked_stages", []) and isinstance(detail, dict) and detail.get("status") != "skipped"


def times(count: int) -> str:
    """2 раза, 5 раз, 21 раз."""
    tail = "раза" if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14) else "раз"
    return f"{count} {tail}"


def is_broken(item: dict) -> bool:
    """Битый файл: не открывается или стабильно ломает этап/процесс — дальше по этапам не идёт."""
    return bool(item.get("broken"))


# Форматы, которые Pillow точно читает: только для них «не открывается» значит «битый файл».
CHECK_READABLE = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def readable_problem(path: Path) -> str | None:
    """None — файл открывается как картинка; иначе — почему нет."""
    if path.stat().st_size == 0:
        return "пустой файл (0 байт)"
    if path.suffix.lower() not in CHECK_READABLE:
        return None
    try:
        from PIL import Image

        with Image.open(path) as image:
            image.verify()
    except Exception as exc:
        if type(exc).__name__ == "UnidentifiedImageError":
            return "не открывается как картинка (формат не распознан — файл повреждён или это не изображение)"
        detail = str(exc).replace(str(path), path.name)[:120]
        return f"не открывается как картинка ({type(exc).__name__}: {detail})"
    return None


def pending_error(item: dict, stage: str) -> bool:
    """Этап упал на файле и ещё не прошёл его — файл ждёт повтора."""
    return stage in item.get("stage_errors", {}) and not real_check(item, stage) and not is_broken(item)


def stage_confidence(item: dict, stage: str) -> float:
    detail = item.get("stage_details", {}).get(stage)
    return normalized_confidence(detail.get("confidence", 0.0)) if isinstance(detail, dict) else 0.0


def stage_stats(items: dict, stage: str) -> dict:
    """Итоги этапа по состоянию: единый источник для конвейера, снапшота и таблицы окна.

    checked — реально проверенные (определил + не определил); errors — упавшие и ещё не проверенные.
    """
    determined = not_determined = errors = 0
    for item in items.values():
        if real_check(item, stage):
            if item.get("stage_results", {}).get(stage):
                determined += 1
            else:
                not_determined += 1
        elif pending_error(item, stage):
            errors += 1
    return {
        "determined": determined,
        "not_determined": not_determined,
        "checked": determined + not_determined,
        "errors": errors,
        "expected": determined + not_determined + errors,
        "remained": errors,
        "touched": bool(determined or not_determined or errors),
    }


def rename_stage_keys(item: dict, old: str, new: str) -> bool:
    changed = False
    if old in item.get("checked_stages", []):
        item["checked_stages"] = list(dict.fromkeys(new if name == old else name for name in item["checked_stages"]))
        changed = True
    for field in ("stage_results", "stage_details", "stage_errors"):
        values = item.get(field)
        if isinstance(values, dict) and old in values:
            values.setdefault(new, values.pop(old))
            values.pop(old, None)
            changed = True
    if item.get("resolved_by") == old:
        item["resolved_by"] = new
        changed = True
    return changed


def config_signature(spec: ConfigSpec) -> list[dict]:
    """Что влияет на результат набора: при изменении завершённый набор открывается снова."""
    extra = [{"previous": "keep"}] if spec.keep_previous else []
    return extra + [
        {"id": stage.id, "type": stage.type, "model": stage.model,
         "accept": stage.accept, "from": stage.from_, "where": stage.where_text}
        for stage in spec.stages
    ]


# ---------------------------------------------------------------- набор

class Dataset:
    def __init__(self, root: Path, settings: Settings, spec: ConfigSpec):
        self.root = root
        self.input = root / "in"
        self.output = root / "out"
        self.work = root / "work"
        self.logs = root / "logs"
        self.cache = root / "cache"
        self.log_path = self.logs / "pipeline_events.jsonl"
        self.settings = settings
        self.spec = spec
        self.stages = spec.stages
        self.by_id = {stage.id: stage for stage in spec.stages}
        self.stage_dirs = {stage.id: f"{stage.index}. {stage.label}" for stage in spec.stages}
        self.other_dir = f"{len(spec.stages) + 1}. {OTHER}"
        self.states: dict[Path, dict] = {}
        self.chunk_of: dict[str, Path] = {}
        self.entries: list[tuple[Path, Path, str]] = []
        self.lock = threading.Lock()
        self.worked_this_run = False
        self.stages_file = self.work / "_stages.json"
        self.broken_on_prepare: list[tuple[str, str]] = []
        self.stages_done: list[str] = []

    # ---------- лог

    def log(self, stage: str, message: str, **data) -> None:
        event = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "stage": stage, "message": message, **data}
        with self.lock:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    # ---------- пауза (кнопка «Остановить» / «Продолжить» в окне набора)

    def checkpoint(self) -> None:
        """Перед каждым файлом: применить свободные ядра из live.json (если их поменяли на ходу)
        и, если в logs\\control.json стоит пауза, ждать, пока её снимут."""
        self.apply_cores()
        control = self.logs / "control.json"
        check_requests(self.logs)
        if not read_json(control).get("paused"):
            return
        self.log("PIPELINE", MSG_PAUSED)
        while read_json(control).get("paused"):
            # Пропуск/возврат во время паузы: этап бросается, пауза остаётся (следующий этап встанет на неё).
            check_requests(self.logs, " (во время паузы)")
            time.sleep(1)
        self.log("PIPELINE", MSG_CONTINUED)

    def apply_cores(self) -> None:
        from engine import live, winproc

        free = live.current().free_cores
        if free != getattr(self, "applied_free_cores", None):
            os.environ["ANIME_SORT_CORES"] = str(winproc.limit_cores(free))
            self.applied_free_cores = free

    # ---------- подготовка

    def prepare(self) -> None:
        """Раскладывает in\\ по chunk_XXXX (продолжая прошлую раскладку) и загружает состояние."""
        self.work.mkdir(parents=True, exist_ok=True)
        fresh = not any(self.work.glob("chunk_*/_state.json"))
        images = sorted((path for path in self.input.iterdir() if is_image(path)),
                        key=lambda path: (path.stat().st_mtime_ns, path.name.casefold()))
        existing = {original_name(path.name): path for chunk in self.work.glob("chunk_*") for path in chunk.iterdir()
                    if is_image(path, include_marked=True)}
        for index, image in enumerate(images):
            target = existing.get(image.name)
            if target is not None:
                if file_digest(target) != file_digest(image):
                    shutil.copy2(image, target)
                continue
            chunk = self.work / f"chunk_{index // CHUNK_SIZE + 1:04d}"
            chunk.mkdir(parents=True, exist_ok=True)
            shutil.copy2(image, chunk / image.name)
        seed = self.snapshot_items() if fresh else {}
        for chunk in sorted(self.work.glob("chunk_*")):
            state = read_json(chunk / "_state.json", {"items": {}})
            items = state.setdefault("items", {})
            for path in chunk.iterdir():
                if not is_image(path, include_marked=True):
                    continue
                key = original_name(path.name)
                identity = source_identity(path)
                if key not in items and key in seed and seed[key].get("source_id") in (None, identity):
                    # Завершённый набор открыт снова (изменился конфиг): состояние берётся из итогового снапшота.
                    items[key] = seed[key]
                item = items.setdefault(key, new_item())
                if item.get("source_id") and item["source_id"] != identity:
                    item.clear()
                    item.update(new_item())
                item["source_id"] = identity
                for old, new in STAGE_ALIASES.items():
                    rename_stage_keys(item, old, new)
                if (not self.spec.keep_previous and item.get("result") is not None and item.get("resolved_by")
                        and item["resolved_by"] not in self.by_id):
                    # Этап, определивший файл, убран из конфига: файл снова не определён и идёт по этапам
                    # (если этап вернуть — он определит файл заново; ответы моделей остаются в кэше).
                    item["result"] = None
                    item.pop("resolved_by", None)
                if "readable" not in item and not is_broken(item):
                    problem = readable_problem(path)
                    item["readable"] = problem is None
                    if problem:
                        self.broken_on_prepare.append((key, problem))
                self.chunk_of[key] = chunk
                self.entries.append((chunk, path, key))
            state.pop("neighbor_done", None)
            self.states[chunk] = state
            atomic_json(chunk / "_state.json", state)
        self.entries.sort(key=lambda entry: (entry[1].stat().st_mtime_ns, entry[2].casefold()))
        self.stages_done = list(read_json(self.stages_file, {}).get("done", []))

    def apply_broken_checks(self) -> None:
        """После строки «запуск»: пометить битыми файлы, которые не открываются, и файлы, на которых падал процесс."""
        for key, problem in self.broken_on_prepare:
            self.mark_broken(key, None, problem)
        self.broken_on_prepare = []
        self.apply_crashes()

    def snapshot_items(self) -> dict:
        snapshot = read_json(self.output / "status_snapshot.json")
        return snapshot.get("items", {}) if isinstance(snapshot.get("items"), dict) else {}

    # ---------- состояние файлов

    def item(self, key: str) -> dict:
        return self.states[self.chunk_of[key]]["items"][key]

    def items(self) -> dict:
        merged: dict = {}
        for state in self.states.values():
            merged.update(state.get("items", {}))
        return merged

    def save(self, key: str) -> None:
        chunk = self.chunk_of[key]
        atomic_json(chunk / "_state.json", self.states[chunk])

    def finish(self, key: str, path: Path, stage: StageSpec, details: dict, result: dict | None) -> None:
        item = self.item(key)
        checked = item.setdefault("checked_stages", [])
        if stage.id not in checked:
            checked.append(stage.id)
        item.setdefault("stage_results", {})[stage.id] = result
        item.setdefault("stage_details", {})[stage.id] = details
        item.get("stage_errors", {}).pop(stage.id, None)
        if result is not None:
            item["result"] = result
            item["resolved_by"] = stage.id
            self.copy_result(path, stage, result)
        self.save(key)

    def record_error(self, key: str, stage: StageSpec, payload: dict) -> None:
        """Ошибка не отмечает этап пройденным: следующий запуск повторит файл."""
        item = self.item(key)
        item.setdefault("stage_details", {})[stage.id] = payload
        item.setdefault("stage_errors", {})[stage.id] = payload.get("error_text", "")
        self.save(key)

    # ---------- битые файлы

    def mark_broken(self, key: str, stage: StageSpec | str | None, reason: str) -> None:
        """Файл больше не обрабатывается: остаётся не определённым (в «Other»), набор может завершиться.
        Пометка снимается сама, если заменить файл (другое содержимое — новое состояние)."""
        item = self.item(key)
        if is_broken(item):
            return
        stage_id = stage.id if isinstance(stage, StageSpec) else stage
        item["broken"] = {"stage": stage_id, "reason": reason, "time": time.strftime("%Y-%m-%d %H:%M:%S")}
        self.save(key)
        where = f" на этапе {self.by_id[stage_id].label}" if stage_id in self.by_id else ""
        self.log("PIPELINE", f"битый файл{where}: {reason}", file=key, broken=True, error=True, error_text=reason)
        self.console(f"битый файл {key}{where} — {reason}; отправлен в «Other»")

    def console(self, text: str) -> None:
        """Строка в журнал пачки — её показывает консоль start.vbs."""
        pids = read_json(self.logs / "pids.json")
        if not pids.get("batch_log"):
            return
        try:
            with Path(pids["batch_log"]).open("a", encoding="utf-8") as handle:
                handle.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {self.root.name.removeprefix('test-')}: {text}\n")
        except OSError:
            pass

    def apply_crashes(self) -> None:
        """logs\\crashes.json (пишет dataset.py): на каком файле падал процесс конвейера. Файл, на котором
        процесс упал broken_after_crashes раз, помечается битым."""
        from engine import live

        limit = live.current().broken_after_crashes
        for key, record in read_json(self.logs / "crashes.json").items():
            if key not in self.chunk_of or record.get("count", 0) < limit:
                continue
            if record.get("identity") not in (None, self.item(key).get("source_id")):
                continue   # файл заменили — старые падения не в счёт
            self.mark_broken(key, record.get("stage"),
                             f"процесс конвейера падал на этом файле {times(record['count'])} (код {record.get('code')})")

    def count_failed_run(self, key: str, stage: StageSpec, error_text: str) -> None:
        """Этап не справился с файлом в этом запуске (а с другими файлами справился): после
        broken_after_runs таких запусков файл считается битым."""
        from engine import live

        item = self.item(key)
        runs = item.setdefault("failed_runs", {})
        runs[stage.id] = runs.get(stage.id, 0) + 1
        self.save(key)
        limit = live.current().broken_after_runs
        if runs[stage.id] >= limit:
            self.mark_broken(key, stage, f"этап не смог обработать файл в {runs[stage.id]} запусках подряд: {error_text[:200]}")

    def write_broken_list(self) -> None:
        lines = [f"{key}\t{item['broken'].get('stage') or '-'}\t{item['broken'].get('reason', '')}"
                 for key, item in sorted(self.items().items()) if is_broken(item)]
        path = self.output / "broken.txt"
        if lines:
            path.write_text("файл\tэтап\tпричина\n" + "\n".join(lines) + "\n", encoding="utf-8")
        else:
            path.unlink(missing_ok=True)

    def pending(self) -> list[str]:
        """Нерешённые файлы с ошибкой хотя бы на одном этапе — будут повторены при следующем запуске."""
        return [key for key, item in self.items().items()
                if item.get("result") is None and any(pending_error(item, stage.id) for stage in self.stages)]

    # ---------- начало/конец этапа в логе

    def announce(self, stage: StageSpec, candidate_count: int, **extra) -> bool:
        """Начало этапа с ПОЛНЫМИ итогами (с учётом прошлых запусков).

        True — этап работает в этом запуске и в конце нужно «этап завершён».
        - этап ещё не начинался → видимое «этап начат»;
        - начат в прошлом запуске и есть что доделать → скрытое «этап продолжен» (если это первый работающий
          этап запуска — строки файлов идут сразу после «запуск»), иначе видимое «этап начат»;
        - делать нечего, а этап уже проходился → скрытое «этап без изменений».
        """
        stats = stage_stats(self.items(), stage.id)
        expected = stats["checked"] + candidate_count
        if candidate_count == 0 and (stats["touched"] or stage.id in self.stages_done):
            self.log(stage.id, MSG_UNCHANGED, expected=stats["expected"], determined=stats["determined"],
                     not_determined=stats["not_determined"], errors=stats["errors"], remained=stats["remained"], **extra)
            return False
        message = MSG_RESUMED if stats["touched"] and not self.worked_this_run else MSG_STARTED
        self.log(stage.id, message, expected=expected, determined=stats["determined"],
                 not_determined=stats["not_determined"], remained=expected - stats["checked"], **extra)
        self.worked_this_run = True
        return True

    def finish_log(self, stage: StageSpec, visible: bool, **extra) -> None:
        if stage.id not in self.stages_done:
            self.stages_done.append(stage.id)
            atomic_json(self.stages_file, {"done": self.stages_done})
        if not visible:
            return
        stats = stage_stats(self.items(), stage.id)
        self.log(stage.id, MSG_FINISHED, determined=stats["determined"], not_determined=stats["not_determined"],
                 errors=stats["errors"], expected=stats["expected"], remained=stats["remained"], **extra)

    # ---------- папки результатов

    def destination(self, root: Path, result: dict | None) -> Path:
        if not result:
            return root / OTHER
        title = sanitize(pretty(clean_series(result.get("series")) or "Неизвестный тайтл"))
        character = str(result.get("character") or "").strip()
        if result.get("kind") == "title_only" or not character:
            return root / title
        return root / title / sanitize(pretty(character))

    def prefix(self, stage_id: str | None) -> str:
        stage = self.by_id.get(stage_id or "")
        return f"{stage.label if stage else (stage_id or 'N')} "

    def copy_result(self, source: Path, stage: StageSpec, result: dict) -> None:
        target_dir = self.destination(self.output / self.stage_dirs[stage.id], result)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / (self.prefix(stage.id) + original_name(source.name))
        if not target.exists():
            shutil.copy2(source, target)
        (self.output / ALL_DIR / OTHER / original_name(source.name)).unlink(missing_ok=True)

    def migrate_output(self) -> None:
        r"""Приводит папки out\ к нумерации и меткам текущего конфига.

        Сопоставление: по out\layout.json прошлого запуска (id этапа -> папка), а у наборов старого
        конвейера — по метке в имени папки («3-1. AI-K3» -> этап с меткой AI-K3). Файлы внутри получают
        префикс новой метки. Папки этапов, убранных из конфига (по layout.json), удаляются — это копии,
        а определения этих этапов больше не действуют (см. prepare).
        """
        self.output.mkdir(parents=True, exist_ok=True)
        if self.spec.keep_previous:
            self.number_after_existing()
        previous = read_json(self.output / "layout.json")
        old_dirs: dict[str, tuple[str, str]] = {}   # id этапа -> (папка, метка на момент записи)
        for entry in previous.get("stages", []):
            if isinstance(entry, dict) and entry.get("id") and entry.get("dir"):
                old_dirs[entry["id"]] = (entry["dir"], entry.get("label") or entry["id"])
        for stage_id, (old_name, _) in list(old_dirs.items()):
            if stage_id not in self.by_id and self.spec.keep_previous:
                old_dirs.pop(stage_id)   # папки прошлых прогонов остаются как есть
            elif stage_id not in self.by_id:
                old_dirs.pop(stage_id)
                if old_name and old_name != ALL_DIR and (self.output / old_name).is_dir():
                    shutil.rmtree(self.output / old_name, ignore_errors=True)
        label_to_id = {stage.label.casefold(): stage.id for stage in self.stages}
        for alias, target in STAGE_ALIASES.items():
            if target in self.by_id:
                label_to_id.setdefault(alias.casefold(), target)
        other_dirs = []
        for path in list(self.output.iterdir()):
            match = re.match(r"^\d+(?:-\d+)?\. (.+)$", path.name)
            if not path.is_dir() or not match or path.name == ALL_DIR:
                continue
            label = match.group(1)
            if label == OTHER:
                if path.name != self.other_dir:
                    other_dirs.append(path)
                continue
            stage_id = label_to_id.get(label.casefold())
            if stage_id and stage_id not in old_dirs:
                old_dirs[stage_id] = (path.name, label)
        # Два шага (сначала во временные имена), чтобы перестановка этапов не слила папки друг с другом.
        moved = []
        for stage in self.stages:
            if stage.id not in old_dirs:
                continue
            old_name, old_label = old_dirs[stage.id]
            old_path = self.output / old_name
            if not old_path.is_dir() or (old_name == self.stage_dirs[stage.id] and old_label == stage.label):
                continue
            temporary = self.output / f".migrate-{stage.index}"
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
            old_path.rename(temporary)
            moved.append((temporary, stage, old_label))
        for temporary, stage, old_label in moved:
            self.merge_dir(temporary, self.output / self.stage_dirs[stage.id], old_label, stage.label)
        for path in other_dirs:
            self.merge_dir(path, self.output / self.other_dir, None, None)
        for stage in self.stages:
            (self.output / self.stage_dirs[stage.id]).mkdir(parents=True, exist_ok=True)
        (self.output / self.other_dir).mkdir(parents=True, exist_ok=True)
        (self.output / ALL_DIR).mkdir(parents=True, exist_ok=True)
        atomic_json(self.output / "layout.json", {
            "stages": [{"id": stage.id, "label": stage.label, "dir": self.stage_dirs[stage.id]} for stage in self.stages],
            "other": self.other_dir,
        })

    def number_after_existing(self) -> None:
        """previous = "keep": этапы конфига нумеруются после папок этапов прошлых прогонов
        (у набора старого конвейера «1. WD-14» … «5. N» -> «6. N-LR», «7. Other»)."""
        own = {stage.label.casefold() for stage in self.stages}
        highest = 0
        for path in self.output.iterdir():
            match = re.match(r"^(\d+)(?:-\d+)?\. (.+)$", path.name)
            if path.is_dir() and match and path.name != ALL_DIR and match.group(2) != OTHER and match.group(2).casefold() not in own:
                highest = max(highest, int(match.group(1)))
        self.stage_dirs = {stage.id: f"{highest + stage.index}. {stage.label}" for stage in self.stages}
        self.other_dir = f"{highest + len(self.stages) + 1}. {OTHER}"

    @staticmethod
    def merge_dir(old: Path, new: Path, old_label: str | None, new_label: str | None) -> None:
        if old.is_dir() and old != new:
            if not new.exists():
                old.rename(new)
            else:
                for path in list(old.rglob("*")):
                    if path.is_file():
                        target = new / path.relative_to(old)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        if not target.exists():
                            shutil.move(str(path), target)
                shutil.rmtree(old, ignore_errors=True)
        if old_label and new_label and old_label != new_label and new.is_dir():
            for path in list(new.rglob(f"{old_label} *")):
                target = path.with_name(new_label + path.name[len(old_label):])
                if path.is_file() and not target.exists():
                    path.rename(target)

    def initialize_all(self) -> None:
        """«0. ALL\\Other» сразу содержит все ещё не определённые файлы; определение убирает файл оттуда."""
        other = self.output / ALL_DIR / OTHER
        other.mkdir(parents=True, exist_ok=True)
        for _, path, key in self.entries:
            target = other / key
            if self.item(key).get("result") is not None:
                target.unlink(missing_ok=True)
            elif not target.exists():
                shutil.copy2(path, target)

    def rebuild_all(self) -> None:
        all_root = self.output / ALL_DIR
        if all_root.exists():
            shutil.rmtree(all_root)
        all_root.mkdir(parents=True, exist_ok=True)
        for _, path, key in self.entries:
            result = self.item(key).get("result")
            target_dir = self.destination(all_root, result)
            target_dir.mkdir(parents=True, exist_ok=True)
            name = (self.prefix(self.item(key).get("resolved_by")) if result else "") + key
            shutil.copy2(path, target_dir / name)

    def sync_other(self) -> None:
        self.write_broken_list()
        self.sync_other_dir()

    def sync_other_dir(self) -> None:
        """«<n+1>. Other» — ровно не определённые файлы (после всех этапов)."""
        other = self.output / self.other_dir
        other.mkdir(parents=True, exist_ok=True)
        unresolved = {key for _, _, key in self.entries if self.item(key).get("result") is None}
        for path in other.iterdir():
            if path.is_file() and original_name(path.name) not in unresolved:
                path.unlink()
        for _, path, key in self.entries:
            if key in unresolved and not (other / key).exists():
                shutil.copy2(path, other / key)
