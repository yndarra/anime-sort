r"""Размеры, шрифты и расстановка окон anime-sort: главное окно (engine\main.py + gui\main_window.py)
и окна наборов (gui\window.py).

Настройки — в configs\live.json: arrangements (расстановки 1–2: режим и отступы), setups (сетапы 1–3: размеры, шрифты).
В отличие от остальных параметров live.json они НЕ применяются на ходу:
    - главное окно берёт их при старте: сетап 1 и расстановка 1 — для себя и для всех новых окон;
    - дальше — только по кнопкам главного окна («Сетап 1/2/3», «Режим 1/2», «Монитор ›»): в момент нажатия
      live.json перечитывается, и изменения получают все окна (и активные, и уже готовых наборов).

Общее состояние всех окон — logs\windows.json (меняется под именованным мьютексом Windows):
    session       pid главного окна, которое начало эту раскладку (0 — окно набора запущено без него)
    arrangement   номер действующей расстановки; mode, margin, gap — её режим, отступ от краёв экрана и между окнами
                  (на момент старта / нажатия «Режим N»)
    monitor       рабочая область монитора [left, top, right, bottom] (без панели задач), куда ставятся окна;
                  при старте — монитор "monitor" из config.json (нет такого — основной), потом — где нажали «Режим N»
    setup         номер действующего сетапа; setup_values — его параметры (как записаны в live.json)
    generation    растёт при каждом нажатии кнопки; окно, заметив новое значение, применяет изменения
    action        "setup" — поменять размеры и шрифты; "arrange" — ещё и переставить окна по местам («Режим N»)
    next_slot     место следующего нового окна
    windows       pid -> {kind: console | gui, title, opened, slot}
    frame         заголовок и рамка окна набора: [видимая ширина − клиентская, видимая высота − клиентская]

Места (slot) по порядку открытия окон; координаты — видимые края окна (невидимые рамки Windows 10/11
для изменения размера учитываются):
    left-right  слева сверху вниз, пока окно влезает по высоте, потом справа сверху вниз; места кончились —
                снова слева сверху (второй круг, поверх первого) и т. д.
    center      посередине по горизонтали сверху вниз; не влезает — снова сверху, поверх.
Место 0 — главное окно (первое по времени открытия): со второго круга на его место окна
не кладутся, наложение начинается со второго места. Исключение — в столбец влезает только одно окно
(высота окна с отступами больше половины экрана): тогда класть больше некуда и наложение на главное разрешено.
Кнопка «Монитор ›» переносит все окна на следующий монитор справа (по расположению на столе, после самого
правого — самый левый) с той же расстановкой.
Кнопки «Наверх» и «Вниз» поднимают все окна anime-sort над окнами других программ или прячут под все окна.
Кнопка «Закрыть готовые» закрывает все окна наборов, где конвейер отработал (пройдены все этапы).
"""
from __future__ import annotations

import contextlib
import ctypes
import json
import os
import re
import time
from ctypes import wintypes
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
# Для тестов состояние можно увести в другой файл, чтобы не трогать окна работающего запуска.
STATE = Path(os.environ.get("ANIME_SORT_WINDOWS") or PROJECT / "logs" / "windows.json")
MUTEX = "Local\\anime-sort-windows"

# Режимы расстановки: значение в live.json -> как это выглядит (для консоли и сообщений об ошибках).
MODES = {
    "left-right": "сверху вниз слева, потом справа",
    "center": "сверху вниз посередине",
}
KEYS = ("arrangements", "setups")
SETUP_NAMES = ("1", "2", "3")
ARRANGEMENT_NAMES = ("1", "2")
ARRANGEMENT_KEYS = ("mode", "margin", "gap")
# margin — отступ от краёв экрана (без панели задач), gap — между соседними окнами; оба в пикселях.
MARGIN_LIMITS = (0, 300)
SETUP_KEYS = ("width", "height", "table_width", "log_font", "table_font")
# Сетап 1 по умолчанию — размеры окон до появления сетапов (1800×300 клиентской части, таблица 436, Consolas 9).
DEFAULT_SETUP = {"width": 1802, "height": 332, "table_width": 436, "log_font": 9, "table_font": 9}
DEFAULTS = {"arrangements": {"1": {"mode": "center", "margin": 20, "gap": 10},
                             "2": {"mode": "left-right", "margin": 20, "gap": 10}},
            "setups": {name: dict(DEFAULT_SETUP) for name in SETUP_NAMES}}
