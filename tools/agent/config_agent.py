r"""Агент конфигов: сам проверяет, какие API и модели сейчас работают (и сколько денег где осталось), и
пересобирает шаблоны пачек configs\template.json (обычные dataN) и configs\template-other.json
(«Other» на второй круг) так, чтобы одновременно идущие пачки меньше пересекались, AI-этапы не повторяли
одну и ту же модель, у каждого этапа были запасные провайдеры и запасная модель и т. д. (правила — RULES ниже,
собраны из пожеланий пользователя). Дальше prepare.bat как обычно делает из шаблонов config1…configN.

Как работает:
    1. Опрос: каждый ключ secrets\providers\<провайдер>\<ключ>.txt × каждая модель его provider.json —
       маленький запрос (tools\probe.py; картинкой, если провайдер принимает картинки, плюс текстом для дешёвых
       моделей этапа JSON). Ключ без баланса / без файла отсекается по первому же ответу — остальные модели
       этого ключа не спрашиваются. У OpenRouter — остаток в долларах.
    2. Модель-агент (OpenRouter, СВОЙ ключ — configs\agent.json → secrets\agent\openrouter.txt; ключи конвейеров
       агент только проверяет, но не тратит на себя) получает сводку, правила и текущие шаблоны и возвращает
       новые шаблоны.
    3. Проверка: из шаблонов собираются пачки тем же кодом, что у prepare (рабочие API, поворот ключей и
       порядка AI-этапов), и проверяются как настоящие конфиги (engine\config.py) + правила агента
       (не меньше 5 AI-этапов у Other, разные модели, запасной провайдер у каждого этапа …). Ошибки — обратно
       агенту, до двух исправлений.
    4. Итог в окне и вопрос «Записать?» (прежние шаблоны — template*.json.bak), затем — «Собрать пачки и
       запустить (prepare)?».

    python tools\agent\config_agent.py              всё по шагам с вопросами
    python tools\agent\config_agent.py --survey     только опрос API (без агента и без денег на OpenRouter)
    python tools\agent\config_agent.py --yes        без вопросов (кроме запуска конвейеров)
Из prepare.bat агент предлагается первым вопросом (по умолчанию — нет: он платный).
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(2, str(PROJECT))

import console  # noqa: E402
import prepare  # noqa: E402
import probe  # noqa: E402
from console import UserError  # noqa: E402
from openrouter import Agent, credits  # noqa: E402

CONFIGS = PROJECT / "configs"
CHECK_DIR = PROJECT / "logs" / "agent-check"
TEMPLATES = ("template", "template-other")
CHEAP_TEXT = ("luna", "flash", "mini", "haiku", "lite")   # дешёвые модели — кандидаты в этап JSON (текст)
DEAD = {"no_balance", "no_key"}

RULES = """You design stage templates for anime-sort, a Windows pipeline that sorts anime pictures into
Title/Character folders. Pictures go through stages in order; every stage only sees files that the stages above
did NOT determine. Local taggers (wd14, camie) are free; ai_image stages send the picture to a vision model through
an API route "provider/key"; json_meta sends only the pin text; neighbors* stages copy the title of neighbouring files.

You return two templates (same JSON format as the current ones you are given):
- "template"       — ordinary batches dataN (fresh pictures). Cheap models first; expensive models only on what the
                     cheap ones left ("from" = minimum confidence of an earlier stage, "where" = "[ID] and [ID]" means
                     the file was checked by those stages and they did not determine it). "max_batches": 4.
- "template-other" — second round of hard pictures (dataN-other). At least 5 ai_image stages with 5 DIFFERENT models,
                     NO "from"/"where" on any stage, "rotate_ai": true, "batches": 4.

Rules (from the owner, all important):
1. Batches run SIMULTANEOUSLY (4 at once). They must overlap as little as possible: in template-other prepare rotates
   the order of ai_image stages per batch ("rotate_ai": true), so INTERLEAVE providers in stage order
   (e.g. beniclo model, ritttta model, beniclo model, …) — then at any moment batches sit on different models
   AND different providers. prepare also rotates which key goes first in every "api" list per batch.
2. No AI model repeats as a main model of two ai_image stages in one template. Use variety: GPT-6, GPT-5.6, Claude Sonnet 5,
   Claude Opus 5 / 5.5, Claude Fable 5.1, Gemini Flash / Pro, Kimi, Grok … — whatever WORKS in the survey.
3. Use ONLY (route, model) pairs that worked in the survey ("ok"); for images — the pair must be ok with images.
   Every stage lists several routes. beniclo is preferred (large balance, rarely runs out) — but do not put
   every stage on beniclo only: each ai_image stage must reach a DIFFERENT provider at least once (in "api" or in its
   "fallback").
