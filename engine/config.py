r"""Чтение и проверка конфигов: configs\config.json, configs\configN.json, providers\<имя>\provider.json.

Каждое сообщение говорит, в каком файле и строке проблема и как её исправить.
check_all() — полная проверка перед запуском (консоль main.py); load_one() — конфиг для работающей пачки
(читается из снимка конфигов в logs\config-snapshots\<время>, который делает main.py при старте).
"""
from __future__ import annotations

import difflib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from engine.jsonconf import Node, load as load_json

PROJECT = Path(__file__).resolve().parent.parent
# ANIME_SORT_CONFIGS — откуда читать конфиги: снимок logs\config-snapshots\<время> (его ставит main.py пачкам)
# или другая папка для проверок на тестовых данных.
CONFIGS = Path(os.environ.get("ANIME_SORT_CONFIGS") or PROJECT / "configs")
LIVE_CONFIGS = PROJECT / "configs"
PROVIDERS = PROJECT / "providers"
# Ключи API — НЕ в репозитории: secrets\providers\<провайдер>\<ключ>.txt (папка secrets\ в .gitignore).
# Ключ агентов (gui/tools, OpenRouter) — отдельно: secrets\agent\openrouter.txt — конвейеры его не видят.
SECRETS = Path(os.environ.get("ANIME_SORT_SECRETS") or PROJECT / "secrets")
KEYS = SECRETS / "providers"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".avif"}

# Типы этапов: даёт ли этап уверенность, нужен ли API, есть ли порог принятия.
STAGE_TYPES = {
    "wd14":            {"confidence": True,  "api": False, "accept": True,  "about": "локальный теггер WD-14"},
    "camie":           {"confidence": True,  "api": False, "accept": True,  "about": "локальный теггер Camie"},
    "ai_image":        {"confidence": True,  "api": True,  "accept": True,  "about": "модель по картинке через API"},
    "json_meta":       {"confidence": True,  "api": True,  "accept": True,  "about": "модель по metadata пина через API"},
    "neighbors":       {"confidence": False, "api": False, "accept": False, "about": "тайтл, если соседи слева и справа — один тайтл и один персонаж"},
    "neighbors_title": {"confidence": False, "api": False, "accept": False, "about": "тайтл, если соседи слева и справа — один тайтл"},
    "neighbors_left":  {"confidence": False, "api": False, "accept": False, "about": "тайтл соседа слева"},
    "neighbors_right": {"confidence": False, "api": False, "accept": False, "about": "тайтл соседа справа"},
    "neighbors_lr":    {"confidence": False, "api": False, "accept": False, "about": "тайтл соседа слева или справа, при разных — левого"},
    "neighbors_rl":    {"confidence": False, "api": False, "accept": False, "about": "тайтл соседа справа или слева, при разных — правого"},
}
RESERVED_IDS = {"PIPELINE", "LINE", "GUI", "SUMMARY", "ALL"}
ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_\-&]{0,19}$")
BAD_PATH_CHARS = set('\\/:*?"<>|')

LOG_COLUMNS = {"time": 21, "stage": 7, "status": 12, "file": 33, "confidence": 4, "title": 50, "character": 50}
TABLE_COLUMNS = {"stage": 12, "determined": 10, "nd": 5, "error": 6, "expected": 8, "remained": 8}
SETTINGS_KEYS = {"run", "collection", "waifu", "source", "results", "names", "log_columns", "table_columns", "monitor",
                 "empty_cells"}
# "empty_cells": пустые поля в логе и таблице окон наборов — "blank" (ничего не писать) или "dash" (прочерки «—»).
EMPTY_CELLS = ("blank", "dash")
CONFIG_KEYS = {"name", "folders", "previous", "stages"}
STAGE_KEYS = {"id", "type", "enabled", "accept", "api", "model", "from", "where", "failover", "fallback"}
# Запасная модель этапа ("fallback": {...}): допустимые параметры.
FALLBACK_KEYS = {"model", "api", "after", "table", "accept"}
PROVIDER_KEYS = {"endpoint", "auth", "reasoning", "interval", "models", "images", "temperature"}
# Переопределение переключения API у этапа ("failover": {...}) -> поле live.json и допустимый диапазон.
FAILOVER_KEYS = {"errors": ("failover_errors", 1, 100), "minutes": ("failover_minutes", 1, 1440),
                 "recheck_minutes": ("failover_recheck_minutes", 1, 1440),
                 "quota_recheck_minutes": ("failover_quota_recheck_minutes", 1, 10080)}
REASONING = {"anthropic", "openrouter", "none"}


# ---------------------------------------------------------------- результаты разбора

@dataclass
class Provider:
    name: str
    folder: Path
    endpoint: str
    auth: str
    reasoning: str
    interval: float
    models: dict[str, str]
    images: bool = True            # false — провайдер не принимает картинки (только текст, для json_meta)
    temperature: bool = True       # false — не отправлять temperature (шлюз отвечает на него ошибкой)


@dataclass
class ApiRoute:
    """Один API этапа: провайдер + файл ключа + модель (имя в конфиге и имя у провайдера)."""
    label: str                     # «beniclo/key1» — так в логе, консоли и таблице
    provider: Provider
    key_path: Path
    model: str
    provider_model: str
    has_key: bool = True           # при проверке файл ключа был на месте и не пустой


@dataclass
class StageSpec:
    id: str
    type: str
    index: int                     # номер этапа в конфиге, с 1 (выключенные этапы не считаются)
    label: str                     # метка в логе и префикс файлов
    table: str                     # название в таблице окна
    accept: float | None
    from_: float | None
    where: tuple | None            # дерево выражения: ("ref", id) / ("and", a, b) / ("or", a, b)
    where_text: str
    apis: list[ApiRoute]           # API по порядку попыток (пусто у локальных этапов)
    model: str | None
    failover: dict[str, int] = field(default_factory=dict)   # переопределения live.json: failover_errors и т.п.
    fallback: "Fallback | None" = None   # запасная модель: все API этапа спят дольше after минут — этап переходит на неё

    @property
    def gives_confidence(self) -> bool:
        return STAGE_TYPES[self.type]["confidence"]


@dataclass
class Fallback:
    """"fallback" этапа: модель и API, на которые этап переходит, если все его API спят дольше after минут
    (модель «зависла» у всех провайдеров). В таблице окна этап тогда называется table."""
    model: str
    apis: list[ApiRoute]
    after: int                     # минут, которые все API этапа должны проспать подряд
    table: str                     # название этапа в таблице после перехода
    accept: float | None           # свой порог принятия (по умолчанию — порог этапа)


@dataclass
class ConfigSpec:
    file: str                      # имя файла без .json (config1)
    name: str                      # "name" (имя пачки)
    folders: list[str]
    stages: list[StageSpec]
    column: int = 0                # колонка окон (порядок в run)
    keep_previous: bool = False    # "previous": "keep" — результаты этапов не из этого конфига сохраняются