# Заголовок и рамка окна Tk на Windows 11 при 100%: видимое окно на 2 пикселя шире и на 32 выше клиентской части.
DEFAULT_FRAME = (2, 32)
# Невидимая рамка слева (для первой установки позиции до того, как окно показано и её можно измерить).
DEFAULT_LEFT_BORDER = 7
PERCENT_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*%\s*$")

# Диапазоны: (наименьшее, наибольшее) для пикселей и для процентов; auto — можно ли "auto".
LIMITS = {
    "width": ((300, 8000), (10, 100), True),
    "height": ((120, 5000), (5, 100), False),
    "table_width": ((150, 4000), (10, 90), True),
}
FONT_LIMITS = (6, 30)
EXAMPLES = {"width": '1802 (пиксели) или "49%" (от ширины экрана) или "auto" (по столбцам лога и таблицы)',
            "height": '332 (пиксели) или "23%" (от высоты экрана без панели задач)',
            "table_width": '436 (пиксели) или "25%" (от ширины окна) или "auto" (по столбцам таблицы)'}

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
dwmapi = ctypes.windll.dwmapi
user32.GetParent.restype = ctypes.c_void_p
user32.GetAncestor.restype = ctypes.c_void_p
user32.GetWindow.restype = ctypes.c_void_p
user32.MonitorFromWindow.restype = ctypes.c_void_p
user32.MonitorFromPoint.restype = ctypes.c_void_p
kernel32.CreateMutexW.restype = ctypes.c_void_p


# ---------- настройки в live.json ----------

def shown(node) -> str:
    return f'"{node.value}"' if node.kind == "string" else str(node.value).lower()


def is_integer(node) -> bool:
    return node.kind == "number" and node.value == int(node.value)


def check_size(key: str, node) -> str | None:
    """Что не так со значением размера (или None, если значение правильное)."""
    (low, high), (plow, phigh), auto = LIMITS[key]
    variants = f"пиксели (целое число от {low} до {high}) или проценты строкой (\"{plow}%\"…\"{phigh}%\")"
    variants += ' или "auto"' if auto else ""
    if node.kind == "number":
        return None if is_integer(node) and low <= node.value <= high else f"нужны {variants}"
    if node.kind == "string":
        text = node.value.strip().lower()
        if auto and text == "auto":
            return None
        match = PERCENT_RE.match(text)
        if match and plow <= float(match.group(1)) <= phigh:
            return None
        return f"проценты вне диапазона {plow}…{phigh}" if match else f"нужны {variants}"
    return f"нужны {variants}"


