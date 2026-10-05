r"""Агент имён: находит в Waifu папки-дубли и названия не по правилам и предлагает правила для names.json.

Что считается проблемой (так же, как при ручном разборе):
    - один тайтл под разными именами: ромадзи и английское, с подзаголовком сезона / в скобках, другая
      кириллица/иероглифы, «(series)», опечатки и т. п. → одно ОФИЦИАЛЬНОЕ английское название;
    - персонаж под разными написаниями внутри тайтла: порядок «имя фамилия» / «фамилия имя», полное имя и
      короткое, романизация (Lee Bora / Lee Bo-ra), пометки в скобках → одно имя по принятой в тайтле форме.
Правила самого пользователя важнее: существующие цели names.json берутся как образец (Oshi no Ko, Demon Slayer и
My Dress-Up Darling — японский порядок имён, Blue Archive — только имя и т. д.). Сомнительное — не трогать.

Работает только через OpenRouter со своим ключом (configs\agent.json, secrets\agent\openrouter.txt).
Ход работы: дерево Waifu → запрос по тайтлам → запросы по персонажам (частями) → проверка ответов (источник
есть в Waifu, нет цепочек и противоречий с names.json) → список правил в окне → «Добавить?» →
names.json (tools\fix_name\regroup.py) → по желанию сразу применить к Waifu (fix_name).

python tools\agent\names_agent.py            — разбор и вопросы в окне
python tools\agent\names_agent.py --dry-run  — только показать предложения, ничего не писать
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
TOOLS = PROJECT / "tools"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(TOOLS / "fix_name"))

import console  # noqa: E402
from console import UserError  # noqa: E402
from regroup import NAMES, add_rules, parse  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from openrouter import Agent  # noqa: E402

NUMBER = re.compile(r"^\d+\.\s+")
MEDIA = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".avif"}
SKIP = {"Other", "0. dev"}

CONVENTIONS = """Naming conventions of this collection (follow them strictly):
- Titles use the OFFICIAL English title (e.g. "Kimi No Na Wa" -> "Your Name", "Kanojo, Okarishimasu" -> "Rent-A-Girlfriend").
  Never keep both variants. Season/arc/movie subtitles merge into the main title ("High School DxD BorN" -> "High School DxD").
- Folder names cannot contain : ? / \\ * " < > | — they are written with spaces or removed (look at existing names).
- If a title already exists in the collection under a good name, map duplicates TO that existing name.
- Characters: one form per title, the form already used by most folders of that title. Western order "Given Family"
  by default; exceptions where the collection keeps Japanese order: Demon Slayer, My Dress-Up Darling, Oshi no Ko.
  Blue Archive characters: given name only ("Sunaookami Shiroko" -> "Shiroko").