@dataclass
class Settings:
    run: list[str]
    waifu: Path
    source: Path
    results: Path
    names: dict[str, tuple[str, str]]
    log_columns: dict[str, int]
    table_columns: dict[str, int]
    free_cores: int = 2             # из configs\live.json (меняется на ходу), см. engine\live.py
    empty_cells: str = "dash"       # "empty_cells": пустые поля в окнах наборов — "blank" (пусто) или "dash" («—»)


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def error(self, where: str, what: str, fix: str) -> None:
        self.errors.append(f"[{where}] {what}\n      Как исправить: {fix}")

    def warn(self, where: str, what: str, fix: str = "") -> None:
        self.warnings.append(f"[{where}] {what}" + (f"\n      Совет: {fix}" if fix else ""))


class Src:
    """Файл конфига для сообщений: «config1.json, строка 12»."""

    def __init__(self, label: str):
        self.label = label

    def at(self, where=None) -> str:
        line = where.line if isinstance(where, Node) else where
        return f"{self.label}, строка {line}" if line else self.label


def suggest(word: str, options) -> str:
    close = difflib.get_close_matches(str(word).casefold(), [str(o).casefold() for o in options], n=1, cutoff=0.6)
    if not close:
        return ""
    original = next(o for o in options if str(o).casefold() == close[0])
    return f" Возможно, вы имели в виду «{original}»?"


def show(node: Node) -> str:
    """Значение узла так, как оно записано (для сообщений)."""
    if node.kind == "string":
        return f'"{node.value}"'
    if node.kind == "number":
        return f"{node.value:g}"
    if node.kind == "bool":
        return "true" if node.value else "false"
    if node.kind == "null":
        return "null"
    return "[...]" if node.kind == "array" else "{...}"


def text_of(node: Node | None) -> str | None:
    return node.value.strip() if node is not None and node.kind == "string" and node.value.strip() else None


def need_text(node: Node, report: Report, src: Src, what: str, example: str) -> str | None:
    value = text_of(node)
    if value is None:
        if node.kind == "string":
            report.error(src.at(node), f"{what}: пустая строка", f"впишите значение: {example}")
        else:
            report.error(src.at(node), f"{what}: {show(node)} — нужен текст в двойных кавычках", example)
    return value


def number_of(node: Node, report: Report, src: Src, what: str, key: str, low: float, high: float,
              low_inclusive: bool = True, high_inclusive: bool = True) -> float | None:
    if node.kind != "number":
        raw = str(node.value) if node.kind == "string" else show(node)
        if node.kind == "string" and re.fullmatch(r"\s*[+-]?(\d+(\.\d*)?|\.\d+)\s*", raw):
            report.error(src.at(node), f"{what}: число {show(node)} записано в кавычках", f'уберите кавычки: "{key}": {raw.strip()}')
        elif node.kind == "string" and re.fullmatch(r"\s*\d+,\d+\s*", raw):
            report.error(src.at(node), f"{what}: «{raw}» — дробная часть через запятую", f'"{key}": {raw.strip().replace(",", ".")}')
        elif node.kind == "null":
            report.error(src.at(node), f"{what}: значение не указано (null)", f'впишите число, например "{key}": 0.5')
        else:
            report.error(src.at(node), f"{what}: {show(node)} — не число", f'впишите число через точку без кавычек, например "{key}": 0.55')
        return None
    number = float(node.value)
    too_low = number < low if low_inclusive else number <= low
    too_high = number > high if high_inclusive else number >= high
    if too_low or too_high:
        left = "[" if low_inclusive else "("
        right = "]" if high_inclusive else ")"
        report.error(src.at(node), f"{what} = {number:g} вне допустимого диапазона {left}{low:g}; {high:g}{right}",
                     f"укажите число от {low:g} до {high:g}")
        return None
    return number


def check_keys(obj: Node, allowed: set, report: Report, src: Src, context: str, hint: str = "") -> None:
    for key in obj.value:
        if key not in allowed:
            report.error(src.at(obj.keys.get(key)), f"{context}: неизвестный параметр «{key}»" + suggest(key, allowed),
                         hint or "допустимы: " + ", ".join(sorted(allowed)))


def need_object(node: Node | None, report: Report, src: Src, what: str, example: str) -> Node | None:
    if node is None:
        return None
    if node.kind != "object":
        report.error(src.at(node), f"{what}: {show(node)} — нужен объект в фигурных скобках", example)
        return None
    return node


def open_config(path: Path, report: Report, label: str) -> Node | None:
    root, problems = load_json(path)
    for line, what, fix in problems:
        report.error(Src(label).at(line), what, fix)
    return root


# ---------------------------------------------------------------- where: разбор выражения

TOKEN_RE = re.compile(r"\s*(\[[^\]]*\]|\(|\)|[^\s\[\]()]+)")


class WhereError(ValueError):
    pass


def parse_where(text: str) -> tuple:
    tokens = [match.group(1) for match in TOKEN_RE.finditer(text) if match.group(1).strip()]
    rest = TOKEN_RE.sub("", text).strip()
    if rest:
        raise WhereError(f"непонятные символы «{rest}»")
    if not tokens:
        raise WhereError("выражение пустое")
    normalized = []
    for token in tokens:
        lowered = token.casefold()
        if token.startswith("["):
            inner = token[1:-1].strip()
            if not inner:
                raise WhereError("пустые скобки []")
            normalized.append(("ref", inner))
        elif lowered in ("and", "и", "&&", "&"):
            normalized.append(("op", "and"))
        elif lowered in ("or", "или", "||", "|"):
            normalized.append(("op", "or"))
        elif token in "()":
            normalized.append(("paren", token))
        elif lowered in ("not", "не", "!"):
            raise WhereError("отрицание (not) не поддерживается")
        else:
            raise WhereError(f"«{token}» — не этап в квадратных скобках и не and/or (этапы пишутся так: [AI-K3])")
    position = 0

    def peek():
        return normalized[position] if position < len(normalized) else None

    def take():
        nonlocal position
        token = peek()
        position += 1
        return token

    def expr():
        node = term()
        while peek() == ("op", "or"):
            take()
            node = ("or", node, term())
        return node

    def term():
        node = factor()
        while peek() == ("op", "and"):
            take()
            node = ("and", node, factor())
        return node

    def factor():
        token = take()
        if token is None:
            raise WhereError("выражение обрывается — после and/or нужен этап")
        if token[0] == "ref":
            return token
        if token == ("paren", "("):
            node = expr()
            if take() != ("paren", ")"):
                raise WhereError("не закрыта скобка (")
            return node
        if token[0] == "op":
            raise WhereError(f"«{token[1]}» стоит там, где ожидается этап [ИМЯ]")
        raise WhereError("лишняя закрывающая скобка )")

    tree = expr()
    if position != len(normalized):
        token = normalized[position]
        if token[0] == "ref":
            raise WhereError(f"между этапами нет and/or (перед [{token[1]}])")
        raise WhereError("лишняя закрывающая скобка )" if token == ("paren", ")") else "лишние символы в конце выражения")
    return tree