def validate(root) -> tuple[dict, list[str]]:
    """Настройки окон из корневого узла live.json (engine.jsonconf.Node).
    Возвращает (настройки, ошибки-строки для консоли). Неправильное значение заменяется значением по умолчанию."""
    config = json.loads(json.dumps(DEFAULTS))
    errors: list[str] = []

    def error(line, what: str, fix: str) -> None:
        errors.append(f"[live.json, строка {line}] {what}\n      Как исправить: {fix}")

    node = root.get("arrangements")
    if node is not None and node.kind != "object":
        error(node.line, "arrangements — нужен объект с расстановками",
              '"arrangements": {"1": {"mode": "center", "margin": 20, "gap": 10}, "2": {"mode": "left-right", "margin": 20, "gap": 10}}')
    elif node is not None:
        for name, item in node.value.items():
            where = f"arrangements → {name}"
            if name not in ARRANGEMENT_NAMES:
                error(node.keys.get(name), f"arrangements: неизвестная расстановка «{name}»", 'расстановки называются "1" и "2"')
                continue
            if item.kind != "object":
                error(item.line, f"{where} — нужен объект с параметрами", f'"{name}": {{"mode": "left-right", "margin": 20, "gap": 10}}')
                continue
            values = dict(config["arrangements"][name])
            for key, value in item.value.items():
                if key == "mode":
                    if value.kind == "string" and value.value in MODES:
                        values["mode"] = value.value
                    else:
                        error(value.line, f"{where} → mode = {shown(value)} — неизвестный режим расстановки окон",
                              " или ".join(f'"{mode}" ({about})' for mode, about in MODES.items()))
                elif key in ("margin", "gap"):
                    low, high = MARGIN_LIMITS
                    about = "от краёв экрана" if key == "margin" else "между окнами"
                    if is_integer(value) and low <= value.value <= high:
                        values[key] = int(value.value)
                    else:
                        error(value.line, f"{where} → {key} = {shown(value)} — нужен отступ {about} в пикселях, "
                                          f"целое число от {low} до {high}", f'"{key}": {DEFAULTS["arrangements"]["1"][key]}')
                else:
                    error(item.keys.get(key), f"{where}: неизвестный параметр «{key}»", "допустимы: " + ", ".join(ARRANGEMENT_KEYS))
            missing = [key for key in ARRANGEMENT_KEYS if key not in item.value]
            if missing:
                error(item.line, f"{where}: не хватает параметров: {', '.join(missing)}", "у каждой расстановки нужны mode, margin и gap")
            config["arrangements"][name] = values
    node = root.get("setups")
    if node is not None and node.kind != "object":
        error(node.line, "setups — нужен объект с сетапами", '"setups": {"1": {…}, "2": {…}, "3": {…}}')
    elif node is not None:
        for name, setup in node.value.items():
            where = f"setups → {name}"
            if name not in SETUP_NAMES:
                error(node.keys.get(name), f"setups: неизвестный сетап «{name}»", 'сетапы называются "1", "2" и "3"')
                continue
            if setup.kind != "object":
                error(setup.line, f"{where} — нужен объект с параметрами",
                      f'"{name}": {{"width": 1802, "height": 332, "table_width": 436, "log_font": 9, "table_font": 9}}')
                continue
            values = dict(config["setups"][name])
            for key, value in setup.value.items():
                if key not in SETUP_KEYS:
                    error(setup.keys.get(key), f"{where}: неизвестный параметр «{key}»", "допустимы: " + ", ".join(SETUP_KEYS))
                    continue
                if key in LIMITS:
                    problem = check_size(key, value)
                    if problem is None:
                        values[key] = value.value.strip().lower() if value.kind == "string" else int(value.value)
                        continue
                    error(value.line, f"{where} → {key} = {shown(value)}: {problem}", f'"{key}": {EXAMPLES[key]}')
                    continue
                low, high = FONT_LIMITS
                if is_integer(value) and low <= value.value <= high:
                    values[key] = int(value.value)
                else:
                    error(value.line, f"{where} → {key} = {shown(value)}: размер шрифта — целое число от {low} до {high} (пункты)",
                          f'"{key}": 9')
            missing = [key for key in SETUP_KEYS if key not in setup.value]
            if missing:
                error(setup.line, f"{where}: не хватает параметров: {', '.join(missing)}",
                      "у каждого сетапа нужны " + ", ".join(SETUP_KEYS))
            config["setups"][name] = values
    return config, errors


def read_config(path: Path | None = None) -> tuple[dict, list[str]]:
    """Настройки окон из live.json прямо сейчас (при нажатии кнопки). Ошибки — как в консоли."""
    from engine import jsonconf, live

    path = path or live.LIVE
    if not path.exists():
        return json.loads(json.dumps(DEFAULTS)), []
    root, problems = jsonconf.load(path)
    errors = [f"[live.json, строка {line}] {what}\n      Как исправить: {fix}" for line, what, fix in problems]
    if root is None:
        return json.loads(json.dumps(DEFAULTS)), errors
    config, window_errors = validate(root)
    return config, errors + window_errors


def describe_setup(values: dict) -> str:
    return (f"{values['width']}×{values['height']}, таблица {values['table_width']}, "
            f"шрифт лога {values['log_font']}, таблицы {values['table_font']}")


def describe_arrangement(values: dict) -> str:
    return f"{values['mode']} ({MODES[values['mode']]}), отступ от краёв {values['margin']}, между окнами {values['gap']}"


def describe(config: dict) -> list[str]:
    """Строки для консоли при старте: расстановки и сетапы."""
    lines = ["окна: сейчас режим 1 и сетап 1"]
    lines += [f"  режим {name}: {describe_arrangement(config['arrangements'][name])}" for name in ARRANGEMENT_NAMES]
    lines += [f"  сетап {name}: {describe_setup(config['setups'][name])}" for name in SETUP_NAMES]
    return lines