- Remove parenthesised aliases: "Bishamonten (Bishamon)" -> "Bishamon"; standard romanization ("Lee Bora" -> "Lee Bo-ra").
- When unsure — do NOT propose a rule. Fewer correct rules are better than many doubtful ones."""


def strip(name: str) -> str:
    return NUMBER.sub("", name)


def waifu_tree(waifu: Path) -> dict[str, dict[str, int]]:
    """{тайтл: {персонаж: файлов}} без номеров папок; файлы прямо в папке тайтла — персонаж ""."""
    tree: dict[str, dict[str, int]] = {}
    for title_dir in waifu.iterdir():
        if not title_dir.is_dir() or title_dir.name.startswith(".") or strip(title_dir.name) in SKIP:
            continue
        characters = tree.setdefault(strip(title_dir.name), {})
        for path in title_dir.iterdir():
            if path.is_dir():
                count = sum(1 for f in path.rglob("*") if f.suffix.lower() in MEDIA)
                characters[strip(path.name)] = characters.get(strip(path.name), 0) + count
            elif path.suffix.lower() in MEDIA:
                characters[""] = characters.get("", 0) + 1
    return tree


def title_rules(agent: Agent, tree: dict, known_targets: list[str]) -> list[tuple[str, str]]:
    titles = sorted(tree)
    user = ("Collection title folders (with file counts):\n" + "\n".join(f"{t} ({sum(tree[t].values())})" for t in titles)
            + "\n\nTitles already chosen by the owner as canonical (prefer them as targets):\n" + "\n".join(known_targets[:400])
            + '\n\nReturn ONLY JSON: {"rules": [{"source": "<existing folder title>", "target": "<official English title>"}]} '
              "— only for titles that are duplicates of another title or are not the official English name.")
    answer = agent.ask(CONVENTIONS, user)
    return [(str(r.get("source", "")), str(r.get("target", ""))) for r in answer.get("rules", []) if isinstance(r, dict)]


def character_rules(agent: Agent, tree: dict, chunk: int) -> list[tuple[str, str]]:
    titles = [t for t in sorted(tree) if len([c for c in tree[t] if c]) >= 2]
    rules = []
    for start in range(0, len(titles), chunk):
        part = titles[start:start + chunk]
        console.info(f"  персонажи: тайтлы {start + 1}–{start + len(part)} из {len(titles)}")
        user = ("Character folders per title (with file counts):\n"
                + "\n".join(f"{t}: " + " | ".join(f"{c} ({n})" for c, n in sorted(tree[t].items()) if c) for t in part)
                + '\n\nReturn ONLY JSON: {"rules": [{"title": "<title>", "source": "<existing character>", '
                  '"target": "<one canonical name>"}]} — only for characters that are the same person written differently '
                  "inside one title (name order, alias in brackets, romanization, full vs short name).")
        answer = agent.ask(CONVENTIONS, user)
        for r in answer.get("rules", []):
            if isinstance(r, dict) and r.get("title") and r.get("source") and r.get("target"):
                rules.append((f"{r['title']}\\{r['source']}", f"{r['title']}\\{r['target']}"))
    return rules


def validate(rules: list[tuple[str, str]], tree: dict, existing: list[tuple[str, str]], limit: int) -> list[tuple[str, str]]:
    """Оставить только безопасные правила: источник есть в Waifu, цель другая, нет цепочек и повторов names.json."""
    old_sources = {source.casefold() for source, _ in existing}
    good, sources = [], set()
    for source, target in rules:
        source, target = source.strip(), target.strip()
        if not source or not target or source == target or source.casefold() in old_sources or source in sources:
            continue
        parts = source.split("\\")
        if parts[0] not in tree or (len(parts) == 2 and parts[1] not in tree[parts[0]]) or len(parts) > 2:
            continue
        if any(ch in target for ch in ':?/*"<>|'):
            continue
        sources.add(source)
        good.append((source, target))
    targets = {target for _, target in good}
    good = [(s, t) for s, t in good if s not in targets]           # без цепочек A→B, B→C
    return good[:limit]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--yes", action="store_true", help="не спрашивать (для finish.bat с подтверждением раньше)")
    args = parser.parse_args()
    from waifu_common import WAIFU_ROOT

    config = json.loads((PROJECT / "configs" / "agent.json").read_text(encoding="utf-8"))
    agent = Agent(config)
    console.title(f"Агент имён (OpenRouter, {agent.model})")
    console.step(f"Читаю Waifu: {WAIFU_ROOT}")
    tree = waifu_tree(WAIFU_ROOT)
    existing, _ = parse(NAMES)
    console.info(f"  тайтлов {len(tree)}, персонажей {sum(len(v) for v in tree.values())}, правил в names.json {len(existing)}")
    known_targets = sorted({target.split("\\")[0] for _, target in existing if target and "\\" not in target})

    console.step("Спрашиваю про тайтлы…")
    proposals = title_rules(agent, tree, known_targets)
    console.step("Спрашиваю про персонажей…")
    proposals += character_rules(agent, tree, int(config.get("names_chunk", 120)))
    rules = validate(proposals, tree, existing, int(config.get("names_max_rules", 400)))
    console.info(f"  предложено {len(proposals)}, после проверки {len(rules)}; потрачено ~${agent.spent:.3f}")
    if not rules:
        console.ok("Новых правил нет — Waifu в порядке.")
        return 0
    console.title("Предложенные правила")
    for source, target in rules:
        console.say(f'  "{source}" → "{target}"')
    if args.dry_run:
        console.warn("--dry-run: ничего не записано.")
        return 0
    if not args.yes and console.ask(f"Добавить эти {len(rules)} правил в names.json?", "дн") != "д":
        console.warn("Отменено — names.json не тронут.")
        return 0
    total = add_rules(NAMES, rules)
    console.ok(f"names.json: добавлено {len(rules)}, всего правил {total} (копия — names.json.bak)")
    if args.yes or console.ask("Применить к Waifu сейчас (fix_name)?", "дн") == "д":
        result = subprocess.run([sys.executable, str(TOOLS / "fix_name" / "fix_name.py"), "--no-pause"])
        if result.returncode != 0:
            raise UserError("fix_name завершился с ошибкой (текст выше)", "исправьте и запустите tools\\fix_name\\fix_name.py вручную")
        console.ok("Правила применены к Waifu.")
    return 0


if __name__ == "__main__":
    raise SystemExit(console.guarded(main))
