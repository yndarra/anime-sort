r"""names.json — правила переименования папок Waifu («Источник» → «Цель»), и порядок в нём.

Формат (обычный JSON, обратный слэш между тайтлом и персонажем пишется двойным — так требует JSON):
    {
      "//": "пояснение",
      "new": {"Kimi No Na Wa": "Your Name"},                 ← сюда вписывать новые правила вручную
      "titles": {
        "Other": {"Original": "Other", ...},                 ← блок тайтла: все правила, чья ЦЕЛЬ — этот тайтл
        "Your Name": {"Kimi No Na Wa": "Your Name",
                      "Your Name\\Miyamizu Mitsuha": "Your Name\\Mitsuha Miyamizu"}
      }
    }
Блок — все правила, чья ЦЕЛЬ относится к одному тайтлу; внутри блока сначала правила тайтлов, потом персонажей
(«Тайтл\Персонаж»). Первым идёт блок «Other», дальше по алфавиту. Пустая цель ("") — правило выключено.
Упорядочивание переносит правила из "new" в блоки и убирает точные повторы.

    python tools\fix_name\regroup.py                 — упорядочить names.json на месте (копия — names.json.bak)
    from regroup import add_rules; add_rules(path, [("Источник", "Цель"), ...])  — добавить правила и упорядочить

До 04.10 правила лежали в names.txt (строки "Источник" - "Цель"); parse() читает и его — для перевода
(python tools\fix_name\regroup.py --from-txt).
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

QUOTED = re.compile(r'^\s*"(?P<s>[^"]*)"\s+-\s+"(?P<t>[^"]*)"\s*$')
NAMES = Path(__file__).resolve().with_name("names.json")
OLD_NAMES = NAMES.with_name("names.txt")
HEADER = ("Правила переименования папок Waifu: \"Источник\": \"Цель\". Тайтл — \"Тайтл\", персонаж — \"Тайтл\\\\Персонаж\" "
          "(слэш в JSON двойной). Новые правила вписывайте в \"new\" — tools\\fix_name\\regroup.py разложит их по блокам "
          "\"titles\" (блок = целевой тайтл, Other первым, дальше по алфавиту). Пустая цель — правило выключено. "
          "Применить к Waifu — tools\\fix_name\\fix_name.py (его же вызывает finish.bat)")


def key(text: str) -> str:
    return " ".join(text.split()).casefold()


def parse_txt(path: Path) -> tuple[list[tuple[str, str]], list[str]]:
    """Старый names.txt: строки "Источник" - "Цель" (слэш между тайтлом и персонажем — двойной)."""
    rules, other = [], []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = QUOTED.match(stripped)
        if not match:
            other.append(stripped)
            continue
        source, target = match.group("s").replace("\\\\", "\\"), match.group("t").replace("\\\\", "\\")
        if source.strip() or target.strip():
            rules.append((source, target))
    return rules, other


def parse(path: Path) -> tuple[list[tuple[str, str]], list[str]]:
    """(правила, нераспознанное). В правилах слэш между тайтлом и персонажем — одинарный."""
    if path.suffix.lower() == ".txt":
        return parse_txt(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    rules = []
    for block in [data.get("new") or {}, *(data.get("titles") or {}).values()]:
        for source, target in block.items():
            if source.strip() or str(target).strip():
                rules.append((source, str(target)))
    return rules, []


def title_of(value: str) -> str:
    return value.split("\\")[0]


def render(rules: list[tuple[str, str]], other: list[str] | None = None) -> str:
    """Правила → текст names.json (блоки по целевому тайтлу, Other первым; точные повторы убраны)."""
    seen, unique = set(), []
    for source, target in rules:
        pair = (key(source), key(target))
        if pair not in seen:
            seen.add(pair)
            unique.append((source, target))
    groups: dict[str, tuple[str, list]] = {}
    for source, target in unique:
        name = title_of(target) if target.strip() else title_of(source)
        groups.setdefault(key(name), (name, []))[1].append((source, target))
    titles: dict[str, dict[str, str]] = {}
    for _, (name, group) in sorted(groups.items(), key=lambda item: (item[0] != "other", item[0])):
        block = titles.setdefault(name, {})
        for source, target in [rule for rule in group if "\\" not in rule[0]] + [rule for rule in group if "\\" in rule[0]]:
            block.setdefault(source, target)   # один источник в блоке — один раз (первое правило главнее)
    return json.dumps({"//": HEADER, "new": {}, "titles": titles}, ensure_ascii=False, indent=2) + "\n"


def add_rules(path: Path, new_rules: list[tuple[str, str]]) -> int:
    """Добавить правила (с копией names.json.bak) и упорядочить. → сколько правил стало."""
    rules, other = parse(path)
    shutil.copy2(path, path.with_suffix(".json.bak"))
    path.write_text(render(rules + list(new_rules), other), encoding="utf-8")
    return len(parse(path)[0])


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    if "--from-txt" in sys.argv:
        rules, other = parse_txt(OLD_NAMES)
        NAMES.write_text(render(rules), encoding="utf-8")
        print(f"names.txt → names.json: правил {len(parse(NAMES)[0])} (из {len(rules)}), нераспознанных строк {len(other)}")
    else:
        print(f"правил: {add_rules(NAMES, [])} (копия — names.json.bak)")