def where_refs(tree) -> list[str]:
    if tree is None:
        return []
    if tree[0] == "ref":
        return [tree[1]]
    return where_refs(tree[1]) + where_refs(tree[2])


def where_to_text(tree) -> str:
    if tree[0] == "ref":
        return f"[{tree[1]}]"
    left, right = where_to_text(tree[1]), where_to_text(tree[2])
    if tree[0] == "and":
        wrap = lambda node, text: f"({text})" if node[0] == "or" else text  # noqa: E731
        return f"{wrap(tree[1], left)} and {wrap(tree[2], right)}"
    return f"{left} or {right}"


# ---------------------------------------------------------------- config.json

PATHS_FILE = "anime-paths.json"


def collection_paths(node: Node, report: Report, src: Src) -> tuple[Path | None, Path | None, Path | None]:
    """(waifu, source, results) из <collection>\anime-paths.json; ошибки — в report."""
    example = '"collection": "C:/Users/<имя>/Pictures/Anime"'
    value = need_text(node, report, src, "collection", example)
    if not value:
        return None, None, None
    root = Path(value)
    paths_file = root / PATHS_FILE
    if not paths_file.exists():
        report.error(src.at(node), f"collection: нет файла {paths_file}",
                     f"укажите папку коллекции, где лежит {PATHS_FILE} (образец — anime-vault/anime-paths.example.json)")
        return None, None, None
    try:
        data = json.loads(paths_file.read_text(encoding="utf-8-sig"))
        waifu, source, results = (root / data[key] for key in ("waifu", "batches", "results"))
    except (ValueError, KeyError, TypeError) as exc:
        report.error(src.at(node), f"{paths_file}: не прочитать waifu / batches / results ({exc})",
                     "проверьте JSON и эти три поля (пути относительно папки коллекции)")
        return None, None, None
    for name, path in (("waifu", waifu), ("batches", source)):
        if not path.is_dir():
            report.error(src.at(node), f"{PATHS_FILE} → {name}: папки «{path}» нет", "проверьте путь в файле путей коллекции")
            return None, None, None
    results.mkdir(parents=True, exist_ok=True)
    return waifu, source, results


def load_settings(report: Report, configs: Path | None = None, check_run_files: bool = True) -> Settings | None:
    configs = configs or CONFIGS
    path = configs / "config.json"
    src = Src("config.json")
    if not path.exists():
        report.error("configs\\config.json", "файл не найден", "создайте configs\\config.json (образец — в configs\\README.txt)")
        return None
    root = open_config(path, report, "config.json")
    if root is None:
        return None
    check_keys(root, SETTINGS_KEYS, report, src, "config.json",
               "в config.json бывают: run, collection, names, log_columns, table_columns, monitor, empty_cells "
               "(свободные ядра и другие параметры на ходу — в live.json)")

    run: list[str] = []
    node = root.get("run")
    if node is None:
        report.error(src.at(), "не указан run — какие конфиги запускать", '"run": ["config1"]')
    else:
        items = node.value if node.kind == "array" else [node]
        if node.kind == "array" and not items:
            report.error(src.at(node), "run пустой", 'укажите хотя бы один конфиг: "run": ["config1"]')
        for item in items:
            name = need_text(item, report, src, "run", '"run": ["config1", "config2"]')
            if not name:
                continue
            name = name.removesuffix(".json")
            if name.casefold() == "config":
                report.error(src.at(item), "в run указан сам config.json", "в run перечисляются configN (config1, config2 …)")
                continue
            if name.casefold() == "live":
                report.error(src.at(item), "в run указан live.json", "live.json — параметры на ходу, а не пачка; в run перечисляются configN")
                continue
            if any(existing.casefold() == name.casefold() for existing in run):
                report.error(src.at(item), f"«{name}» указан в run дважды", "оставьте каждый конфиг один раз")
                continue
            if check_run_files and not (configs / f"{name}.json").exists():
                available = sorted(p.stem for p in configs.glob("*.json") if p.stem.casefold() not in ("config", "live"))
                report.error(src.at(item), f"конфиг «{name}» не найден (нет файла configs\\{name}.json)" + suggest(name, available),
                             "создайте этот файл или исправьте имя; есть: " + (", ".join(available) or "ни одного"))
                continue
            run.append(name)

    def folder(key: str, must_exist: bool = True) -> Path | None:
        node = root.get(key)
        example = f'"{key}": "C:/путь/к/папке"'
        if node is None:
            report.error(src.at(), f"не указан параметр {key}", f"добавьте строку {example}")
            return None
        value = need_text(node, report, src, key, example)
        if not value:
            return None
        target = Path(value)
        if not target.is_absolute():
            report.error(src.at(node), f"{key}: путь «{value}» не полный", 'укажите полный путь с буквой диска: "C:/..."')
            return None
        if must_exist and not target.is_dir():
            report.error(src.at(node), f"{key}: папки «{value}» нет", "проверьте путь (скопируйте его из адресной строки Проводника, слэши — / )")
            return None
        if not must_exist and not target.is_dir() and not target.parent.is_dir():
            report.error(src.at(node), f"{key}: нет ни папки «{value}», ни папки над ней", "проверьте путь")
            return None
        return target

    # Пути коллекции: "collection" — папка с anime-paths.json (общий файл путей с anime-vault): из него
    # waifu, batches (source — папки dataN) и results (test-dataN). Старые ключи waifu/source/results — запасной вариант.
    if root.get("collection") is not None:
        waifu, source, results = collection_paths(root.get("collection"), report, src)
    else:
        waifu = folder("waifu")
        source = folder("source")
        results = folder("results", must_exist=False)

    names: dict[str, tuple[str, str]] = {}
    seen_labels: dict[str, str] = {}
    section = need_object(root.get("names"), report, src, "names", '"names": {"AI-K3": ["AI-K3", "AI Kimi K3"]}')
    for stage_id, node in (section.value.items() if section else []):
        example = f'"{stage_id}": ["{stage_id}", "Название в таблице"]'
        items = node.value if node.kind == "array" else [node]
        texts = [text_of(item) for item in items]
        if not 1 <= len(items) <= 2 or any(not text for text in texts):
            report.error(src.at(node), f"names → {stage_id}: нужно одно или два названия в кавычках", example)
            continue
        label = texts[0]
        table = texts[1] if len(texts) > 1 else texts[0]
        bad = sorted(set(label) & BAD_PATH_CHARS)
        if bad:
            report.error(src.at(node), f"метка «{label}» содержит символы {' '.join(bad)} — она идёт в имена файлов и папок",
                         "уберите символы \\ / : * ? \" < > |")
            continue
        if label.casefold() in ("other", "all") or re.match(r"^\d+(-\d+)?\. ", label):
            report.error(src.at(node), f"метка «{label}» совпадает со служебной папкой результатов", "выберите другую метку")
            continue
        if len(label) > 20:
            report.error(src.at(node), f"метка «{label}» длиннее 20 символов", "сократите метку (она стоит перед каждым файлом и в логе)")
            continue
        if label.casefold() in seen_labels:
            report.error(src.at(node), f"метка «{label}» уже занята этапом {seen_labels[label.casefold()]}", "метки разных этапов должны различаться")
            continue
        seen_labels[label.casefold()] = stage_id
        names[stage_id.casefold()] = (label, table)

    def columns(section_name: str, defaults: dict[str, int]) -> dict[str, int]:
        result = dict(defaults)
        section = need_object(root.get(section_name), report, src, section_name, f'"{section_name}": {{"stage": 12}}')
        for key, node in (section.value.items() if section else []):
            if key not in defaults:
                report.error(src.at(section.keys.get(key)), f"{section_name}: неизвестный столбец «{key}»" + suggest(key, defaults),
                             "столбцы: " + ", ".join(defaults))
                continue
            number = number_of(node, report, src, f"{section_name} → {key}", key, 1, 300)
            if number is None:
                continue
            if number != int(number):
                report.error(src.at(node), f"{section_name} → {key} = {number:g}: ширина — целое число символов", f'"{key}": {int(number)}')
                continue
            result[key] = int(number)
        return result

    log_columns = columns("log_columns", LOG_COLUMNS)
    table_columns = columns("table_columns", TABLE_COLUMNS)
    monitor = root.get("monitor")
    if monitor is not None:
        # Стартовый монитор окон (engine\winlayout.py): номер Windows; такого нет — окна пойдут на основной.
        number = number_of(monitor, report, src, "monitor", "monitor", 1, 16)
        if number is not None and number != int(number):
            report.error(src.at(monitor), f"monitor = {number:g}: номер монитора — целое число", f'"monitor": {int(number)}')
        elif number is not None:
            from engine import winlayout

            present = [item["number"] for item in winlayout.monitors()]
            if int(number) not in present:
                report.warn(src.at(monitor), f"монитора {int(number)} сейчас нет (есть: {', '.join(map(str, present))}) — "
                                             "окна откроются на основном")
    empty_cells = "dash"
    node = root.get("empty_cells")
    if node is not None:
        value = need_text(node, report, src, "empty_cells", '"empty_cells": "blank"')
        if value is not None and value not in EMPTY_CELLS:
            report.error(src.at(node), f"empty_cells = «{value}»: нужно \"blank\" (пустые поля без символов) или "
                                       "\"dash\" (прочерки «—»)", '"empty_cells": "blank"')
        elif value is not None:
            empty_cells = value
    if None in (waifu, source, results):
        return None
    from engine import live

    return Settings(run, waifu, source, results, names, log_columns, table_columns, live.current().free_cores,
                    empty_cells=empty_cells)