def changes(old: dict, new: dict) -> list[str]:
    """Что поменялось в настройках окон (для строки «live.json изменён»)."""
    result = []
    for name in ARRANGEMENT_NAMES:
        for key in ARRANGEMENT_KEYS:
            before, after = old["arrangements"][name][key], new["arrangements"][name][key]
            if before != after:
                result.append(f"режим {name} {key}: {before} → {after}")
    for name in SETUP_NAMES:
        for key in SETUP_KEYS:
            before, after = old["setups"][name][key], new["setups"][name][key]
            if before != after:
                result.append(f"сетап {name} {key}: {before} → {after}")
    return result


def pixels(value, total: int) -> int | None:
    """Значение из сетапа в пикселях: число — как есть, "45%" — доля от total, "auto" — None (считает окно)."""
    if isinstance(value, (int, float)):
        return int(value)
    match = PERCENT_RE.match(str(value))
    if match:
        return round(total * float(match.group(1)) / 100)
    return None


# ---------- мониторы и геометрия окон (Win32) ----------

class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


def work_area(hwnd=None) -> list[int]:
    """Рабочая область (без панели задач) монитора, где окно hwnd; без hwnd — основного монитора."""
    if hwnd:
        monitor = user32.MonitorFromWindow(ctypes.c_void_p(hwnd), 2)                  # MONITOR_DEFAULTTONEAREST
    else:
        monitor = user32.MonitorFromPoint(wintypes.POINT(0, 0), 1)                    # MONITOR_DEFAULTTOPRIMARY
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    if not monitor or not user32.GetMonitorInfoW(ctypes.c_void_p(monitor), ctypes.byref(info)):
        return [0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)]
    rect = info.rcWork
    return [rect.left, rect.top, rect.right, rect.bottom]


class MONITORINFOEX(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD), ("szDevice", wintypes.WCHAR * 32)]


def monitors() -> list[dict]:
    """Все мониторы: номер Windows (из имени устройства DISPLAYn — обычно совпадает с цифрой в «Параметры → Дисплей →
    Определить»), рабочая область, полный размер и основной ли. По возрастанию номера."""
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(wintypes.RECT), ctypes.c_void_p)
    def callback(handle, _dc, _rect, _data):
        info = MONITORINFOEX()
        info.cbSize = ctypes.sizeof(MONITORINFOEX)
        if user32.GetMonitorInfoW(ctypes.c_void_p(handle), ctypes.byref(info)):
            digits = re.search(r"(\d+)$", info.szDevice)
            work, full = info.rcWork, info.rcMonitor
            found.append({"number": int(digits.group(1)) if digits else 0,
                          "work": [work.left, work.top, work.right, work.bottom],
                          "size": (full.right - full.left, full.bottom - full.top), "primary": bool(info.dwFlags & 1)})
        return True

    user32.EnumDisplayMonitors(None, None, callback, 0)
    return sorted(found, key=lambda item: item["number"])


def start_monitor(configs: Path | None = None) -> int | None:
    """Номер стартового монитора — "monitor" в config.json (проверяет engine/config.py); нет или ошибка — None."""
    from engine import jsonconf

    path = Path(configs or os.environ.get("ANIME_SORT_CONFIGS") or PROJECT / "configs") / "config.json"
    try:
        root, _ = jsonconf.load(path)
        node = root.get("monitor") if root is not None else None
    except OSError:
        return None
    return int(node.value) if node is not None and is_integer(node) else None


def monitor_work(number: int | None) -> tuple[list[int], dict | None]:
    """Рабочая область монитора с этим номером; такого нет (или номер не задан) — основного. (область, монитор или None)."""
    items = monitors()
    chosen = next((item for item in items if item["number"] == number), None)
    if chosen is None:
        return work_area(), None
    return chosen["work"], chosen


def describe_monitors(number: int | None) -> list[str]:
    """Строки для консоли: какие мониторы есть и куда пойдут окна."""
    items = monitors()
    lines = []
    for item in items:
        mark = " (основной)" if item["primary"] else ""
        if item["number"] == number:
            mark += " ← окна здесь"
        lines.append(f"  монитор {item['number']}: {item['size'][0]}×{item['size'][1]}{mark}")
    if number is not None and all(item["number"] != number for item in items):
        lines.append(f"  монитора {number} нет — окна на основном")
    return lines


def window_rect(hwnd) -> wintypes.RECT:
    rect = wintypes.RECT()
    user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect))
    return rect


