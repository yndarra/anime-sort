r"""prepare.bat — подготовка запуска: проверить API, найти необработанные папки, собрать конфиги пачек.

0. По вопросу — агент конфигов (tools\agent\config_agent.py): опрос всех ключей и моделей, новые template*.json
   под то, что сейчас работает и сколько где денег (платно: OpenRouter, ключ агента). --no-agent — не спрашивать.
1. Папки: все dataN (и dataN-other) коллекции, у которых набор test-dataN ещё не готов (нет или остались
   необработанные файлы). Готовые наборы получают значок-галочку (engine\marks.py).
2. API: каждый ключ-кандидат из configs\template.json проверяется маленьким запросом (tools\probe.py):
   работает / нет баланса / не получает картинку / нет ключа / ошибка. Таблица — в окне.
3. Пачки: сколько запускать одновременно = min(max_batches, папок, рабочих ключей первого AI-этапа).
   Папки делятся между пачками подряд; в каждой пачке у этапов остаются только рабочие API, а первым
   стоит другой ключ (нагрузка делится). Этап без единого рабочего API выключается (с пометкой).
   Пишутся configs\config1.json … configN.json и "run" в configs\config.json; лишние старые configN удаляются.
4. Проверка конфигов (engine\main.py --check) и вопрос «Запустить конвейеры?».
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "tools"))
sys.path.insert(1, str(PROJECT))

import console  # noqa: E402
from console import UserError  # noqa: E402

CONFIGS = PROJECT / "configs"
STATUS_COLOR = {"ok": console.GREEN, "no_balance": console.RED, "no_image": console.YELLOW, "no_key": console.GREY,
                "error": console.RED}
STATUS_TEXT = {"ok": "работает", "no_balance": "нет баланса", "no_image": "теряет картинку", "no_key": "нет ключа",
               "error": "ошибка"}
FOLDER_RE = re.compile(r"^data(\d+)(-other)?$")


def number_key(name: str) -> tuple:
    match = FOLDER_RE.match(name)
    return (int(match.group(1)), bool(match.group(2))) if match else (10 ** 9, name)


def pending_folders(batches: Path, results: Path) -> list[str]:
    from engine import marks

    pending = []
    for folder in sorted((p for p in batches.iterdir() if p.is_dir() and FOLDER_RE.match(p.name)), key=lambda p: number_key(p.name)):
        result = results / f"test-{folder.name}"
        # Уже влит в Waifu (DONE.txt) — готов, даже если остался work\; старые наборы, обработанные частями
        # (test-data80_1 … _5), — готовы, если влиты все части.
        parts = sorted(results.glob(f"test-{folder.name}_*"))
        merged = (result / "DONE.txt").exists() or (parts and all((part / "DONE.txt").exists() for part in parts))
        if merged and not marks.is_marked(folder):
            marks.mark_done(folder)
        if merged:
            continue
        if marks.completed(result):
            if not marks.is_marked(folder):
                marks.mark_done(folder)
                marks.mark_done(result)
            continue
        pending.append(folder.name)
    # Наборы второго круга: dataN-other удаляется, как только файлы перенесены в test-dataN-other\in —
    # незавершённый такой набор ищется уже по папке результатов.
    for result in sorted(results.glob("test-data*-other"), key=lambda p: number_key(p.name.removeprefix("test-"))):
        name = result.name.removeprefix("test-")
        if name in pending or not FOLDER_RE.match(name) or (batches / name).exists():
            continue
        if (result / "DONE.txt").exists() or marks.completed(result) or not (result / "in").is_dir():
            continue
        pending.append(name)
    return sorted(pending, key=number_key)


def route_of(item) -> tuple[str, str | None]:
    """Элемент списка api: строка «пров/ключ» или {"api", "model"} → (route, модель или None)."""
    if isinstance(item, dict):
        return item["api"], item.get("model")
    return item, None


def stage_pairs(template: dict) -> list[tuple[str, str, bool]]:
    pairs = []
    for stage in template["stages"]:
        for item in stage.get("api", []):
            route, model = route_of(item)
            pairs.append((route, model or stage["model"], stage["type"] == "ai_image"))
        fallback = stage.get("fallback") or {}
        for item in fallback.get("api", []):
            route, model = route_of(item)
            pairs.append((route, model or fallback["model"], stage["type"] == "ai_image"))
    return pairs


def show_probes(probes: dict) -> None:
    console.title("Проверка API")
    for (route, model, images), probe in sorted(probes.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        kind = "картинка" if images else "текст"
        text = f"  {route:<16} {model:<20} {kind:<9} {STATUS_TEXT[probe.status]:<16}"
        if probe.ok:
            text += f"{probe.seconds:5.1f} с"
        else:
            text += probe.detail[:70]
        console.say(text, STATUS_COLOR[probe.status])


def table_name(model: str) -> str:
    """Название этапа в таблице по модели: claude-opus-5 → «AI Opus 5», gpt-6-sol → «AI GPT-6»."""
    parts = model.removeprefix("claude-").split("-")
    if parts[0] == "gpt":
        return f"AI GPT-{parts[1]}" if len(parts) > 1 else "AI GPT"
    return "AI " + " ".join(part.capitalize() if part.isalpha() else part for part in parts)


def rotate_ai(stages: list[dict], index: int) -> list[dict]:
    """"rotate_ai": true в шаблоне — пачки работают одновременно, поэтому у каждой AI-этапы идут со своего места
    по кругу: пачка 1 — как в шаблоне, пачка 2 — со второго AI-этапа и т. д. Тогда в один момент пачки сидят на
    разных моделях (и, если в шаблоне провайдеры чередуются, на разных провайдерах) и меньше мешают друг другу.
    Остальные этапы (WD-14, Camie, JSON, соседи) остаются на своих местах. Годится только для этапов без
    from/where — им не важно, кто стоит выше."""
    places = [i for i, stage in enumerate(stages) if stage.get("type") == "ai_image"]
    if len(places) < 2:
        return stages
    ai = [stages[i] for i in places]
    shift = index % len(ai)
    ai = ai[shift:] + ai[:shift]
    result = list(stages)
    for place, stage in zip(places, ai):
        result[place] = stage
    return result


def build_configs(template: dict, probes: dict, folders: list[str]) -> list[dict]:
    """Конфиги пачек: рабочие API этапов (с поворотом — у каждой пачки свой первый ключ), папки поровну подряд."""
    def working(stage) -> list:
        result = []
        for item in stage.get("api", []):
            route, model = route_of(item)
            if probes[(route, model or stage["model"], stage["type"] == "ai_image")].ok:
                result.append(item)
        return result

    ai_stages = [stage for stage in template["stages"] if stage.get("type") == "ai_image"]
    first = ai_stages[0] if ai_stages else None
    first_keys = len(working(first)) if first else 1
    if first is not None and first_keys == 0:
        raise UserError(f"у первого AI-этапа {first['id']} ({first['model']}) нет ни одного рабочего API",
                        "пополните баланс или добавьте ключ (secrets\\providers\\<провайдер>\\<ключ>.txt), затем запустите prepare снова")
    if template.get("batches"):
        # Шаблон задаёт число пачек жёстко (template-other.json): ключи первого этапа делятся между пачками.
        count = max(1, min(int(template["batches"]), len(folders)))
    else:
        count = max(1, min(int(template.get("max_batches", 4)), len(folders), first_keys))
    size, extra = divmod(len(folders), count)
    configs, start = [], 0
    for index in range(count):
        part = folders[start:start + size + (1 if index < extra else 0)]
        start += len(part)
        stages = []
        for stage in template["stages"]:
            stage = {key: value for key, value in stage.items()}
            original_api = list(stage.get("api", []))
            if "api" in stage:
                alive = working(stage)
                if not alive:
                    stage["enabled"] = False
                    stage["//"] = "выключен prepare: ни одного рабочего API"
                    alive = stage["api"][:1]
                else:
                    shift = index % len(alive)
                    alive = alive[shift:] + alive[:shift]
                # Нерабочие сейчас API (нет баланса, сбой) не выбрасываются, а идут в конец списка: конвейер сам
                # проверит их после паузы (кнопка «↻» — сразу), и пополненный провайдер подхватится без prepare.
                stage["api"] = alive + [item for item in original_api if item not in alive]
            if stage.get("fallback"):
                # Запасная модель: сначала рабочие API, нерабочие — в конце (вдруг пополнят).
                fallback = dict(stage["fallback"])
                spare = [item for item in fallback.get("api", [])
                         if probes[(route_of(item)[0], route_of(item)[1] or fallback["model"], stage["type"] == "ai_image")].ok]
                # Нерабочие — с явной моделью: если ниже модель запасной сменится на модель перекрёстного маршрута,
                # их собственная модель не должна смениться вместе с ней.
                dead = [item if not isinstance(item, str) else {"api": item, "model": fallback["model"]}
                        for item in fallback.get("api", []) if item not in spare]
                if not spare:
                    fallback["api"] = dead
                    stage["fallback"] = fallback
                if spare:
                    # Сдвигаются только маршруты самой запасной модели; перекрёстные (другой провайдер со своей
                    # моделью — на случай, если лёг весь провайдер) всегда в конце.
                    own = [item for item in spare if route_of(item)[1] in (None, fallback["model"])]
                    cross = [item for item in spare if item not in own]
                    shift = index % len(own) if own else 0
                    fallback["api"] = own[shift:] + own[:shift] + cross + dead
                    if not own:
                        # Сама запасная модель нигде не работает — остались перекрёстные маршруты со своей моделью:
                        # в таблице этап должен называться по ней, а не по неработающей.
                        fallback["model"] = route_of(cross[0])[1]
                        fallback["table"] = table_name(fallback["model"])
                    stage["fallback"] = fallback
                    if stage.get("enabled") is False:
                        # Основная модель сейчас нигде не работает, а запасная работает: этап не выключается,
                        # а через минуту переходит на запасную (в таблице — её название). Основные API остаются
                        # в списке: пополните баланс — со следующего запуска этап снова начнёт с основной модели.
                        stage.pop("enabled")
                        stage["api"] = original_api
                        fallback["after"] = 1
                        stage["//"] = "основная модель сейчас без рабочих API — через минуту этап переходит на запасную"
            stages.append(stage)
        if template.get("rotate_ai"):
            stages = rotate_ai(stages, index)
        configs.append({
            "//": f"Сгенерировано prepare.bat из template.json (пачка {index + 1} из {count}) — правьте template.json, этот файл перезапишется",
            "name": f"batch{index + 1}",
            "folders": part,
            "stages": stages,
        })
        if template.get("previous"):
            # "previous": "keep" — если этап выключен (нет рабочих API) или убран, его прежние определения
            # у начатых наборов остаются, а не уходят на повторную обработку.
            configs[-1]["previous"] = template["previous"]
    return configs


def load_template(name: str) -> dict:
    try:
        return json.loads((CONFIGS / f"{name}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UserError(f"configs\\{name}.json не прочитан: {exc}", "проверьте файл (образец — в README)")


def write_configs(configs: list[dict]) -> None:
    for path in CONFIGS.glob("config*.json"):
        if re.fullmatch(r"config\d+\.json", path.name):
            path.unlink()
    for index, config in enumerate(configs, 1):
        text = json.dumps(config, ensure_ascii=False, indent=2)
        (CONFIGS / f"config{index}.json").write_text(text + "\n", encoding="utf-8")
    main = CONFIGS / "config.json"
    text = main.read_text(encoding="utf-8")
    run = "[\n" + ",\n".join(f'    "config{i}"' for i in range(1, len(configs) + 1)) + "\n  ]"
    text, replaced = re.subn(r'"run":\s*\[[^\]]*\]', '"run": ' + run, text, count=1)
    if not replaced:
        raise UserError('в configs\\config.json нет параметра "run"', 'добавьте строку "run": ["config1"]')
    main.write_text(text, encoding="utf-8")


def main() -> int:
    from engine.config import Report, load_settings

    import probe

    console.title("Подготовка запуска anime-sort")
    # Агент конфигов (tools\agent\config_agent.py): опрашивает все ключи и модели и пересобирает шаблоны
    # template*.json под то, что сейчас работает. Платный (OpenRouter, ключ агента) — поэтому только по вопросу.
    if "--no-agent" not in sys.argv and console.ask(
            "Пересобрать шаблоны агентом конфигов (опрос всех API + OpenRouter, платно)?", "дн") == "д":
        sys.path.insert(0, str(PROJECT / "tools" / "agent"))
        import config_agent

        config_agent.main(from_prepare=True)
    report = Report()
    settings = load_settings(report, check_run_files=False)
    if settings is None:
        raise UserError("configs\\config.json с ошибками: " + "; ".join(e.splitlines()[0] for e in report.errors[:3]),
                        "исправьте config.json (подробно: venv\\Scripts\\python engine\\main.py --check)")

    console.step("Ищу папки, которые ещё не обработаны…")
    folders = pending_folders(settings.source, settings.results)
    if not folders:
        console.ok("Всё обработано — новых папок нет. Новые картинки: anime-vault → download.bat, distribute.bat.")
        return 0
    console.info(f"  к обработке: {len(folders)} — " + ", ".join(folders))
    # Обычные dataN — по template.json, «Other» на второй круг (dataN-other) — по template-other.json.
    groups = [(name, [f for f in folders if f.endswith("-other") == (name == "template-other")])
              for name in ("template", "template-other")]
    groups = [(load_template(name), part) for name, part in groups if part]

    console.step("Проверяю API (по одному маленькому запросу на ключ и модель)…")
    probes = probe.probe_all(list(dict.fromkeys(pair for template, _ in groups for pair in stage_pairs(template))))
    show_probes(probes)

    configs = [config for template, part in groups for config in build_configs(template, probes, part)]
    for index, config in enumerate(configs, 1):
        config["name"] = f"batch{index}"
        config["//"] = config["//"].replace("(пачка ", f"(config{index}; пачка ", 1)
    console.title("Пачки")
    for index, config in enumerate(configs, 1):
        first = next((s for s in config["stages"] if s.get("type") == "ai_image" and s.get("enabled", True)), None)
        lead = route_of(first["api"][0])[0] if first else "—"
        console.say(f"  config{index}: {len(config['folders'])} папок ({config['folders'][0]}…{config['folders'][-1]}), первый ключ {lead}")
        for stage in config["stages"]:
            if stage.get("enabled") is False:
                console.warn(f"    этап {stage['id']} выключен: ни одного рабочего API")
    write_configs(configs)
    console.ok(f"Записаны config1…config{len(configs)}.json и run в config.json.")

    check = subprocess.run([sys.executable, str(PROJECT / "engine" / "main.py"), "--check"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    if check.returncode != 0:
        console.error("проверка конфигов нашла ошибки:", "исправьте template.json и запустите prepare снова")
        console.say(check.stdout[-3000:], console.RED)
        return 1
    console.ok("Проверка конфигов: ошибок нет.")
    if console.ask("Запустить конвейеры?", "дн") == "д":
        subprocess.Popen(["explorer.exe", str(PROJECT / "start.vbs")])
        console.ok("Запущено — откроется главное окно anime-sort.")
    return 0


if __name__ == "__main__":
    raise SystemExit(console.guarded(main))