# ---------------------------------------------------------------- providers

_PROVIDER_CACHE: dict[str, Provider | None] = {}


def load_provider(name: str, report: Report, where: str) -> Provider | None:
    cache_key = name.casefold()
    if cache_key in _PROVIDER_CACHE:
        return _PROVIDER_CACHE[cache_key]
    folder = PROVIDERS / name
    available = sorted(p.name for p in PROVIDERS.iterdir() if p.is_dir()) if PROVIDERS.is_dir() else []
    if not folder.is_dir():
        report.error(where, f"провайдер «{name}» не найден (нет папки providers\\{name})" + suggest(name, available),
                     "есть: " + (", ".join(available) or "ни одного"))
        _PROVIDER_CACHE[cache_key] = None
        return None
    label = f"providers\\{name}\\provider.json"
    src = Src(label)
    path = folder / "provider.json"
    if not path.exists():
        report.error(where, f"нет файла {label} — непонятно, куда отправлять запросы",
                     f"создайте {label} (образец — providers\\README.txt)")
        _PROVIDER_CACHE[cache_key] = None
        return None
    errors_before = len(report.errors)
    root = open_config(path, report, label)
    provider = None
    if root is not None:
        check_keys(root, PROVIDER_KEYS, report, src, "provider.json")
        endpoint = None
        node = root.get("endpoint")
        if node is None:
            report.error(src.at(), "не указан endpoint — адрес, куда отправлять запросы", '"endpoint": "https://.../v1/chat/completions"')
        else:
            endpoint = need_text(node, report, src, "endpoint", '"endpoint": "https://.../v1/chat/completions"')
            if endpoint and not re.match(r"^https?://[^\s/]+\.[^\s]+$", endpoint):
                report.error(src.at(node), f"endpoint «{endpoint}» не похож на адрес", "адрес начинается с https:// и содержит домен")
        node = root.get("auth")
        auth = (need_text(node, report, src, "auth", '"auth": "Bearer"') if node else "Bearer") or "Bearer"
        if auth.casefold() != "bearer":
            report.error(src.at(node), f"auth «{auth}» не поддерживается", '"auth": "Bearer"')
        node = root.get("reasoning")
        reasoning = (need_text(node, report, src, "reasoning", '"reasoning": "none"') if node else "none") or "none"
        if reasoning.casefold() not in REASONING:
            report.error(src.at(node), f"reasoning «{reasoning}» неизвестен", "допустимо: anthropic (мост), openrouter, none")
        interval = 3.2
        node = root.get("interval")
        if node is not None:
            interval = number_of(node, report, src, "interval (секунд между запросами)", "interval", 0, 120) or interval
        models: dict[str, str] = {}
        section = need_object(root.get("models"), report, src, "models", '"models": {"kimi-k3": "kimi-k3"}')
        if section is None or not section.value:
            if root.get("models") is None or section is not None:
                report.error(src.at(), "нет списка моделей models", '"models": {"имя-в-конфиге": "имя-у-провайдера"}')
        else:
            for key, node in section.value.items():
                target = need_text(node, report, src, f"модель {key}", f'"{key}": "{key}"')
                if target:
                    models[key] = target
        flags = {}
        for flag in ("images", "temperature"):
            node = root.get(flag)
            if node is None:
                flags[flag] = True
            elif node.kind != "bool":
                report.error(src.at(node), f"{flag}: {show(node)} — нужно true или false (без кавычек)", f'"{flag}": false')
            else:
                flags[flag] = node.value
        if len(report.errors) == errors_before:
            provider = Provider(name, folder, endpoint or "", "Bearer", reasoning.casefold(), interval, models,
                                flags.get("images", True), flags.get("temperature", True))
    _PROVIDER_CACHE[cache_key] = provider
    return provider


