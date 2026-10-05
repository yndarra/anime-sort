r"""Api — всё, что вызывает страница Пульта (window.pywebview.api.<метод>() → Promise).

Тонкий слой над уже готовыми модулями: context (наборы, конвейеры, провайдеры, ключи), tasks (инструменты
подпроцессом), checks (проверка конфигов кодом движка), thumbs (миниатюры), gui\stats.py (модели, API и пачки —
тот же сборщик, что у главного окна). pywebview зовёт методы в отдельных потоках, поэтому долгие вызовы
(проверка всех API, подсчёт Waifu) окно не подвешивают.

Правило: значения ключей API страница не получает никогда — только «есть / пусто»; вставка ключа пишет его в файл
здесь, на стороне Python.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from control import checks, context, thumbs
from control.tasks import TaskManager

MEDIA = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".avif"}
NAMES = context.PROJECT / "tools" / "fix_name" / "names.json"
HISTORY = context.LOGS / "history.json"
NO_PAUSE = "--no-pause"

# Инструменты страницы «Инструменты»: ключ → (группа, название, описание, скрипт, аргументы «Показать» или None,
# аргументы «Выполнить», меняет Waifu, предупреждение перед запуском).
TOOLS = {
    "add": ("Waifu", "Слияние наборов", "Готовые test-dataN → Waifu (дубли по SHA-256), потом номера папок",
            "tools/add/add.py", ["--dry-run", NO_PAUSE], [NO_PAUSE], True, ""),
    "sort": ("Waifu", "Нумерация папок", "Снять номера → применить names.json → пронумеровать по числу картинок",
             "tools/sort/sort.py", ["--no-edit", NO_PAUSE], [NO_PAUSE], True, ""),
    "sort_cancel": ("Waifu", "Снять номера", "Убрать «123. » у тайтлов и персонажей (одноимённые папки сольются)",
                    "tools/sort/sort_cancel.py", ["--dry-run", NO_PAUSE], [NO_PAUSE], True, ""),
    "small": ("Waifu", "Мелкие тайтлы → Other", "Персонажи мелких тайтлов — в большие тайтлы, остальное — в Other",
              "tools/cleanup/small_titles.py", None, [], True, ""),
    "other": ("Waifu", "Other → dataN-other", "Всё из Waifu\\Other — во временные папки второго круга",
              "tools/other.py", None, [], True, ""),
    "fix_name": ("Имена", "Применить names.json", "Переименовать и слить папки Waifu по правилам",
                 "tools/fix_name/fix_name.py", ["--dry-run", NO_PAUSE], [NO_PAUSE], True, ""),
    "honkai": ("Имена", "Honkai по играм", "Разнести «Honkai (series)» по Honkai Impact 3rd / Star Rail",
               "tools/fix_name/honkai.py", ["--dry-run", NO_PAUSE], [NO_PAUSE], True, ""),
    "names_agent": ("Имена", "Агент имён", "LLM находит дубли тайтлов и персонажей и предлагает правила names.json",
                    "tools/agent/names_agent.py", ["--dry-run"], [], False,
                    "Агент тратит баланс OpenRouter (ключ агента). Запустить?"),
    "dublicates": ("Файлы", "Одинаковые имена файлов", "Сделать имена файлов в Waifu уникальными",
                   "tools/fix_name/dublicates.py", ["--once", "--dry-run", NO_PAUSE], ["--once", NO_PAUSE], True, ""),
    "ani": ("Файлы", "Каталог тайтлов (ani.txt)", "Частые тайтлы и персонажи Waifu — подсказка этапу JSON",
            "tools/ani/ani.py", None, [NO_PAUSE], False, ""),
    "mark_broken": ("Наборы", "Пометить битые файлы", "Файлы, на которых этап не справился, — битые (уйдут в Other)",
                    "tools/mark_broken/mark_broken.py", [], ["--apply"], False, ""),
    "reset_no_image": ("Наборы", "Сбросить «нет картинки»", "Снять ошибки «модель не получила картинку»",
                       "tools/mark_broken/reset_no_image.py", [], ["--apply"], False, ""),
    "check": ("Наборы", "Проверить конфиги", "Все конфиги и live.json — тем же кодом, что при запуске",
              "engine/main.py", None, ["--check"], False, ""),
    "watch_titles": ("Наблюдатели", "Номера тайтлов", "Держит номера тайтлов Waifu (останавливается в «Задачах»)",
                     "tools/watch/number_titles.py", None, [NO_PAUSE], False, ""),
    "watch_characters": ("Наблюдатели", "Номера персонажей", "Держит номера персонажей", "tools/watch/number_characters.py",
                         None, [NO_PAUSE], False, ""),
    "watch_empty": ("Наблюдатели", "Пустые папки", "Убирает пустые папки Waifu", "tools/watch/empty_dirs.py",
                    None, [NO_PAUSE], False, ""),
}


def to_recycle_bin(path: Path) -> None:
    """В Корзину, а не безвозвратно (штатно, через Microsoft.VisualBasic.FileIO)."""
    script = ("Add-Type -AssemblyName Microsoft.VisualBasic; "
              f"[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile('{path}', 'OnlyErrorDialogs', 'SendToRecycleBin')")
    subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


class Api:
    def __init__(self, start_page: str = "overview"):
        self.start = start_page
        self.tasks = TaskManager()
        self.stats = None
        self.stats_running = None
        self.waifu_cache: dict | None = None
        self.waifu_time = 0.0
        self.lock = threading.Lock()

    # ================================================================ общее

    def start_page(self) -> str:
        """Страница при открытии (--page в командной строке)."""
        return self.start

    def poll(self, cursors=None) -> dict:
        """Раз в секунду со страницы: задачи (+ новые строки показываемых), идут ли конвейеры, время."""
        result = self.tasks.poll(cursors or {})
        result["pipelines"] = context.pipelines_running()
        result["time"] = time.strftime("%H:%M:%S")
        return result

    def run_tool(self, key: str, preview: bool = False, extra=None) -> int:
        group, title, about, script, preview_args, apply_args, waifu, warning = TOOLS[key]
        args = (preview_args if preview and preview_args is not None else apply_args) + list(extra or [])
        task = self.tasks.run(f"{title}{' — показать' if preview else ''}", script, *args,
                              waifu=waifu and not preview)
        return task.id

    def tools(self) -> list[dict]:
        return [{"key": key, "group": group, "title": title, "about": about, "preview": preview is not None,
                 "warning": warning} for key, (group, title, about, _, preview, _, _, warning) in TOOLS.items()]

    def task_answer(self, number: int, text: str) -> None:
        task = self.tasks.get(int(number))
        if task:
            task.answer(text)

    def task_stop(self, number: int) -> None:
        task = self.tasks.get(int(number))
        if task:
            task.stop()

    # ================================================================ обзор

    def overview(self) -> dict:
        found = context.datasets()
        sets = [{"name": d.name, "mode": d.mode, "status": d.status, "statusText": context.STATUS_TEXT[d.status],
                 "resolved": d.resolved, "total": d.total, "broken": d.broken} for d in found]
        resolved, total = sum(d.resolved for d in found), sum(d.total for d in found)
        waifu = self.waifu()
        point = {"time": time.strftime("%Y-%m-%d %H:%M"), "datasets": len(found), "resolved": resolved,
                 "total": total, "images": waifu.get("images"), "other": waifu.get("other")}
        history = self.history(point)
        probes = context.read_json(context.LOGS / "probes.json")
        return {"datasets": sets, "resolved": resolved, "total": total, "broken": sum(d.broken for d in found),
                "waifu": waifu, "history": history, "live": self.live(), "probes": probes,
                "pipelines": context.pipelines_running()}

    def waifu(self) -> dict:
        """Тайтлы, картинки и Other в Waifu (подсчёт — десятки тысяч файлов, поэтому кэш на минуту)."""
        with self.lock:
            if self.waifu_cache is not None and time.time() - self.waifu_time < 60:
                return self.waifu_cache
            current = context.settings()
            if current is None or not current.waifu.is_dir():
                self.waifu_cache = {"error": "Waifu не найдена (config.json → collection)"}
            else:
                titles = [p for p in current.waifu.iterdir() if p.is_dir() and p.name != "Other"]
                images = sum(1 for p in current.waifu.rglob("*") if p.suffix.lower() in MEDIA)
                other_dir = current.waifu / "Other"
                other = sum(1 for p in other_dir.rglob("*") if p.suffix.lower() in MEDIA) if other_dir.is_dir() else 0
                self.waifu_cache = {"titles": len(titles), "images": images, "other": other}
            self.waifu_time = time.time()
            return self.waifu_cache

    @staticmethod
    def history(point: dict) -> list[dict]:
        """История показателей для мини-графиков: не чаще точки в час, последние 60 точек."""
        history = context.read_json(HISTORY, [])
        if not isinstance(history, list):
            history = []
        if point.get("images") is not None and (not history or history[-1].get("time", "")[:13] != point["time"][:13]):
            history.append(point)
            history = history[-60:]
            context.write_json(HISTORY, history)
        return history

    def live(self) -> dict:
        """Модели и API за сегодня, пачки — тот же сборщик, что у главного окна (gui\\stats.py)."""
        from stats import Stats

        running = context.pipelines_running()
        if self.stats is None or running != self.stats_running:
            self.stats = Stats(context.DispatcherView(), width=66)
            self.stats_running = running
        stats = self.stats
        stats.collect()
        models = []
        for model, counter in sorted(stats.models.items(), key=lambda item: -item[1]["checked"]):
            gaps = sorted(stats.gaps[model])
            models.append({"model": model, "checked": counter["checked"], "determined": counter["determined"],
                           "errors": counter["errors"], "seconds": round(gaps[len(gaps) // 2], 1) if gaps else None})
        apis = []
        now = time.time()
        for api, entry in sorted(stats.apis.items(), key=lambda item: (-item[1]["files"], item[0])):
            until = entry["sleep_until"]
            sleeping = until is not None and until.timestamp() > now
            apis.append({"api": api, "files": entry["files"], "errors": entry["errors"],
                         "last": str(entry["last_ok"] or "")[11:16],
                         "state": f"{entry['sleep_kind']} до {until.strftime('%H:%M')}" if sleeping else "работает",
                         "ok": not sleeping})
        return {"models": models, "apis": apis, "batches": self.batches(stats)}

    @staticmethod
    def batches(stats) -> list[dict]:
        rows = []
        dispatcher = stats.dispatcher
        if dispatcher.settings is None:
            return rows
        results = dispatcher.settings.results
        seen = set()
        for spec in dispatcher.started:
            current, left = None, 0
            for folder in spec.folders:
                if folder in seen:
                    continue
                seen.add(folder)
                status = stats.dataset_status(results / f"test-{folder}")
                if status == "active":
                    current = folder
                if status not in ("done", "completed"):
                    left += 1
            row = {"config": spec.file, "folder": current, "left": left, "stage": "", "resolved": 0, "total": 0,
                   "running": ""}
            if current:
                root = results / f"test-{current}"
                row["resolved"], row["total"] = stats.progress(root)
                row["stage"] = stats.current_stage(root)
                row["running"] = stats.running_for(root)
            rows.append(row)
        return rows

    def thumbs(self, name: str) -> list[str]:
        return thumbs.for_dataset(name)

    def open_dataset(self, name: str, what: str = "folder") -> None:
        current = context.settings()
        if current is None:
            return
        result = current.results / f"test-{name}"
        if what == "folder":
            context.open_path(result if result.exists() else current.source / name)
            return
        logs = result / "logs"
        if (logs / "pipeline_events.jsonl").exists():
            python = Path(sys.executable).with_name("pythonw.exe")
            subprocess.Popen([str(python if python.exists() else sys.executable), str(context.PROJECT / "gui" / "window.py"),
                              "--log", str(logs / "pipeline_events.jsonl"),
                              "--snapshot", str(result / "out" / "status_snapshot.json"),
                              "--layout", str(logs / "layout.json"), "--title", result.name])

    # ================================================================ конвейеры

    def pipeline_state(self, mode: str) -> dict:
        """Для страниц «Конвейеры» / «Второй круг»: идут ли конвейеры, пачки, наборы этого режима."""
        live = self.live()
        sets = [{"name": d.name, "status": d.status, "statusText": context.STATUS_TEXT[d.status],
                 "resolved": d.resolved, "total": d.total} for d in context.datasets() if d.mode == mode]
        return {"running": context.pipelines_running(), "batches": live["batches"], "datasets": sets}

    def start_pipelines(self) -> str:
        if context.pipelines_running():
            return "Главное окно anime-sort уже открыто."
        context.start_pipelines()
        return "Запускаю конвейеры…"

    def stop_pipelines(self) -> str:
        if not context.pipelines_running():
            return "Конвейеры не запущены."
        context.stop_pipelines()
        return "Попросил главное окно закрыться — пачки и окна наборов закроются вместе с ним."

    def step(self, action: str, mode: str = "main", value=None) -> int:
        """Шаги страниц «Конвейеры» / «Второй круг» → номер задачи."""
        mode_title = "второй круг" if mode == "other" else "обычный"
        if action == "agent":
            return self.tasks.run("Агент конфигов", "tools/agent/config_agent.py").id
        if action == "prepare":
            return self.tasks.run(f"Подготовить ({mode_title})", "tools/prepare.py", "--mode", mode, "--no-agent").id
        if action == "finish":
            return self.tasks.run("Finish", "tools/finish.py", waifu=True).id
        if action == "small":
            args = ["--max", str(int(value))] if value else []
            return self.tasks.run("Мелкие тайтлы → Other", "tools/cleanup/small_titles.py", *args, waifu=True).id
        if action == "other":
            return self.tasks.run("Other → dataN-other", "tools/other.py", waifu=True).id
        raise ValueError(f"неизвестный шаг {action}")

    def other_settings(self) -> dict:
        import modes

        return modes.other_settings()

    def save_other_settings(self, data: dict) -> str:
        path = context.CONFIGS / "other" / "settings.json"
        current = context.read_json(path)
        for key in ("other_batch_size", "small_title_max_files"):
            if key in data:
                current[key] = int(data[key])
        context.write_json(path, current)
        return "Сохранено"

    # ================================================================ конфиги

    def config_files(self) -> list[dict]:
        found = [{"group": "Обычный режим", "title": "Шаблон пачки", "path": "main/template.json", "kind": "template"},
                 {"group": "Второй круг", "title": "Шаблон пачки", "path": "other/template.json", "kind": "template"},
                 {"group": "Второй круг", "title": "Настройки", "path": "other/settings.json", "kind": "flat"},
                 {"group": "Общие", "title": "config.json — коллекция, этапы, окна", "path": "config.json", "kind": "config"},
                 {"group": "Общие", "title": "live.json — на ходу", "path": "live.json", "kind": "flat"},
                 {"group": "Общие", "title": "agent.json — агенты", "path": "agent.json", "kind": "flat"}]
        for path in sorted(context.CONFIGS.glob("config*.json"), key=lambda p: (len(p.name), p.name)):
            if path.name[6:-5].isdigit():
                found.append({"group": "Пачки (пишет prepare)", "title": path.stem, "path": path.name, "kind": "readonly"})
        return found

    def config_meta(self) -> dict:
        """Списки для форм: модели, маршруты ключей, типы этапов, подписи этапов, диапазоны live.json, мониторы."""
        from engine import live

        monitors = []
        try:
            from engine import winlayout

            monitors = [item["number"] for item in winlayout.monitors()]
        except Exception:
            pass
        return {"models": context.models(), "routes": [{"route": r, "filled": f} for r, f in context.key_routes()],
                "types": checks.STAGE_TYPES, "names": sorted(context.stage_names()),
                "limits": {key: list(value) for key, value in live.limits().items()}, "monitors": monitors}

    def read_config(self, relative: str) -> dict:
        path = self.config_path(relative)
        text = path.read_text(encoding="utf-8") if path.exists() else "{}"
        try:
            return {"data": json.loads(text), "text": text}
        except ValueError as exc:
            return {"data": None, "text": text, "error": f"ошибка JSON: {exc}"}

    def check_config(self, relative: str, data: dict) -> list[str]:
        return checks.check_file(relative, data)

    def check_where(self, text: str) -> str:
        return checks.check_where(text)

    def save_config(self, relative: str, data: dict, force: bool = False) -> dict:
        """Сохранить (прежний файл — .bak). Есть ошибки (не «совет:») и не force — не сохранять, вернуть их."""
        problems = checks.check_file(relative, data)
        errors = [p for p in problems if not p.startswith("совет:")]
        if errors and not force:
            return {"saved": False, "problems": problems}
        path = self.config_path(relative)
        if path.exists():
            shutil.copy2(path, path.with_suffix(".json.bak"))
        context.write_json(path, data)
        return {"saved": True, "problems": problems}

    @staticmethod
    def config_path(relative: str) -> Path:
        path = (context.CONFIGS / relative).resolve()
        if context.CONFIGS.resolve() not in path.parents or path.suffix != ".json":
            raise ValueError("путь вне configs")
        return path

    # ================================================================ names.json

    def names(self) -> dict:
        data = context.read_json(NAMES) or {}
        data.setdefault("new", {})
        data.setdefault("titles", {})
        return data

    def save_names(self, data: dict) -> str:
        if NAMES.exists():
            shutil.copy2(NAMES, NAMES.with_suffix(".json.bak"))
        NAMES.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return "Сохранено"

    def regroup_names(self, data: dict) -> dict:
        sys.path.insert(0, str(context.PROJECT / "tools" / "fix_name"))
        from regroup import add_rules

        self.save_names(data)
        add_rules(NAMES, [])
        return self.names()

    def apply_names(self, preview: bool) -> int:
        args = ["--dry-run", NO_PAUSE] if preview else [NO_PAUSE]
        return self.tasks.run("names.json → Waifu" + (" — показать" if preview else ""), "tools/fix_name/fix_name.py",
                              *args, waifu=not preview).id

    # ================================================================ API и ключи

    def providers(self) -> list[dict]:
        found = []
        for name, data in context.providers().items():
            path = context.PROVIDERS / name / "provider.json"
            found.append({"name": name, "text": path.read_text(encoding="utf-8"), "models": len(data.get("models") or {}),
                          "endpoint": data.get("endpoint", ""), "images": data.get("images", True)})
        return found

    def save_provider(self, name: str, text: str) -> dict:
        from engine.config import Report, load_provider

        try:
            data = json.loads(text)
        except ValueError as exc:
            return {"saved": False, "error": f"ошибка JSON: {exc}"}
        path = context.PROVIDERS / name / "provider.json"
        backup = path.read_text(encoding="utf-8") if path.exists() else None
        context.write_json(path, data)
        report = Report()
        if load_provider(name, report, "Пульт") is None:
            if backup is not None:
                path.write_text(backup, encoding="utf-8")
            return {"saved": False, "error": "; ".join(e.splitlines()[0] for e in report.errors[:2])}
        return {"saved": True}

    def new_provider(self, name: str) -> dict:
        name = name.strip()
        if not name or not name.replace("-", "").replace("_", "").isalnum():
            return {"ok": False, "error": "имя — латиница и цифры, без пробелов"}
        folder = context.PROVIDERS / name
        if folder.exists():
            return {"ok": False, "error": "такой провайдер уже есть"}
        example = context.read_json(context.PROVIDERS / "example" / "provider.json")
        context.write_json(folder / "provider.json", example or {"endpoint": "https://…/v1/chat/completions",
                                                                 "auth": "bearer", "models": {}})
        (context.KEYS / name).mkdir(parents=True, exist_ok=True)
        return {"ok": True}

    def keys(self) -> list[dict]:
        rows = [{"route": route, "filled": filled} for route, filled in context.key_routes()]
        agent = context.AGENT_KEY.exists() and bool(context.AGENT_KEY.read_text(encoding="utf-8-sig").strip())
        rows.append({"route": "agent/openrouter", "filled": agent, "agent": True})
        return rows

    def put_key(self, provider: str, key: str, value: str) -> dict:
        """Ключ — только в файл secrets\\…; обратно страница его не получает."""
        provider, key, value = provider.strip(), key.strip(), value.strip()
        if not provider or not key or not value or " " in value:
            return {"ok": False, "error": "нужны провайдер, имя ключа и сам ключ (одной строкой, без пробелов)"}
        if provider == "agent":
            path = context.AGENT_KEY
        else:
            if not key.replace("-", "").replace("_", "").isalnum():
                return {"ok": False, "error": "имя ключа — латиница и цифры"}
            path = context.KEYS / provider / f"{key}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value + "\n", encoding="utf-8")
        return {"ok": True}

    def remove_key(self, route: str) -> dict:
        path = context.AGENT_KEY if route == "agent/openrouter" else context.KEYS / f"{route}.txt"
        if not path.exists() or context.PROJECT / "secrets" not in path.resolve().parents:
            return {"ok": False, "error": "нет такого ключа"}
        to_recycle_bin(path)
        return {"ok": True}

    def survey(self) -> dict:
        """Все ключи × все модели (config_agent.survey) + балансы OpenRouter; итог — в logs\\probes.json."""
        sys.path.insert(0, str(context.PROJECT / "tools" / "agent"))
        import config_agent
        import probe
        from openrouter import credits

        routes = config_agent.routes()
        probes = config_agent.survey(routes)
        rows = [{"route": r, "model": m, "images": i, "status": p.status, "detail": p.detail[:160],
                 "seconds": round(p.seconds, 1)} for (r, m, i), p in sorted(probes.items())]
        data = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "probes": rows}
        context.write_json(context.LOGS / "probes.json", data)
        balances = []
        for route, provider in routes:
            if "openrouter.ai" in provider.endpoint:
                value = credits(probe.read_key(route))
                if value is not None:
                    balances.append({"route": route, "usd": round(value, 2)})
        agent = context.AGENT_KEY.read_text(encoding="utf-8-sig").strip() if context.AGENT_KEY.exists() else ""
        if agent:
            value = credits(agent)
            if value is not None:
                balances.append({"route": "ключ агентов", "usd": round(value, 2)})
        data["balances"] = balances
        return data

    def probes(self) -> dict:
        return context.read_json(context.LOGS / "probes.json")