def visible_rect(hwnd) -> wintypes.RECT:
    """Видимые края окна: без невидимых рамок изменения размера (DWMWA_EXTENDED_FRAME_BOUNDS)."""
    rect = wintypes.RECT()
    if dwmapi.DwmGetWindowAttribute(ctypes.c_void_p(hwnd), 9, ctypes.byref(rect), ctypes.sizeof(rect)) != 0:
        return window_rect(hwnd)
    return rect


def client_size(hwnd) -> tuple[int, int]:
    rect = wintypes.RECT()
    user32.GetClientRect(ctypes.c_void_p(hwnd), ctypes.byref(rect))
    return rect.right, rect.bottom


def place(hwnd, x: int, y: int, width: int | None = None, height: int | None = None) -> None:
    """Поставить окно так, чтобы его ВИДИМЫЙ левый верхний угол был в (x, y); с width/height — ещё и видимый размер.
    Свёрнутое или развёрнутое на весь экран окно сначала восстанавливается (без перехвата фокуса)."""
    handle = ctypes.c_void_p(hwnd)
    if user32.IsIconic(handle) or user32.IsZoomed(handle):
        user32.ShowWindow(handle, 4)                                                       # SW_SHOWNOACTIVATE
    outer, seen = window_rect(hwnd), visible_rect(hwnd)
    left, top = seen.left - outer.left, seen.top - outer.top
    right, bottom = outer.right - seen.right, outer.bottom - seen.bottom
    flags = 0x0004 | 0x0010                                                              # SWP_NOZORDER | SWP_NOACTIVATE
    if width is None or height is None:
        user32.SetWindowPos(handle, None, x - left, y - top, 0, 0, flags | 0x0001)        # SWP_NOSIZE
    else:
        user32.SetWindowPos(handle, None, x - left, y - top, width + left + right, height + top + bottom, flags)