def resolve_api(value: str, node: Node, src: Src, report: Report, prefix: str) -> tuple[Provider | None, Path | None, bool]:
    """«провайдер/ключ» -> (провайдер, файл ключа, ключ на месте). Нет файла ключа или он пустой — предупреждение:
    этот API пока пропускается (ключ можно вписать позже — этап проверит файл снова при следующем круге)."""
    where = src.at(node)
    parts = [part for part in re.split(r"[\\/]+", value) if part]
    if len(parts) == 3 and parts[0].casefold() == "providers":
        parts = parts[1:]
    if len(parts) != 2:
        report.error(where, f"{prefix}: api «{value}» — нужно «провайдер/ключ», ровно две части", '"api": "bridge/key_new"')
        return None, None, False
    provider = load_provider(parts[0], report, where)
    key_name = parts[1].removesuffix(".txt")
    if key_name.casefold() == "provider":
        report.error(where, f"{prefix}: api указывает на provider.json, а не на файл ключа", '"api": "bridge/key_new"')
        return provider, None, False
    key_path = KEYS / parts[0] / f"{key_name}.txt"
    folder = KEYS / parts[0]
    if not key_path.is_file():
        keys = sorted(p.stem for p in folder.glob("*.txt") if p.name.casefold() != "readme.txt") if folder.is_dir() else []
        close = suggest(key_name, keys)
        if close:
            # Похоже на опечатку, а не на ещё не купленный ключ — сказать отдельно.
            report.warn(where, f"{prefix}: файла ключа {parts[0]}/{key_name}.txt нет" + close, f"есть ключи: {', '.join(keys)}")
        return provider, key_path, False
    try:
        lines = [line.strip() for line in key_path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    except (OSError, UnicodeDecodeError):
        lines = []
    if not lines:
        return provider, key_path, False
    if " " in lines[0] or len(lines[0]) < 8:
        report.error(where, f"{prefix}: в файле {parts[0]}/{key_name}.txt не похоже на ключ (пробелы или слишком короткий)",
                     "в файле должна быть только одна строка — сам ключ")
        return provider, None, False
    if len(lines) > 1:
        report.warn(where, f"{prefix}: в файле {parts[0]}/{key_name}.txt несколько строк — используется первая")
    return provider, key_path, True


def load_fallback(node: Node, stage_type: str, accept: float | None, src: Src, report: Report, prefix: str) -> Fallback | None:
    """"fallback": {"model": "claude-opus-5", "api": [...], "after": 20, "table": "AI Opus 5", "accept": 0.6}."""
    example = '"fallback": {"model": "claude-opus-5", "api": ["beniclo/key1"], "after": 20, "table": "AI Opus 5"}'
    prefix = f"{prefix}: fallback"
    if node.kind != "object":
        report.error(src.at(node), f"{prefix}: {show(node)} — нужен объект", example)
        return None
    ok = True
    for key in node.value:
        if key not in FALLBACK_KEYS:
            report.error(src.at(node.keys.get(key)), f"{prefix}: неизвестный параметр «{key}»" + suggest(key, FALLBACK_KEYS),
                         "допустимы: " + ", ".join(sorted(FALLBACK_KEYS)))
            ok = False
    model = need_text(node.get("model"), report, src, f"{prefix}: model", example) if node.get("model") is not None else None
    if node.get("model") is None:
        report.error(src.at(node), f"{prefix}: не указана model", example)
    if node.get("api") is None:
        report.error(src.at(node), f"{prefix}: не указан api", example)
        ok = False
    routes = load_routes(node.get("api"), model, stage_type, src, report, prefix) if model and node.get("api") is not None else None
    after = 20
    if node.get("after") is not None:
        number = number_of(node.get("after"), report, src, f"{prefix}: after", "after", 1, 1440)
        ok &= number is not None
        after = int(number or after)
    own_accept = accept
    if node.get("accept") is not None:
        own_accept = number_of(node.get("accept"), report, src, f"{prefix}: accept", "accept", 0, 1, low_inclusive=False)
        ok &= own_accept is not None
    table = text_of(node.get("table")) or model
    if not ok or model is None or routes is None:
        return None
    return Fallback(model, routes, after, table, own_accept)


def load_routes(node: Node, stage_model: str | None, stage_type: str, src: Src, report: Report, prefix: str) -> list[ApiRoute] | None:
    """"api": строка или список (строка «провайдер/ключ» или {"api": …, "model": …}) -> API по порядку попыток."""
    example = '"api": ["beniclo/key1", "ritttta/key1"]  или  "api": "bridge/key_new"'
    items = node.value if node.kind == "array" else [node]
    if node.kind == "array" and not items:
        report.error(src.at(node), f"{prefix}: api — пустой список", example)
        return None
    routes: list[ApiRoute] = []
    seen: dict[str, int] = {}
    ok = True
    for item in items:
        model = stage_model
        if item.kind == "object":
            extra = set(item.value) - {"api", "model"}
            if extra:
                report.error(src.at(item), f"{prefix}: в элементе api лишние параметры: {', '.join(sorted(extra))}",
                             '{"api": "ritttta/key3", "model": "gpt-6-sol"}')
                ok = False
                continue
            value = need_text(item.get("api"), report, src, f"{prefix}: api", '{"api": "ritttta/key3", "model": "gpt-6-sol"}') \
                if item.get("api") is not None else None
            if value is None:
                if item.get("api") is None:
                    report.error(src.at(item), f"{prefix}: в элементе api нет \"api\"", '{"api": "ritttta/key3", "model": "gpt-6-sol"}')
                ok = False
                continue
            if item.get("model") is not None:
                model = need_text(item.get("model"), report, src, f"{prefix}: model", '"model": "gpt-6-sol"')
                if model is None:
                    ok = False
                    continue
                if stage_type == "ai_image" and stage_model and model != stage_model:
                    report.warn(src.at(item), f"{prefix}: у API {value} другая модель ({model}), а accept подбирался под {stage_model}",
                                "у ai_image лучше держать одну модель на этап; другую модель — отдельным этапом со своим accept")
        elif item.kind == "string":
            value = need_text(item, report, src, f"{prefix}: api", example)
            if value is None:
                ok = False
                continue
        else:
            report.error(src.at(item), f"{prefix}: api — {show(item)}: нужна строка «провайдер/ключ» или объект", example)
            ok = False
            continue
        label = "/".join(part for part in re.split(r"[\\/]+", value) if part).removeprefix("providers/").removesuffix(".txt")
        if label.casefold() in seen:
            report.error(src.at(item), f"{prefix}: API {label} указан в списке дважды (первый раз — строка {seen[label.casefold()]})",
                         "оставьте каждый провайдер/ключ один раз")
            ok = False
            continue
        seen[label.casefold()] = item.line
        provider, key_path, has_key = resolve_api(value, item, src, report, prefix)
        if provider is None or key_path is None:
            ok = False
            continue
        if not model:
            ok = False
            continue
        if stage_type == "ai_image" and not provider.images:
            report.error(src.at(item), f"{prefix}: провайдер {provider.name} не принимает картинки (\"images\": false в provider.json), а этап ai_image отправляет картинку",
                         "уберите этот API из ai_image — он годится только для json_meta")
            ok = False
            continue
        if model not in provider.models:
            report.error(src.at(item), f"{prefix}: модели «{model}» нет в providers\\{provider.name}\\provider.json" + suggest(model, provider.models),
                         "есть: " + ", ".join(provider.models) + '. Новую модель добавьте в "models" файла provider.json')
            ok = False
            continue
        routes.append(ApiRoute(label, provider, key_path, model, provider.models[model], has_key))
    if not ok:
        return None
    missing = [route.label for route in routes if not route.has_key]
    if missing and len(missing) < len(routes):
        report.warn(src.at(node), f"{prefix}: ключей пока нет (файл пустой или отсутствует), эти API пропускаются: {', '.join(missing)}",
                    "впишите ключ одной строкой в providers\\<провайдер>\\<ключ>.txt — этап подхватит его сам, без перезапуска")
    if not any(route.has_key for route in routes):
        report.error(src.at(node), f"{prefix}: ни у одного API этапа нет ключа (файлы ключей отсутствуют или пустые)",
                     "впишите хотя бы один ключ в providers\\<провайдер>\\<ключ>.txt")
        return None
    return routes


# ---------------------------------------------------------------- configN.json

def expand_folders(node: Node, src: Src, report: Report, source: Path | None, results: Path | None = None) -> list[str]:
    if node.kind == "string":
        items = [(part.strip(), node) for part in node.value.split(",")]
    elif node.kind == "array":
        items = [(text_of(item) or "", item) for item in node.value]
        for item in node.value:
            if item.kind != "string":
                report.error(src.at(item), f"folders: {show(item)} — нужно имя папки в кавычках", '"folders": ["data8..30", "data49"]')
    else:
        report.error(src.at(node), f"folders: {show(node)} — нужен список папок", '"folders": ["data8..30", "data49"]')
        return []
    folders: list[str] = []
    for text, item in items:
        if not text:
            if item.kind == "string":
                report.error(src.at(item), "folders: пустой элемент", '"folders": ["data8..30", "data49"]')
            continue
        match = re.fullmatch(r"data(\d+)\s*\.\.\s*(?:data)?(\d+)", text, re.I)
        if match:
            start, end = int(match.group(1)), int(match.group(2))
            step = 1 if end >= start else -1
            folders += [f"data{number}" for number in range(start, end + step, step)]
            continue
        if ".." in text:
            report.error(src.at(item), f"folders: диапазон «{text}» записан неверно", '"data8..30" (или "data30..8" — в обратном порядке)')
            continue
        if re.fullmatch(r"\d+", text):
            report.error(src.at(item), f"folders: «{text}» — нужно имя папки", f'"data{text}"')
            continue
        folders.append(text)
    seen: dict[str, int] = {}
    for name in folders:
        seen[name.casefold()] = seen.get(name.casefold(), 0) + 1
    repeated = [name for name, count in seen.items() if count > 1]
    if repeated:
        report.error(src.at(node), "folders: папки указаны больше одного раза: " + ", ".join(repeated[:10]), "каждая папка — один раз (проверьте пересекающиеся диапазоны)")
    if source is not None:
        def moved(name: str) -> bool:
            # dataN-other удаляется, как только файлы перенесены в test-dataN-other\in (engine\dataset.py).
            return (name.casefold().endswith("-other") and results is not None
                    and (results / f"test-{name}" / "in").is_dir())

        missing = [name for name in folders if not (source / name).is_dir() and not moved(name)]
        if missing:
            shown = ", ".join(missing[:8]) + (f" и ещё {len(missing) - 8}" if len(missing) > 8 else "")
            report.error(src.at(node), f"folders: в {source} нет папок: {shown}", "проверьте номера или параметр source в config.json")
        empty = [name for name in folders if (source / name).is_dir() and not any(p.suffix.lower() in IMAGE_EXTENSIONS for p in (source / name).iterdir())]
        if empty:
            report.error(src.at(node), "folders: в папках нет картинок: " + ", ".join(empty[:8]), "уберите их из списка")
    return folders


def load_config(file: str, settings: Settings | None, report: Report, configs: Path | None = None) -> ConfigSpec | None:
    configs = configs or CONFIGS
    src = Src(f"{file}.json")
    root = open_config(configs / f"{file}.json", report, f"{file}.json")
    if root is None:
        return None
    check_keys(root, CONFIG_KEYS, report, src, f"{file}.json",
               'в configN.json бывают: name, folders, previous, stages (параметры этапов — внутри "stages": [ {...} ])')
    name = file
    node = root.get("name")
    if node is None:
        report.error(src.at(), "не указано name — имя пачки (для журнала logs\\batch_<name>.log)", '"name": "reverse"')
    else:
        value = need_text(node, report, src, "name", '"name": "reverse"')
        if value and (set(value) & BAD_PATH_CHARS or len(value) > 40):
            report.error(src.at(node), f"name «{value}» не годится для имени файла журнала", "латиница/кириллица, цифры, - и _, до 40 символов")
        elif value:
            name = value
    folders: list[str] = []
    node = root.get("folders")
    if node is None:
        report.error(src.at(), "не указано folders — какие папки проходить", '"folders": ["data8..30", "data49"]')
    else:
        folders = expand_folders(node, src, report, settings.source if settings else None, settings.results if settings else None)
        if not folders and node.kind == "array" and not node.value:
            report.error(src.at(node), "folders пустой", '"folders": ["data8..30"]')
    keep_previous = False
    node = root.get("previous")
    if node is not None:
        value = (text_of(node) or "").casefold()
        if value in ("keep", "drop"):
            keep_previous = value == "keep"
        else:
            report.error(src.at(node), f"previous {show(node)} — непонятное значение",
                         '"previous": "keep" (сохранить результаты прошлых прогонов набора) или "drop" (по умолчанию: '
                         "действуют только этапы этого конфига)")
    node = root.get("stages")
    if node is None or node.kind != "array" or not node.value:
        report.error(src.at(node), "нет списка этапов stages" if node is None or node.kind == "array" else f"stages: {show(node)} — нужен список",
                     '"stages": [ {"id": "WD-14", "type": "wd14", "accept": 0.75}, ... ]')
        return None
    enabled_nodes = []
    for stage_node in node.value:
        if stage_node.kind != "object":
            report.error(src.at(stage_node), f"этап {show(stage_node)} — нужен объект в фигурных скобках", '{"id": "WD-14", "type": "wd14", "accept": 0.75}')
            continue
        if not stage_node.value:
            continue   # только комментарий {"//": "..."}
        enabled = stage_node.get("enabled")
        if enabled is not None and enabled.kind != "bool":
            report.error(src.at(enabled), f"enabled: {show(enabled)} — нужно true или false (без кавычек)", '"enabled": false')
            continue
        if enabled is None or enabled.value:
            enabled_nodes.append(stage_node)
    if not enabled_nodes:
        report.error(src.at(node), "все этапы выключены (enabled: false)", "включите хотя бы один этап")
        return None
    all_ids: dict[str, int] = {}
    for stage_node in enabled_nodes:
        stage_id = text_of(stage_node.get("id"))
        if stage_id:
            all_ids.setdefault(stage_id.casefold(), stage_node.line)
    stages: list[StageSpec] = []
    ids_seen: dict[str, int] = {}
    labels_seen: dict[str, str] = {}
    for index, stage_node in enumerate(enabled_nodes, start=1):
        spec = load_stage(stage_node, index, src, report, settings, stages, ids_seen, labels_seen, all_ids)
        if spec is not None:
            stages.append(spec)
    if not stages:
        return None
    return ConfigSpec(file, name, folders, stages, keep_previous=keep_previous)


def load_stage(obj: Node, index: int, src: Src, report: Report, settings: Settings | None,
               previous: list[StageSpec], ids_seen: dict[str, int], labels_seen: dict[str, str],
               all_ids: dict[str, int]) -> StageSpec | None:
    id_node = obj.get("id")
    if id_node is None:
        report.error(src.at(obj), f"у этапа №{index} не указан id — имя этапа", '"id": "AI-K3"')
        return None
    stage_id = need_text(id_node, report, src, "id", '"id": "AI-K3"')
    if not stage_id:
        return None
    head = src.at(obj)
    prefix = f"этап {stage_id}"
    valid = True
    if not ID_RE.match(stage_id):
        report.error(src.at(id_node), f"имя этапа «{stage_id}» не годится",
                     "начинается с латинской буквы, дальше латиница, цифры, - _ и &, до 20 символов, без пробелов: AI-K3, N-L&R")
        valid = False
    if stage_id.upper() in RESERVED_IDS:
        report.error(src.at(id_node), f"имя этапа «{stage_id}» зарезервировано программой", "выберите другое имя этапа")
        valid = False
    if stage_id.casefold() in ids_seen:
        report.error(src.at(id_node), f"этап {stage_id} уже есть (строка {ids_seen[stage_id.casefold()]})",
                     "имена этапов в одном конфиге не повторяются; для второй копии дайте другое имя, например AI-K3b")
        valid = False
    ids_seen.setdefault(stage_id.casefold(), obj.line)
    type_node = obj.get("type")
    stage_type = None
    if type_node is None:
        report.error(head, f"{prefix}: не указан type", '"type": один из ' + ", ".join(STAGE_TYPES))
        valid = False
    else:
        value = text_of(type_node)
        if value and value.casefold() in STAGE_TYPES:
            stage_type = value.casefold()
        else:
            report.error(src.at(type_node), f"{prefix}: неизвестный type {show(type_node)}" + suggest(value or "", STAGE_TYPES),
                         "допустимо: " + ", ".join(f"{k} ({v['about']})" for k, v in STAGE_TYPES.items()))
            valid = False
    if stage_type is None:
        return None
    info = STAGE_TYPES[stage_type]
    allowed = {"id", "type", "enabled", "from", "where"} | ({"accept"} if info["accept"] else set()) | ({"api", "model", "failover", "fallback"} if info["api"] else set())
    for key in obj.value:
        if key in allowed:
            continue
        line = obj.keys.get(key)
        if key == "accept":
            report.error(src.at(line), f"{prefix}: у этапа {stage_type} нет порога принятия (accept)",
                         "уберите accept: соседи определяют по соседним файлам, без уверенности")
        elif key in ("api", "model", "failover", "fallback"):
            report.error(src.at(line), f"{prefix}: этапу {stage_type} не нужен {key}", f"уберите {key}: этот этап работает локально")
        else:
            report.error(src.at(line), f"{prefix}: неизвестный параметр «{key}»" + suggest(key, allowed),
                         "для этого этапа допустимы: " + ", ".join(sorted(allowed)))
        valid = False

    accept = None
    if info["accept"]:
        node = obj.get("accept")
        if node is None:
            report.error(head, f"{prefix}: не указан accept — порог принятия (обязателен)", '"accept": 0.55')
            valid = False
        else:
            accept = number_of(node, report, src, f"{prefix}: accept", "accept", 0, 1, low_inclusive=False)
            valid &= accept is not None

    routes: list[ApiRoute] = []
    model = None
    failover: dict[str, int] = {}
    if info["api"]:
        node = obj.get("model")
        if node is None:
            report.error(head, f"{prefix}: не указана model", '"model": "kimi-k3"')
            valid = False
        else:
            model = need_text(node, report, src, f"{prefix}: model", '"model": "kimi-k3"')
            valid &= model is not None
        node = obj.get("api")
        if node is None:
            report.error(head, f"{prefix}: не указан api — провайдер/ключ или список", '"api": ["beniclo/key1", "ritttta/key1"]')
            valid = False
        elif model:
            loaded = load_routes(node, model, stage_type, src, report, prefix)
            if loaded is None:
                valid = False
            else:
                routes = loaded
        node = obj.get("failover")
        if node is not None:
            if node.kind != "object":
                report.error(src.at(node), f"{prefix}: failover: {show(node)} — нужен объект", '"failover": {"errors": 3, "minutes": 5}')
                valid = False
            else:
                for key, value in node.value.items():
                    if key not in FAILOVER_KEYS:
                        report.error(src.at(node.keys.get(key)), f"{prefix}: failover: неизвестный параметр «{key}»" + suggest(key, FAILOVER_KEYS),
                                     "допустимы: " + ", ".join(FAILOVER_KEYS))
                        valid = False
                        continue
                    field_name, low, high = FAILOVER_KEYS[key]
                    number = number_of(value, report, src, f"{prefix}: failover → {key}", key, low, high)
                    if number is None or number != int(number):
                        if number is not None:
                            report.error(src.at(value), f"{prefix}: failover → {key} = {number:g}: нужно целое число", f'"{key}": {int(number)}')
                        valid = False
                        continue
                    failover[field_name] = int(number)
    fallback = None
    if info["api"] and obj.get("fallback") is not None:
        fallback = load_fallback(obj.get("fallback"), stage_type, accept, src, report, prefix)
        valid &= fallback is not None

    from_value = None
    from_node = obj.get("from")
    if from_node is not None:
        from_value = number_of(from_node, report, src, f"{prefix}: from", "from", 0, 1, high_inclusive=False)
        valid &= from_value is not None
    where_tree, where_text = None, ""
    where_node = obj.get("where")
    if where_node is not None:
        raw = need_text(where_node, report, src, f"{prefix}: where", '"where": "[AI-K3] or [AI-F5]"')
        if raw is None:
            valid = False
        else:
            try:
                where_tree = parse_where(raw)
                where_text = where_to_text(where_tree)
            except WhereError as exc:
                report.error(src.at(where_node), f"{prefix}: where: {exc}",
                             'пример: "where": "[AI-K3] or [AI-F5]"; скобки для группировки: "([A] or [B]) and [C]"')
                valid = False

    # --- вход этапа: from / where против предыдущих этапов
    lo = from_value or 0.0
    confidence_stages = [stage for stage in previous if stage.gives_confidence]
    if index == 1 and (from_node is not None or where_node is not None):
        report.warn(head, f"{prefix}: первый этап — from/where игнорируются (перед ним нет этапов)")
        from_value, where_tree, where_text = None, None, ""
    elif where_tree is not None:
        by_id = {stage.id.casefold(): stage for stage in previous}
        for ref in dict.fromkeys(where_refs(where_tree)):
            target = by_id.get(ref.casefold())
            if ref.casefold() == stage_id.casefold():
                report.error(src.at(where_node), f"{prefix}: where ссылается на сам этап [{ref}]", "в where указываются только этапы выше этого")
                valid = False
            elif target is None:
                line_of_ref = all_ids.get(ref.casefold())
                if line_of_ref is not None and line_of_ref < obj.line:
                    valid = False   # этап выше сам с ошибками — о нём уже сказано
                elif line_of_ref is not None:
                    report.error(src.at(where_node), f"{prefix}: where — этап [{ref}] идёт ниже этого, его результатов ещё нет",
                                 "в where можно ссылаться только на этапы, стоящие выше в списке stages")
                else:
                    report.error(src.at(where_node), f"{prefix}: where — этапа [{ref}] нет в этом конфиге (или он выключен)" + suggest(ref, [s.id for s in previous]),
                                 "проверьте имя (id этапа, в квадратных скобках)")
                valid = False
            elif not target.gives_confidence:
                report.error(src.at(where_node), f"{prefix}: where — этап [{ref}] ({target.type}) не даёт уверенности, диапазон по нему не посчитать",
                             "ссылайтесь на этапы wd14, camie, ai_image или json_meta")
                valid = False
            elif target.accept is not None and lo >= target.accept:
                report.error(src.at(from_node or where_node),
                             f"{prefix}: from = {lo:g} не меньше порога [{ref}] (accept = {target.accept:g}) — через [{ref}] сюда не попадёт ни один файл",
                             f"уменьшите from ниже {target.accept:g} или уберите [{ref}] из where")
                valid = False
    elif from_node is not None and from_value is not None:
        if not confidence_stages:
            report.error(src.at(from_node), f"{prefix}: from — выше нет ни одного этапа с уверенностью, не от чего считать",
                         "уберите from или поставьте этап выше (wd14, camie, ai_image, json_meta)")
            valid = False
        elif all(stage.accept is not None and from_value >= stage.accept for stage in confidence_stages):
            report.error(src.at(from_node), f"{prefix}: from = {from_value:g} не меньше порогов всех этапов выше — сюда не попадёт ни один файл",
                         "уменьшите from")
            valid = False

    if not valid:
        return None
    label, table = stage_id, stage_id
    if settings is not None and stage_id.casefold() in settings.names:
        label, table = settings.names[stage_id.casefold()]
    if label.casefold() in labels_seen:
        report.error(head, f"{prefix}: метка «{label}» совпадает с меткой этапа {labels_seen[label.casefold()]}",
                     "задайте этапам разные метки в names (config.json) — иначе перепутаются папки и префиксы файлов")
        return None
    labels_seen[label.casefold()] = stage_id
    if settings is not None:
        width = settings.log_columns["stage"]
        if len(label) + 2 > width + 1:
            report.warn(head, f"{prefix}: метка «[{label}]» длиннее столбца этапа в логе ({width})", "увеличьте stage в log_columns или сократите метку")
        if len(table) > settings.table_columns["stage"]:
            report.warn(head, f"{prefix}: название «{table}» длиннее столбца таблицы ({settings.table_columns['stage']}) — будет перенесено на вторую строку")
    return StageSpec(stage_id, stage_type, index, label, table, accept, from_value, where_tree, where_text,
                     routes, model, failover, fallback)


# ---------------------------------------------------------------- всё вместе

def check_conflicts(spec: ConfigSpec, others: list[ConfigSpec], report: Report) -> None:
    """Пачки не должны делить имя или папки с уже принятыми (работающими) конфигами."""
    for other in others:
        if other.file.casefold() == spec.file.casefold():
            continue
        if other.name.casefold() == spec.name.casefold():
            report.error(f"{spec.file}.json", f"name «{spec.name}» уже у {other.file}.json", "имена пачек должны различаться (по ним называются журналы)")
        clash = [folder for folder in spec.folders if folder.casefold() in {f.casefold() for f in other.folders}]
        if clash:
            report.error(f"{spec.file}.json", f"папки {', '.join(clash[:6])} уже проходит {other.file}.json",
                         "два конфига не могут одновременно обрабатывать одну папку — уберите её из одного из них")


def check_one(file: str, settings: Settings, accepted: list[ConfigSpec], configs: Path | None = None) -> tuple[ConfigSpec | None, Report]:
    """Проверка одного configN (консоль main.py): сам файл + конфликты с уже принятыми."""
    _PROVIDER_CACHE.clear()
    report = Report()
    spec = load_config(file, settings, report, configs)
    if spec is not None:
        spec.column = settings.run.index(file) if file in settings.run else 0
        check_conflicts(spec, accepted, report)
    return (spec if not report.errors else None), report


def check_all(configs: Path | None = None) -> tuple[Settings | None, list[ConfigSpec], Report]:
    _PROVIDER_CACHE.clear()
    report = Report()
    settings = load_settings(report, configs)
    specs: list[ConfigSpec] = []
    for column, file in enumerate(settings.run if settings else []):
        spec = load_config(file, settings, report, configs)
        if spec is not None:
            spec.column = column
            check_conflicts(spec, specs, report)
            specs.append(spec)
    return settings, specs, report


def load_one(file: str) -> tuple[Settings, ConfigSpec]:
    """Для работающей пачки/набора: config.json + один configN из CONFIGS (снимок main.py).
    Другие конфиги не проверяются. Ошибка — исключение с текстом."""
    _PROVIDER_CACHE.clear()
    report = Report()
    settings = load_settings(report, check_run_files=False)
    spec = load_config(file, settings, report) if settings else None
    if report.errors or spec is None or settings is None:
        raise RuntimeError("ошибки в конфигах:\n" + "\n".join(report.errors or [f"конфиг {file} не найден"]))
    spec.column = settings.run.index(file) if file in settings.run else 0
    return settings, spec
