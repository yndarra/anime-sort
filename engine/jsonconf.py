r"""Чтение конфигов в формате JSON с номерами строк и понятными сообщениями об ошибках.

load(path) -> (корневой узел или None, [(строка, что не так, как исправить), ...]).
Узел (Node) помнит строку, где записано значение, — проверка конфигов указывает её в сообщениях.
Комментарии: JSON их не поддерживает, поэтому ключи, начинающиеся с "//", игнорируются:
    "//": "любой текст"
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Node:
    kind: str                 # object | array | string | number | bool | null
    value: object             # object: dict ключ->Node; array: list[Node]; остальное — значение Python
    line: int
    keys: dict = field(default_factory=dict)    # object: ключ -> строка, где записан ключ

    def get(self, key: str):
        return self.value.get(key) if self.kind == "object" else None


def is_comment(key: str) -> bool:
    return key.startswith("//")


# Типичные ошибки JSON -> по-русски и как исправить.
HINTS = (
    ("Invalid \\escape", "обратный слэш \\ в строке JSON — служебный символ",
     'пишите путь с прямыми слэшами "C:/Users/..." (или удвойте обратные: "C:\\\\Users\\\\...")'),
    ("Invalid \\uXXXX escape", "обратный слэш \\ в строке JSON — служебный символ",
     'пишите путь с прямыми слэшами "C:/Users/..." (или удвойте обратные: "C:\\\\Users\\\\...")'),
    ("Expecting ',' delimiter", "не хватает запятой между элементами (или лишняя/незакрытая кавычка)",
     "поставьте запятую после предыдущего значения; у последнего элемента перед } или ] запятой быть не должно"),
    ("Expecting property name enclosed in double quotes", "ожидается имя параметра в двойных кавычках",
     'чаще всего это лишняя запятая перед }, одинарные кавычки \'...\' или имя без кавычек — пишите "имя": значение'),
    ("Expecting ':' delimiter", "после имени параметра нет двоеточия", 'пишите "имя": значение'),
    ("Expecting value", "здесь ожидается значение",
     'лишняя запятая перед ] или }, пропущенное значение или текст без кавычек. Строки — в двойных кавычках "…", '
     "числа — без кавычек через точку (0.55), логические — true / false маленькими буквами"),
    ("Unterminated string", "строка не закрыта кавычкой", 'закройте строку двойной кавычкой "'),
    ("Invalid control character", "внутри строки перенос строки или табуляция", "строка должна помещаться в одну строку файла"),
    ("Extra data", "после конца конфига (закрывающей }) есть ещё текст", "уберите лишнее или перенесите его внутрь {...}"),
)


def explain(error: json.JSONDecodeError, text: str) -> tuple[str, str]:
    lines = text.splitlines()
    source_line = lines[error.lineno - 1] if 0 < error.lineno <= len(lines) else ""
    stripped = source_line.strip()
    if stripped.startswith(("#", "//", "/*")):
        return ("в JSON нет комментариев через # или //",
                'удалите строку или сделайте её параметром-комментарием: "//": "текст"')
    if "'" in source_line and '"' not in source_line:
        return ("строки в JSON пишутся в двойных кавычках, а не в одинарных", 'замените \'текст\' на "текст"')
    if error.msg.startswith("Expecting value") and re.search(r"\b(True|False|TRUE|FALSE|None|NULL)\b", source_line):
        return ("логические значения в JSON пишутся маленькими буквами", "true / false (а пустое значение — null)")
    if re.search(r"\d,\d", source_line) and error.msg.startswith(("Expecting", "Extra")):
        return ("дробная часть числа через запятую", "пишите через точку: 0.55")
    for prefix, what, fix in HINTS:
        if error.msg.startswith(prefix):
            return what, fix
    return error.msg, "проверьте запятые, кавычки и скобки рядом с этим местом"


class _Parser:
    """Разбор уже проверенного json.loads текста — только чтобы узнать строки значений и повторы ключей."""

    def __init__(self, text: str):
        self.text = text
        self.pos = 0
        self.problems: list[tuple[int, str, str]] = []
        self.decoder = json.JSONDecoder()

    def line(self, pos: int | None = None) -> int:
        return self.text.count("\n", 0, self.pos if pos is None else pos) + 1

    def skip(self) -> None:
        while self.pos < len(self.text) and self.text[self.pos] in " \t\r\n\ufeff":
            self.pos += 1

    def value(self) -> Node:
        self.skip()
        char = self.text[self.pos]
        line = self.line()
        if char == "{":
            self.pos += 1
            result, keys = {}, {}
            self.skip()
            if self.text[self.pos] == "}":
                self.pos += 1
                return Node("object", result, line, keys)
            while True:
                self.skip()
                key_line = self.line()
                key, self.pos = self.decoder.raw_decode(self.text, self.pos)
                self.skip()
                self.pos += 1  # :
                node = self.value()
                if not is_comment(key):
                    if key in result:
                        self.problems.append((key_line, f"параметр «{key}» указан второй раз (первый — в строке {keys[key]})",
                                              "оставьте одно значение"))
                    else:
                        result[key], keys[key] = node, key_line
                self.skip()
                if self.text[self.pos] == ",":
                    self.pos += 1
                    continue
                self.pos += 1  # }
                return Node("object", result, line, keys)
        if char == "[":
            self.pos += 1
            items = []
            self.skip()
            if self.text[self.pos] == "]":
                self.pos += 1
                return Node("array", items, line)
            while True:
                items.append(self.value())
                self.skip()
                if self.text[self.pos] == ",":
                    self.pos += 1
                    continue
                self.pos += 1  # ]
                return Node("array", items, line)
        raw, self.pos = self.decoder.raw_decode(self.text, self.pos)
        if isinstance(raw, bool):
            return Node("bool", raw, line)
        if raw is None:
            return Node("null", None, line)
        if isinstance(raw, (int, float)):
            return Node("number", float(raw), line)
        return Node("string", raw, line)


def load(path: Path) -> tuple[Node | None, list[tuple[int, str, str]]]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return None, [(0, f"файл не читается: {exc}", "проверьте, что файл существует")]
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None, [(0, "файл не в кодировке UTF-8", "сохраните файл как UTF-8 (Блокнот: Файл → Сохранить как → UTF-8)")]
    if not text.strip():
        return None, [(0, "файл пустой", "впишите конфиг (образец — configs\\README.txt)")]
    try:
        json.loads(text)
    except json.JSONDecodeError as exc:
        what, fix = explain(exc, text)
        line = exc.lineno
        lines = text.splitlines()
        if exc.msg.startswith("Expecting ',' delimiter") and 0 < line <= len(lines) and not lines[line - 1][:exc.colno - 1].strip():
            # Ошибка встала на начало строки — значит, запятой не хватает в конце предыдущей непустой строки.
            previous = line - 1
            while previous > 1 and not lines[previous - 1].strip():
                previous -= 1
            line = previous
            what = "в конце строки не хватает запятой"
            fix = "поставьте запятую в конце этой строки (у последнего элемента перед } или ] запятой быть не должно)"
        return None, [(line, what, fix)]
    parser = _Parser(text)
    root = parser.value()
    if root.kind != "object":
        parser.problems.append((root.line, "конфиг должен быть объектом в фигурных скобках { ... }", "оберните параметры в { }"))
        return None, parser.problems
    return root, parser.problems