4. Every ai_image stage has "fallback": {"model": <other model>, "after": 20, "table": "<table name, <=12 chars, like
   AI Sonnet 5>", "api": [...]}: first routes of the SAME provider with the fallback model (one model hung), then at
   least one route of ANOTHER provider written as {"api": "provider/key", "model": "<model of that route>"}
   (the whole provider is down).
5. json_meta stage "JSON": cheap text models (luna/flash class) across DIFFERENT providers, MORE than 3 routes; a route
   with another model is written as {"api": "provider/key", "model": "..."}.
6. accept thresholds: wd14/camie 0.75; gemini-3.8-flash 0.80; other ai_image 0.50–0.60 (keep current values for the
   same models); json_meta 0.50. Neighbour stages have no accept.
7. Keep stage order skeleton: WD-14, Camie, ai_image stages, JSON, then neighbours ("N-L&R" neighbors_title and
   "N-LR" neighbors_lr; in "template" N-LR has "from": 0.10).
8. Stage "id": use ids from the provided names table when the model matches (AI-GF Gemini Flash, AI-K3 Kimi K3,
   AI-GP Gemini Pro, AI-F5 Fable 5.1, AI-G6 GPT-6, AI-G5 GPT-5.6, AI-S5 Sonnet 5, AI-O5 Opus 5, AI-O55 Opus 5.5,
   AI-GR Grok); a new id is <= 6 characters like "AI-XX".
9. Money: expensive models (Opus, Fable, GPT-6) are fine (the owner likes GPT-6), but on routes/providers with a low
   balance prefer later positions. Never use the agent's own OpenRouter key (you never see it); "openrouter/key" is an
   ordinary pipeline route and may be used only if it worked in the survey.
10. Explain decisions with "//" keys in Russian inside the templates (short), as in the current files.

Return ONLY JSON: {"template": {...}, "template-other": {...}, "notes": ["short Russian lines: what you changed and why"]}"""


# ---------------------------------------------------------------- опрос API

def routes() -> list[tuple[str, object]]:
    """Все «провайдер/ключ» с файлом ключа и provider.json → [(route, Provider)]."""
    from engine.config import KEYS, Report, load_provider

    found = []
    for folder in sorted(p for p in KEYS.iterdir() if p.is_dir()) if KEYS.is_dir() else []:
        provider = load_provider(folder.name, Report(), "agent")
        if provider is None:
            continue
        for key in sorted(folder.glob("*.txt")):
            if key.name.casefold() != "readme.txt":
                found.append((f"{folder.name}/{key.stem}", provider))
    return found


def cheap(model: str) -> bool:
    """Дешёвая модель (кандидат в JSON): слово из CHEAP_TEXT — отдельной частью имени («gemini» ≠ «mini»)."""
    return any(part in CHEAP_TEXT for part in re.split(r"[-_.\s]+", model.casefold()))


def survey_route(route: str, provider) -> list[probe.Probe]:
    """Все модели одного ключа; ключ без баланса/ключа отсекается по первому ответу."""
    results = []
    pairs = [(model, provider.images) for model in provider.models]
    pairs += [(model, False) for model in provider.models if provider.images and cheap(model)]
    for index, (model, images) in enumerate(pairs):
        result = probe.probe(route, model, images)
        results.append(result)
        if index == 0 and (result.status in DEAD or "HTTP 401" in result.detail or "HTTP 403" in result.detail):
            for other_model, other_images in pairs[1:]:
                results.append(probe.Probe(route, other_model, other_images, result.status, "ключ отсечён по первой модели"))
            break
    return results


def survey(route_list) -> dict:
    with ThreadPoolExecutor(8) as pool:
        groups = list(pool.map(lambda pair: survey_route(*pair), route_list))
    return {(p.route, p.model, p.images): p for group in groups for p in group}


class Probes(dict):
    """Пары, которых нет в опросе, — нерабочие (prepare.build_configs спрашивает любую пару из шаблона)."""

    def __missing__(self, pair):
        return probe.Probe(pair[0], pair[1], pair[2], "error", "не проверялся")


def summary(probes: dict, route_list) -> dict:
    image, text, dead = {}, {}, {}
    for (route, model, images), result in sorted(probes.items()):
        if result.ok:
            (image if images else text).setdefault(model, []).append({"route": route, "seconds": round(result.seconds, 1)})
        else:
            dead.setdefault(route, {}).setdefault(result.status, []).append(model)
    balances = {}
    for route, provider in route_list:
        if "openrouter.ai" in provider.endpoint:
            value = credits(probe.read_key(route))
            if value is not None:
                balances[route] = round(value, 2)
    return {"image_models_ok": image, "text_models_ok": text, "not_working": dead, "balances_usd": balances}


def show_survey(info: dict) -> None:
    console.title("Опрос API")
    for model, items in sorted(info["image_models_ok"].items()):
        console.say(f"  картинка  {model:<20} " + ", ".join(item["route"] for item in items), console.GREEN)
    for model, items in sorted(info["text_models_ok"].items()):
        console.say(f"  текст     {model:<20} " + ", ".join(item["route"] for item in items), console.GREEN)
    for route, statuses in sorted(info["not_working"].items()):
        text = "; ".join(f"{prepare.STATUS_TEXT.get(status, status)}: {len(models)}" for status, models in statuses.items())
        console.say(f"  не работает {route:<16} {text}", console.GREY)
    for route, value in info["balances_usd"].items():
        console.say(f"  баланс {route}: ${value}", console.WHITE)


# ---------------------------------------------------------------- проверка ответа агента

def own_rules(name: str, template: dict, configs: list[dict]) -> list[str]:
    """Правила пользователя, которые engine\\config.py не проверяет."""
    errors = []
    stages = template.get("stages") or []
    ai = [s for s in stages if s.get("type") == "ai_image"]
    models = [s.get("model") for s in ai]
    if len(set(models)) != len(models):
        errors.append(f"{name}: одна модель у нескольких ai_image этапов: {models}")
    if name == "template-other":
        if len(ai) < 5:
            errors.append(f"{name}: ai_image этапов {len(ai)}, нужно не меньше 5")
        if any("from" in s or "where" in s for s in stages if s.get("type") in ("ai_image", "json_meta")):
            errors.append(f"{name}: у этапов второго круга не должно быть from/where (порядок AI-этапов поворачивается)")
        if not template.get("rotate_ai"):
            errors.append(f'{name}: нужно "rotate_ai": true')
    for config in configs:
        for stage in config["stages"]:
            if stage.get("enabled") is False:
                errors.append(f"{name}/{config['name']}: этап {stage['id']} без единого рабочего API")
            if stage.get("type") != "ai_image" or stage.get("enabled") is False:
                continue
            providers = {prepare.route_of(item)[0].split("/")[0] for item in stage.get("api", [])}
            fallback = stage.get("fallback") or {}
            if not fallback:
                errors.append(f"{name}: у этапа {stage['id']} нет запасной модели (fallback) с рабочими API")
                continue
            providers |= {prepare.route_of(item)[0].split("/")[0] for item in fallback.get("api", [])}
            if len(providers) < 2:
                errors.append(f"{name}: этап {stage['id']} целиком на одном провайдере {providers} — нужен запасной провайдер")
        json_stage = next((s for s in config["stages"] if s.get("type") == "json_meta"), None)
        if json_stage and len(json_stage.get("api", [])) <= 3:
            errors.append(f"{name}: у этапа JSON рабочих маршрутов {len(json_stage.get('api', []))}, нужно больше 3")
    return sorted(set(errors))


def check(templates: dict, probes: Probes, settings, folders: dict[str, list[str]]) -> tuple[list[str], dict]:
    """(ошибки, собранные пачки по шаблонам). Пачки пишутся в logs\\agent-check и проверяются как настоящие."""
    from engine.config import check_one

    errors, built = [], {}
    shutil.rmtree(CHECK_DIR, ignore_errors=True)
    CHECK_DIR.mkdir(parents=True)
    sample = sorted(p.name for p in settings.source.iterdir() if p.is_dir())[:1]
    for name in TEMPLATES:
        template = templates.get(name)
        if not isinstance(template, dict) or not isinstance(template.get("stages"), list):
            errors.append(f"{name}: нет шаблона или в нём нет списка stages")
            continue
        try:
            configs = prepare.build_configs(template, probes, folders.get(name) or sample)
        except UserError as exc:
            errors.append(f"{name}: {exc}")
            continue
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"{name}: шаблон не собирается в пачки: {type(exc).__name__}: {exc}")
            continue
        built[name] = configs
        accepted = []
        for index, config in enumerate(configs, 1):
            if not folders.get(name):
                config["folders"] = sample   # папок этого вида нет — проверяем только этапы
            file = f"{name}-{index}"
            (CHECK_DIR / f"{file}.json").write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
            spec, report = check_one(file, settings, accepted, CHECK_DIR)
            if spec is not None:
                accepted.append(spec)
            errors += [f"{name}/{config['name']}: {text.splitlines()[0]}" for text in report.errors]
        errors += own_rules(name, template, configs)
    return errors, built


def show_plan(templates: dict, built: dict, notes: list[str]) -> None:
    for name in TEMPLATES:
        configs = built.get(name) or []
        console.title(f"{name}.json — пачек {len(configs)}")
        for config in configs:
            order = [s["id"] for s in config["stages"] if s.get("type") == "ai_image"]
            console.say(f"  {config['name']}: " + " → ".join(order), console.WHITE)
        for stage in (templates.get(name) or {}).get("stages", []):
            if stage.get("type") not in ("ai_image", "json_meta"):
                continue
            fallback = stage.get("fallback") or {}
            spare = f"  запасная: {fallback.get('model')}" if fallback else ""
            console.say(f"    {stage['id']:<7} {stage.get('model', ''):<20} API {len(stage.get('api', []))}{spare}", console.GREY)
    if notes:
        console.title("Пояснения агента")
        for line in notes:
            console.say(f"  {line}", console.WHITE)


# ---------------------------------------------------------------- всё вместе

def main(from_prepare: bool = False) -> int:
    from engine.config import Report, load_settings

    parser = argparse.ArgumentParser()
    parser.add_argument("--survey", action="store_true", help="только опрос API")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args([] if from_prepare else None)

    console.title("Агент конфигов")
    settings = load_settings(Report(), check_run_files=False)
    if settings is None:
        raise UserError("configs\\config.json с ошибками", "проверьте: venv\\Scripts\\python engine\\main.py --check")
    pending = prepare.pending_folders(settings.source, settings.results)
    folders = {"template": [f for f in pending if not f.endswith("-other")],
               "template-other": [f for f in pending if f.endswith("-other")]}
    console.info(f"  ждут обработки: обычных папок {len(folders['template'])}, -other {len(folders['template-other'])}")

    route_list = routes()
    console.step(f"Опрашиваю API: ключей {len(route_list)} (ключ без баланса отсекается по первой модели)…")
    probes = Probes(survey(route_list))
    info = summary(probes, route_list)
    show_survey(info)
    if args.survey:
        return 0

    agent = Agent()
    balance = agent.balance()
    if balance is not None:
        console.info(f"  баланс ключа агента OpenRouter: ${balance:.2f}")
    current = {name: json.loads((CONFIGS / f"{name}.json").read_text(encoding="utf-8")) for name in TEMPLATES
               if (CONFIGS / f"{name}.json").exists()}
    names = {stage_id: table for stage_id, (_, table) in settings.names.items()}
    request = json.dumps({"survey": info, "folders_waiting": {k: len(v) for k, v in folders.items()},
                          "names_table": names, "current_templates": current}, ensure_ascii=False)
    console.step(f"Спрашиваю агента ({agent.model})…")
    answer = agent.ask(RULES, request, max_tokens=16000)
    for attempt in range(3):
        templates = {name: answer.get(name) for name in TEMPLATES}
        errors, built = check(templates, probes, settings, folders)
        if not errors:
            break
        console.warn(f"  проверка нашла {len(errors)} ошибок" + (" — отправляю агенту на исправление" if attempt < 2 else ""))
        for line in errors[:12]:
            console.say(f"    {line}", console.YELLOW)
        if attempt == 2:
            raise UserError("агент так и не собрал шаблоны без ошибок (шаблоны не тронуты)",
                            "запустите снова или поправьте configs\\template*.json вручную")
        answer = agent.ask(RULES, request + "\n\nYour previous answer:\n" + json.dumps(answer, ensure_ascii=False)
                           + "\n\nFix ALL these problems and return the full JSON again:\n" + "\n".join(errors),
                           max_tokens=16000)
    show_plan(templates, built, [str(line) for line in answer.get("notes") or []])
    console.info(f"  агент потратил ${agent.spent:.3f}")
    if not args.yes and console.ask("Записать эти шаблоны (прежние — template*.json.bak)?", "дн") != "д":
        console.warn("Отменено — шаблоны не тронуты.")
        return 0
    for name in TEMPLATES:
        path = CONFIGS / f"{name}.json"
        if path.exists():
            shutil.copy2(path, path.with_suffix(".json.bak"))
        path.write_text(json.dumps(templates[name], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    console.ok("Шаблоны записаны.")
    if from_prepare:
        return 0
    if console.ask("Собрать пачки по новым шаблонам (prepare)?", "дн") == "д":
        subprocess.run([sys.executable, str(PROJECT / "tools" / "prepare.py"), "--no-agent"])
    return 0


if __name__ == "__main__":
    raise SystemExit(console.guarded(main))