def slot_position(state: dict, slot: int, width: int, height: int) -> tuple[int, int]:
    """Видимый левый верхний угол окна размером width×height на месте slot (раскладка из state)."""
    left, top, right, bottom = state["monitor"]
    margin = int(state.get("margin", 20))                 # от краёв экрана
    gap = int(state.get("gap", margin))                   # между окнами
    # Сколько окон влезает в столбец: margin + rows·height + (rows − 1)·gap + margin <= высоты экрана.
    rows = max(1, (bottom - top - 2 * margin + gap) // (height + gap))
    columns = 1 if state.get("mode") == "center" else 2
    per_round = rows * columns
    # Первый круг — все места по порядку. Дальше место 0 (главное окно) пропускается: круг — места 1…per_round−1.
    # В столбец влезает одно окно — пропускать нельзя (в режиме center мест бы не осталось вовсе).
    if slot < per_round or rows < 2:
        position = slot % per_round
    else:
        position = (slot - per_round) % (per_round - 1) + 1
    column, row = divmod(position, rows)
    if columns == 1:
        x = left + max(0, (right - left - width) // 2)
    else:
        x = left + margin if column == 0 else max(left + margin, right - margin - width)
    return x, top + margin + row * (height + gap)


# ---------- общее состояние logs\windows.json ----------

@contextlib.contextmanager
def locked():
    """Мьютекс на чтение-изменение-запись состояния: два окна не займут одно место."""
    handle = kernel32.CreateMutexW(None, False, MUTEX)
    owned = bool(handle) and kernel32.WaitForSingleObject(ctypes.c_void_p(handle), 5000) in (0, 0x80)   # WAIT_OBJECT_0 / ABANDONED
    try:
        yield
    finally:
        if owned:
            kernel32.ReleaseMutex(ctypes.c_void_p(handle))
        if handle:
            kernel32.CloseHandle(ctypes.c_void_p(handle))


def read_state() -> dict:
    # Файл могут как раз заменять — несколько коротких попыток.
    for _ in range(5):
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            time.sleep(0.05)
    return {}


def write_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE.with_name(f"{STATE.stem}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    for attempt in range(20):
        try:
            temporary.replace(STATE)
            return
        except PermissionError:
            # Файл сейчас читает другое окно — через мгновение освободится.
            time.sleep(0.05)
    temporary.unlink(missing_ok=True)


def new_state(config: dict, monitor: list[int], session: int) -> dict:
    return {
        "session": session, "arrangement": 1, "mode": config["arrangements"]["1"]["mode"],
        "margin": config["arrangements"]["1"]["margin"], "gap": config["arrangements"]["1"]["gap"], "monitor": monitor,
        "setup": 1, "setup_values": config["setups"]["1"], "generation": 1, "action": "arrange",
        "next_slot": 0, "windows": {}, "frame": list(DEFAULT_FRAME),
    }


def prune(state: dict) -> None:
    """Убрать из списка закрытые окна (процесс окна завершился)."""
    from engine import winproc

    state["windows"] = {pid: entry for pid, entry in state.get("windows", {}).items() if winproc.pid_alive(pid)}


def current_state() -> dict:
    """Состояние для изменения (уже под locked()): если главное окно, начавшее раскладку, закрыто — новое по live.json."""
    from engine import winproc

    state = read_state()
    session = state.get("session")
    if not state or (session and not winproc.pid_alive(session)):
        config, _ = read_config()
        state = new_state(config, monitor_work(start_monitor())[0], 0)
    return state


def start_session(main_pid: int, configs: Path | None = None) -> tuple[dict, list[str]]:
    """Старт главного окна: новая раскладка по live.json (сетап 1, режим 1) на мониторе "monitor" из config.json
    (нет такого монитора или номер не задан — на основном); главное окно — первое (место 0)."""
    config, errors = read_config()
    state = new_state(config, monitor_work(start_monitor(configs))[0], main_pid)
    state["windows"][str(main_pid)] = {"kind": "main", "title": "anime-sort", "opened": time.time(), "slot": 0}
    state["next_slot"] = 1
    with locked():
        write_state(state)
    return state, errors


def register(pid: int, title: str) -> tuple[dict, int]:
    """Новое окно набора: следующее свободное место по порядку открытия."""
    with locked():
        state = current_state()
        prune(state)
        slot = int(state.get("next_slot", 0))
        state["next_slot"] = slot + 1
        state["windows"][str(pid)] = {"kind": "gui", "title": title, "opened": time.time(), "slot": slot}
        write_state(state)
    return state, slot


def choose_setup(number: int, config: dict) -> dict:
    """Кнопка «Сетап N»: размеры и шрифты этого сетапа (из live.json на момент нажатия) — всем окнам, и сразу
    действующий режим: окна заново встают по местам (по порядку открытия) на мониторе раскладки — так после
    сетапа и место, и размер снова «правильные»."""
    with locked():
        state = current_state()
        prune(state)
        ordered = sorted(state["windows"].values(), key=lambda entry: entry.get("opened", 0))
        for slot, entry in enumerate(ordered):
            entry["slot"] = slot
        state["setup"] = number
        state["setup_values"] = config["setups"][str(number)]
        state["next_slot"] = len(ordered)
        state["generation"] = int(state.get("generation", 0)) + 1
        state["action"] = "arrange"
        write_state(state)
    return state


def arrange(number: int, config: dict, monitor: list[int]) -> dict:
    """Кнопка «Режим N»: режим и отступ расстановки N из live.json на момент нажатия, монитор — окна, где нажали.
    Все открытые окна получают места заново по порядку открытия; новые окна пойдут следом."""
    with locked():
        state = current_state()
        prune(state)
        ordered = sorted(state["windows"].values(), key=lambda entry: entry.get("opened", 0))
        for slot, entry in enumerate(ordered):
            entry["slot"] = slot
        chosen = config["arrangements"][str(number)]
        state.update(arrangement=number, mode=chosen["mode"], margin=chosen["margin"], gap=chosen["gap"], monitor=monitor,
                     next_slot=len(ordered), generation=int(state.get("generation", 0)) + 1, action="arrange")
        write_state(state)
    return state


def next_monitor() -> tuple[dict, dict | None]:
    """Кнопка «Монитор ›»: все окна — на следующий монитор справа (по расположению: слева направо, при равенстве —
    сверху вниз; после самого правого — самый левый) с той же расстановкой и сетапом. Места — заново по порядку открытия.
    Номера Windows для этого не годятся: у пользователя 2 — слева, 1 — посередине, 3 — справа."""
    with locked():
        state = current_state()
        prune(state)
        items = sorted(monitors(), key=lambda item: (item["work"][0], item["work"][1]))
        if not items:
            return state, None
        index = next((i for i, item in enumerate(items) if item["work"] == list(state.get("monitor", []))), -1)
        target = items[(index + 1) % len(items)]
        ordered = sorted(state["windows"].values(), key=lambda entry: entry.get("opened", 0))
        for slot, entry in enumerate(ordered):
            entry["slot"] = slot
        state.update(monitor=target["work"], next_slot=len(ordered),
                     generation=int(state.get("generation", 0)) + 1, action="arrange")
        write_state(state)
    return state, target


def request_close_done() -> None:
    """Кнопка «Закрыть готовые»: просьба всем окнам — закрыться, если конвейер набора отработал.
    Каждое окно само знает, готов ли его набор (раз в секунду сверяет счётчик close_done)."""
    with locked():
        state = current_state()
        state["close_done"] = int(state.get("close_done", 0)) + 1
        write_state(state)


def layer_all(raised: bool) -> None:
    """Кнопки «Наверх» (raised=True) и «Вниз»: все окна anime-sort — над окнами других программ или под все окна."""
    with locked():
        state = current_state()
        prune(state)
        write_state(state)
    set_layer(state, raised)


def window_handles(state: dict) -> list[tuple[int, int]]:
    """(место, hwnd) всех окон раскладки — по pid их процессов (главное окно — процесс main.py)."""
    by_pid: dict[int, list[int]] = {}

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def callback(hwnd, _data):
        if user32.IsWindowVisible(ctypes.c_void_p(hwnd)) and not user32.GetWindow(ctypes.c_void_p(hwnd), 4):   # GW_OWNER
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(owner))
            by_pid.setdefault(owner.value, []).append(hwnd)
        return True

    user32.EnumWindows(callback, 0)
    result = []
    for pid, entry in state.get("windows", {}).items():
        slot = int(entry.get("slot", 0))
        if entry.get("hwnd"):
            result.append((slot, int(entry["hwnd"])))
        for hwnd in by_pid.get(int(pid), []):
            result.append((slot, hwnd))
    return result


def set_layer(state: dict, raised: bool) -> None:
    """Поднять все окна над остальными программами (главное — самым верхним) или спрятать под все окна.
    Поднять без перехвата фокуса: на миг «поверх всех» (TOPMOST) и сразу обратно — окно остаётся наверху обычного слоя."""
    flags = 0x0001 | 0x0002 | 0x0010                                                   # SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE
    handles = sorted(window_handles(state), reverse=raised)
    for _slot, hwnd in handles:
        handle = ctypes.c_void_p(hwnd)
        if raised:
            if user32.IsIconic(handle):
                user32.ShowWindow(handle, 4)                                               # SW_SHOWNOACTIVATE
            user32.SetWindowPos(handle, ctypes.c_void_p(-1), 0, 0, 0, 0, flags)            # HWND_TOPMOST
            user32.SetWindowPos(handle, ctypes.c_void_p(-2), 0, 0, 0, 0, flags)            # HWND_NOTOPMOST
        else:
            user32.SetWindowPos(handle, ctypes.c_void_p(1), 0, 0, 0, 0, flags)             # HWND_BOTTOM


def set_manual(pid: int, moved: bool, resized: bool) -> None:
    """Окно сдвинули или растянули вручную (не кнопками) — отметка для главного окна: его кнопки действующего
    режима (сдвиг) и сетапа (размер) становятся светло-серыми, пока снова не нажать режим или сетап."""
    with locked():
        state = read_state()
        entry = state.get("windows", {}).get(str(pid)) if state else None
        if entry is not None and (entry.get("moved"), entry.get("resized")) != (moved, resized):
            entry["moved"], entry["resized"] = moved, resized
            write_state(state)


def manual_flags(state: dict) -> tuple[bool, bool]:
    """(хоть одно живое окно сдвинуто, хоть одно растянуто) — по отметкам set_manual."""
    from engine import winproc

    moved = resized = False
    for pid, entry in state.get("windows", {}).items():
        if (entry.get("moved") or entry.get("resized")) and winproc.pid_alive(pid):
            moved = moved or bool(entry.get("moved"))
            resized = resized or bool(entry.get("resized"))
    return moved, resized


def save_frame(frame: tuple[int, int]) -> None:
    """Запомнить измеренные заголовок и рамку окна набора — следующие окна сразу откроются нужного размера."""
    with locked():
        state = read_state()
        if state and state.get("frame") != list(frame):
            state["frame"] = list(frame)
            write_state(state)
