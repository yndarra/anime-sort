r"""Окна anime-sort: общий каркас (BaseWindow) и окно наблюдения за одним набором (PipelineWindow).

BaseWindow — слева лог, справа таблица; размер, шрифты и место окна — из logs\windows.json (engine\winlayout.py):
сетап и режим расстановки. Его же использует главное окно (gui\main_window.py — журнал всех пачек и статистика).
PipelineWindow — окно набора: этапы, их подписи и ширины столбцов — из logs\layout.json набора (пишет
engine\dataset.py); под таблицей только «Остановить» и «Завершить». Кнопки окон (сетапы, режимы, слой,
«✕ Готовые», монитор) — только в главном окне.
Любая ошибка внутри окна пишется красной строкой в лог и не закрывает окно.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# Папка проекта anime-sort (окно лежит в anime-sort/gui).
PROJECT = ROOT.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(1, str(PROJECT))

FONT_FAMILY = "Consolas"
# Самые маленькие части окна, если сетап задан слишком тесно (иначе панели схлопнутся).
MIN_PANEL = 120
MIN_HEIGHT = 120
# Высота строки кнопок, в пикселях.
BUTTON_SIZE = 24
# Нажатая кнопка (действующий сетап/режим) — светло-серый акцент.
ACTIVE_SETUP_BG = "#7E7E7E"
# Действующий сетап/режим, но какое-то окно сдвинули или растянули вручную — чуть светлее обычной кнопки.
MANUAL_SETUP_BG = "#4A4A4A"
# Допуск в пикселях: меньшие отличия от места/размера раскладки — не ручное изменение.
MANUAL_TOLERANCE = 3
# Поля текста (padx=6 с двух сторон) и запас под вертикальную полосу прокрутки лога.
TEXT_PADDING = 16
SCROLLBAR = 18
POLL_MS = 350
TABLE_MS = 500

# Палитра консоли Windows (Campbell): так цвета совпадают со старыми окнами.
BACKGROUND = "#0C0C0C"
BUTTON_BG = "#2A2A2A"
BUTTON_ACTIVE = "#3E3E3E"
CONTROL_MS = 1000
# После «Продолжить» пачке нужно время, чтобы снова дойти до этого набора (пропуская готовые папки).
RESTART_GRACE = 120
FOREGROUND = "#CCCCCC"
COLORS = {"31": "#E74856", "32": "#16C60C", "33": "#F9F1A5", "36": "#61D6D6", "37": "#CCCCCC", "90": "#767676", "97": "#F2F2F2"}
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
SCROLL_TROUGH = "#0C0C0C"
SCROLL_THUMB = "#3A3A3A"
SCROLL_THUMB_ACTIVE = "#5A5A5A"
# Общий вид кнопок всех окон.
BUTTON_STYLE = dict(bg=BUTTON_BG, fg="#F2F2F2", activebackground=BUTTON_ACTIVE, activeforeground="#FFFFFF",
                    disabledforeground="#555555", relief="flat", bd=0, highlightthickness=0,
                    font=("Segoe UI", 9), pady=3, cursor="hand2")


# Буквы слова DONE: 7 строк по 5 «пикселей»; пиксель — два символа █ (символ вдвое выше своей ширины,
# так буквы выходят квадратными, а не вытянутыми).
DONE_GLYPHS = {
    "D": ["11110", "10001", "10001", "10001", "10001", "10001", "11110"],
    "O": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
    "N": ["10001", "11001", "11001", "10101", "10011", "10011", "10001"],
    "E": ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
}


def banner(word: str, scale: int = 2) -> list[str]:
    """Слово крупными буквами из сплошных блоков █ (буквы — из DONE_GLYPHS). Пиксель буквы — scale строк
    по 2·scale символов (символ вдвое выше своей ширины — пиксель квадратный); между буквами — один пиксель."""
    rows = []
    for row in range(7):
        parts = ["".join(("██" * scale) if bit == "1" else ("  " * scale) for bit in DONE_GLYPHS[letter][row])
                 for letter in word]
        line = ("  " * scale).join(parts).rstrip()
        rows.extend([line] * scale)
    return rows


def dark_title_bar(root) -> None:
    """Чёрный заголовок окна (Windows 11 DWM); на других системах молча ничего не делает."""
    try:
        import ctypes

        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
        dwm = ctypes.windll.dwmapi
        on = ctypes.c_int(1)
        dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(on), ctypes.sizeof(on))  # DWMWA_USE_IMMERSIVE_DARK_MODE
        black = ctypes.c_int(0x000C0C0C)  # COLORREF 0x00BBGGRR
        dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(black), ctypes.sizeof(black))  # DWMWA_CAPTION_COLOR
        text = ctypes.c_int(0x00CCCCCC)
        dwm.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(text), ctypes.sizeof(text))  # DWMWA_TEXT_COLOR
    except Exception as exc:
        crash_log(f"dark title bar failed: {exc}")


def ctypes_parent(root) -> int:
    """Настоящее окно Windows для Tk (рамка с заголовком — родитель внутреннего окна Tk)."""
    import ctypes

    user32 = ctypes.windll.user32
    user32.GetParent.restype = ctypes.c_void_p
    return user32.GetParent(ctypes.c_void_p(root.winfo_id())) or root.winfo_id()


def crash_log(text: str) -> None:
    try:
        with (PROJECT / "logs" / "pipeline_gui_errors.log").open("a", encoding="utf-8") as handle:
            handle.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {text}\n")
    except OSError:
        pass


class BaseWindow:
    """Каркас окна anime-sort: слева лог, справа таблица; сетапы (размер, шрифты) и место в раскладке окон.
    Наследник до вызова __init__ задаёт log_chars и table_chars — ширины лога и таблицы в символах
    (для "auto" в сетапе), а после — свои кнопки и опросы."""

    log_chars = 120
    table_chars = 60

    def __init__(self, tk, title: str, state: dict, slot: int, position: tuple[int, int] | None = None):
        from engine import winlayout

        self.tk = tk
        self.layout = winlayout
        self.slot = slot
        self.position = position
        self.generation = state.get("generation")
        self.current_setup = int(state.get("setup", 1))
        self.current_arrangement = int(state.get("arrangement", 1))
        self.frame = tuple(state.get("frame") or winlayout.DEFAULT_FRAME)
        self.visible_size = (0, 0)

        self.root = tk.Tk()
        self.root.title(title)
        from tkinter import font as tkfont

        self.tkfont = tkfont
        values = state["setup_values"]
        # Шрифты — именованные: смена сетапа меняет их размер, и текст перерисовывается сам.
        self.log_font = tkfont.Font(root=self.root, family=FONT_FAMILY, size=int(values["log_font"]))
        self.table_font = tkfont.Font(root=self.root, family=FONT_FAMILY, size=int(values["table_font"]))
        client_w, client_h, table_width = self.compute_size(values, state["monitor"])
        log_width = client_w - table_width
        self.visible_size = (client_w + self.frame[0], client_h + self.frame[1])
        # Где и какого размера окно должно быть по раскладке (видимый прямоугольник: x, y, ширина, высота) —
        # запоминается после каждого применения сетапа/режима. Отличается — окно сдвинули (moved) или растянули (resized).
        self.expected_rect: tuple[int, int, int, int] | None = None
        self.moved = self.resized = False
        # Пока раскладка сама двигает окно, события <Configure> не считаются ручными.
        self.layout_busy_until = time.time() + 30
        if position is not None:
            where = f"+{position[0]}+{position[1]}"
        else:
            # Первая прикидка места (невидимая рамка слева — примерно 7 пикселей); точно — в run(), когда окно видно.
            x, y = winlayout.slot_position(state, self.slot, *self.visible_size)
            where = f"+{x - winlayout.DEFAULT_LEFT_BORDER}+{y}"
        self.root.geometry(f"{client_w}x{client_h}{where}")
        self.root.configure(bg=BACKGROUND)
        self.root.report_callback_exception = self.on_tk_error
        self.setup_scroll_style()
        dark_title_bar(self.root)

        # Лог — таблица с переносами внутри столбцов, горизонтальная прокрутка не нужна.
        self.log_text = self.make_panel(0, log_width, client_h, font=self.log_font, scroll_x=False)
        # Таблица целиком помещается в окно — полосы прокрутки не нужны.
        self.table_text = self.make_panel(1, table_width, client_h, font=self.table_font, scroll_x=False, scroll_y=False)
        self.log_frame, self.table_frame = self.log_text.master, self.table_text.master
        self.root.grid_rowconfigure(0, weight=1)
        # Растягивание окна мышью достаётся логу, таблица сохраняет ширину из сетапа.
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_columnconfigure(1, weight=0)
        self.root.bind("<Configure>", self.on_resize)

    def setup_scroll_style(self) -> None:
        # Нативные полосы прокрутки Windows не красятся, поэтому ttk-тема clam с тёмными цветами.
        from tkinter import ttk

        style = ttk.Style(self.root)
        style.theme_use("clam")
        for orient in ("Vertical", "Horizontal"):
            name = f"Dark.{orient}.TScrollbar"
            style.configure(
                name,
                background=SCROLL_THUMB,
                troughcolor=SCROLL_TROUGH,
                bordercolor=SCROLL_TROUGH,
                lightcolor=SCROLL_THUMB,
                darkcolor=SCROLL_THUMB,
                arrowcolor=FOREGROUND,
                gripcount=0,
                relief="flat",
            )
            style.map(name, background=[("active", SCROLL_THUMB_ACTIVE), ("pressed", SCROLL_THUMB_ACTIVE)])

    def make_panel(self, column: int, width: int, height: int, font, scroll_x: bool, scroll_y: bool = True):
        from tkinter import ttk

        tk = self.tk
        frame = tk.Frame(self.root, width=width, height=height, bg=BACKGROUND)
        frame.grid(row=0, column=column, sticky="nsew")
        frame.grid_propagate(False)
        frame.grid_rowconfigure(0, weight=1)
        frame.grid_columnconfigure(0, weight=1)
        text = tk.Text(
            frame,
            bg=BACKGROUND,
            fg=FOREGROUND,
            insertbackground=FOREGROUND,
            font=font,
            wrap="none",
            borderwidth=0,
            highlightthickness=0,
            padx=6,
            pady=4,
        )
        text.grid(row=0, column=0, sticky="nsew")
        if scroll_y:
            bar_y = ttk.Scrollbar(frame, orient="vertical", command=text.yview, style="Dark.Vertical.TScrollbar")
            bar_y.grid(row=0, column=1, sticky="ns")
            text.configure(yscrollcommand=bar_y.set)
        if scroll_x:
            bar = ttk.Scrollbar(frame, orient="horizontal", command=text.xview, style="Dark.Horizontal.TScrollbar")
            bar.grid(row=1, column=0, sticky="ew")
            text.configure(xscrollcommand=bar.set)
        for code, color in COLORS.items():
            text.tag_configure(code, foreground=color)
        # Выделение должно оставаться видимым поверх цветных строк.
        text.tag_configure("sel", background="#264F78", foreground="#FFFFFF")
        text.tag_raise("sel")
        self.make_read_only(text)
        return text

    # ---------- размер, шрифты и место окна (сетапы и расстановка) ----------

    def hwnd(self) -> int:
        return ctypes_parent(self.root)

    def char_width(self, size: int) -> int:
        return self.tkfont.Font(root=self.root, family=FONT_FAMILY, size=size).measure("0")

    def compute_size(self, values: dict, monitor: list[int]) -> tuple[int, int, int]:
        """(ширина и высота клиентской части, ширина таблицы) в пикселях по сетапу.
        Сетап задаёт ВИДИМОЕ окно (с заголовком); клиентская часть = видимое окно − self.frame.
        "auto" — по столбцам лога и таблицы из config.json при шрифте этого сетапа."""
        frame_w, frame_h = self.frame
        screen_w, screen_h = monitor[2] - monitor[0], monitor[3] - monitor[1]
        log_auto = self.char_width(int(values["log_font"])) * (self.log_chars + 1) + TEXT_PADDING + SCROLLBAR
        table_auto = self.char_width(int(values["table_font"])) * (self.table_chars + 1) + TEXT_PADDING
        visible_w = self.layout.pixels(values["width"], screen_w)
        auto_width = visible_w is None
        if auto_width:
            visible_w = log_auto + table_auto + frame_w
        table = self.layout.pixels(values["table_width"], visible_w)    # проценты — от ширины окна
        if table is None:
            table = table_auto
        client_w = log_auto + table if auto_width else visible_w - frame_w
        client_w = max(client_w, 2 * MIN_PANEL)
        table = max(MIN_PANEL, min(table, client_w - MIN_PANEL))
        client_h = max(MIN_HEIGHT, self.layout.pixels(values["height"], screen_h) - frame_h)
        return client_w, client_h, table

    def apply_setup(self, values: dict, monitor: list[int]) -> None:
        self.log_font.configure(size=int(values["log_font"]))
        self.table_font.configure(size=int(values["table_font"]))
        client_w, client_h, table = self.compute_size(values, monitor)
        self.log_frame.configure(width=client_w - table, height=client_h)
        self.table_frame.configure(width=table, height=client_h)
        self.root.geometry(f"{client_w}x{client_h}")
        self.visible_size = (client_w + self.frame[0], client_h + self.frame[1])
        self.root.update_idletasks()
        self.paint_setup_buttons()

    def current_rect(self) -> tuple[int, int, int, int]:
        rect = self.layout.visible_rect(self.hwnd())
        return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top

    def layout_applied(self) -> None:
        """Сетап или режим только что применён: через мгновение (окно успеет встать) запомнить место и размер
        как «правильные» и снять отметки ручного изменения."""
        self.layout_busy_until = time.time() + 1.5
        self.root.after(900, self.capture_expected)

    def capture_expected(self) -> None:
        try:
            self.expected_rect = self.current_rect()
            self.set_manual(False, False)
        except Exception as exc:
            self.report(exc)

    def on_resize(self, event) -> None:
        """<Configure> окна (сдвиг, растягивание, разворачивание): сравнить с местом и размером раскладки.
        Свёрнутое окно не считается — после разворачивания оно там же и того же размера."""
        try:
            if event.widget is not self.root or self.expected_rect is None or time.time() < self.layout_busy_until:
                return
            if self.root.state() == "iconic":
                return
            x, y, width, height = self.current_rect()
            ex, ey, ewidth, eheight = self.expected_rect
            moved = abs(x - ex) > MANUAL_TOLERANCE or abs(y - ey) > MANUAL_TOLERANCE
            resized = abs(width - ewidth) > MANUAL_TOLERANCE or abs(height - eheight) > MANUAL_TOLERANCE
            self.set_manual(moved, resized)
        except Exception as exc:
            self.report(exc)

    def set_manual(self, moved: bool, resized: bool) -> None:
        if (moved, resized) == (self.moved, self.resized):
            return
        self.moved, self.resized = moved, resized
        self.layout.set_manual(os.getpid(), moved, resized)
        self.paint_setup_buttons()

    def settle_window(self) -> None:
        """Окно уже показано: измерить настоящие заголовок и рамку, поправить размер и точно встать на своё место."""
        layout = self.layout
        hwnd = self.hwnd()
        seen = layout.visible_rect(hwnd)
        client_w, client_h = layout.client_size(hwnd)
        frame = (seen.right - seen.left - client_w, seen.bottom - seen.top - client_h)
        state = layout.read_state()
        if frame != self.frame and 0 <= frame[0] < 100 and 0 <= frame[1] < 200:
            self.frame = frame
            layout.save_frame(frame)
        if state.get("setup_values"):
            self.apply_setup(state["setup_values"], state["monitor"])
        if self.position is None and state.get("monitor"):
            layout.place(hwnd, *layout.slot_position(state, self.slot, *self.visible_size))
        self.layout_applied()

    def poll_layout(self) -> bool:
        """Кто-то нажал «Сетап N», «Режим N» или «Монитор ›» в главном окне — применить к этому окну.
        True — окно закрылось (у окна набора: его набор готов, а в главном окне нажали «✕ Готовые»)."""
        layout = self.layout
        state = layout.read_state()
        if not state or self.before_layout(state):
            return bool(state)
        generation = state.get("generation")
        if generation == self.generation:
            return False
        self.generation = generation
        self.current_setup = int(state.get("setup", 1))
        self.current_arrangement = int(state.get("arrangement", 1))
        self.paint_setup_buttons()
        entry = state.get("windows", {}).get(str(os.getpid()))
        move = state.get("action") == "arrange" and entry is not None
        if move:
            self.slot = int(entry["slot"])
        # Проценты сетапа — от монитора, где окно окажется: при расстановке — её монитор, иначе — текущий.
        monitor = state["monitor"] if move else layout.work_area(self.hwnd())
        self.apply_setup(state["setup_values"], monitor)
        if move:
            layout.place(self.hwnd(), *layout.slot_position(state, self.slot, *self.visible_size))
        self.layout_applied()
        return False

    def before_layout(self, state: dict) -> bool:
        """Перед сетапом/расстановкой: True — окно закрылось и дальше ничего делать не нужно."""
        return False

    def paint_setup_buttons(self) -> None:
        """Подсветка действующих сетапа и режима — есть только у главного окна."""

    # ---------- только чтение + копирование ----------

    def make_read_only(self, text) -> None:
        """Текст остаётся в state=normal (в disabled Tk не даёт копировать),
        а любые правки с клавиатуры и мыши блокируются."""
        allowed = {"Left", "Right", "Up", "Down", "Home", "End", "Prior", "Next"}

        def on_key(event):
            ctrl = bool(event.state & 0x4)
            # keycode, а не keysym: так Ctrl+C / Ctrl+A работают и в русской раскладке.
            if ctrl and event.keycode == 67:
                return self.copy_selection(text)
            if ctrl and event.keycode == 65:
                text.tag_add("sel", "1.0", "end-1c")
                return "break"
            if event.keysym in allowed:
                return None
            return "break"

        text.bind("<Key>", on_key)
        for sequence in ("<<Paste>>", "<<Cut>>", "<<Clear>>", "<Button-2>", "<<PasteSelection>>"):
            text.bind(sequence, lambda _event: "break")
        menu = self.tk.Menu(text, tearoff=0)
        menu.add_command(label="Копировать", command=lambda: self.copy_selection(text))
        menu.add_command(label="Выделить всё", command=lambda: text.tag_add("sel", "1.0", "end-1c"))

        def on_menu(event):
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()
            return "break"

        text.bind("<Button-3>", on_menu)

    def copy_selection(self, text) -> str:
        try:
            if text.tag_ranges("sel"):
                selected = text.get("sel.first", "sel.last")
                self.root.clipboard_clear()
                self.root.clipboard_append(selected)
        except Exception as exc:
            self.report(exc)
        return "break"

    def at_bottom(self) -> bool:
        # Пока окно не показано, yview ещё не знает размеров — считаем, что мы внизу.
        text = self.log_text
        return not text.winfo_viewable() or text.yview()[1] >= 0.999

    # ---------- ошибки ----------

    def report(self, exc: BaseException) -> None:
        details = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).strip()
        crash_log(details)
        try:
            self.show_error(f"сбой окна: {exc}", type(exc).__name__)
        except Exception:
            pass

    def show_error(self, message: str, kind: str) -> None:
        """Красная строка в лог этого окна (у окна набора и главного окна — свой формат лога)."""

    def on_tk_error(self, exc_type, exc, tb) -> None:
        self.report(exc)


class PipelineWindow(BaseWindow):
    def __init__(self, tk, args: argparse.Namespace):
        import logformat

        self.fmt = logformat
        self.formatter = logformat.Formatter(logformat.read_json(args.layout))
        # Ширины в символах для "auto" — по столбцам лога и таблицы из config.json.
        self.log_chars, self.table_chars = self.formatter.log_width, self.formatter.table_width
        self.args = args
        self.log_path: Path = args.log
        self.snapshot_path: Path | None = args.snapshot
        self.offset = 0
        self.remainder = b""
        self.events: list[dict] = []
        self.frozen = False
        self.last_run_start = ""
        self.table_dirty = True
        # Идёт ли сейчас этап (было «начало этапа», ещё не было «конец этапа»).
        self.stage_open = False
        self.completed = False
        self.restarting_until = 0.0

        # Место в раскладке окон: сетап (размеры, шрифты), режим, монитор и номер места этого окна.
        from engine import winlayout

        state, slot = winlayout.register(os.getpid(), args.title)
        # Просьбы «✕ Готовые», сделанные до открытия этого окна, его не касаются.
        self.close_seen = int(state.get("close_done", 0))
        position = (args.x, args.y) if args.x is not None and args.y is not None else None
        super().__init__(tk, args.title, state, slot, position)
        self.make_buttons()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        if args.capture_log:
            try:
                args.capture_log.parent.mkdir(parents=True, exist_ok=True)
                args.capture_log.write_text("", encoding="utf-8")
            except OSError as exc:
                crash_log(f"capture reset failed: {exc}")

    def before_layout(self, state: dict) -> bool:
        """«✕ Готовые» в главном окне: окно закрывается, если конвейер этого набора отработал."""
        request = int(state.get("close_done", 0))
        if request != self.close_seen:
            self.close_seen = request
            # «Лишние ✕»: закрываются все окна, над которыми конвейер сейчас не работает
            # (набор готов, остановлен или упал); окна работающих наборов остаются.
            if not self.active() and time.time() >= self.restarting_until:
                self.root.destroy()
                return True
        return False

    def show_error(self, message: str, kind: str) -> None:
        event = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "stage": "GUI"}
        for line, code in self.formatter.error_rows(event, "ошибка", message, kind):
            self.append_log(line, code)

    # ---------- лог ----------

    def append_log(self, line: str, code: str | None) -> None:
        text = self.log_text
        text.insert("end", line + "\n", (code,) if code else ())
        if self.args.capture_log:
            try:
                with self.args.capture_log.open("a", encoding="utf-8") as handle:
                    handle.write(ANSI_RE.sub("", line) + "\n")
            except OSError:
                pass

    def handle_event(self, event: dict) -> None:
        # Форматирование целиком в logformat: событие -> строки таблицы (с переносами внутри столбцов).
        message = str(event.get("message", ""))
        run_started = event.get("stage") == "PIPELINE" and message.startswith(self.fmt.RUN_STARTED_PREFIX)
        rendered = self.formatter.format_event(event)
        if run_started and self.stage_open:
            # Прошлый запуск оборвался посреди этапа — «запуск» идёт сразу, без пустой строки.
            rendered = [row for row in rendered if row[0]]
        if message in (self.fmt.MSG_STARTED, self.fmt.MSG_RESUMED):
            self.stage_open = True
        elif message in (self.fmt.MSG_FINISHED, self.fmt.MSG_UNCHANGED) or (
                event.get("stage") == "PIPELINE" and message not in (self.fmt.MSG_PAUSED, self.fmt.MSG_CONTINUED)):
            self.stage_open = False
        for line, code in rendered:
            # Пустая строка-разделитель в самом начале окна не нужна.
            if not line and not self.log_text.get("1.0", "end-1c").strip():
                continue
            self.append_log(line, code)

    def read_new_lines(self) -> list[str]:
        if not self.log_path.exists():
            return []
        size = self.log_path.stat().st_size
        if size < self.offset:
            # Лог перезаписан (новый прогон или сброс) — перечитываем с начала.
            self.offset, self.remainder, self.events = 0, b"", []
            self.stage_open = False
            self.log_text.delete("1.0", "end")
        if size == self.offset:
            return []
        with self.log_path.open("rb") as handle:
            handle.seek(self.offset)
            chunk = handle.read(size - self.offset)
        self.offset = size
        data = self.remainder + chunk
        *lines, self.remainder = data.split(b"\n")
        return [line.decode("utf-8", errors="replace") for line in lines if line.strip()]

    def poll_log(self) -> None:
        try:
            follow = self.at_bottom()
            lines = self.read_new_lines()
            for raw in lines:
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                self.events.append(event)
                self.table_dirty = True
                if event.get("stage") == "PIPELINE" and str(event.get("message", "")).startswith(self.fmt.RUN_STARTED_PREFIX):
                    self.on_run_started(event)
                self.handle_event(event)
            if lines and follow:
                self.log_text.see("end")
        except Exception as exc:
            self.report(exc)
        finally:
            self.root.after(POLL_MS, self.poll_log)

    def on_run_started(self, event: dict) -> None:
        """Новый запуск набора (в т.ч. повторное открытие завершённого после смены конфига):
        перечитать этапы и ширины столбцов из layout.json и снова следить за таблицей."""
        self.last_run_start = str(event.get("time", ""))
        self.formatter = self.fmt.Formatter(self.fmt.read_json(self.args.layout))
        if self.frozen:
            self.frozen = False
            self.root.title(self.args.title)

    # ---------- таблица ----------

    def poll_table(self) -> None:
        try:
            if not self.frozen:
                snapshot = self.fmt.read_json(self.snapshot_path)
                # Снапшот прошлого завершения не считается, если после него начался новый запуск.
                completed = (snapshot.get("status") == "completed"
                             and str(snapshot.get("completed_at", "")) >= self.last_run_start)
                # Пока в таблице что-то выделено, не перерисовываем — иначе выделение пропадёт.
                selecting = bool(self.table_text.tag_ranges("sel"))
                if (self.table_dirty or completed) and not selecting:
                    self.render_table(snapshot)
                    self.table_dirty = False
                self.completed = completed
                if completed and not self.table_dirty:
                    self.frozen = True
                    self.root.title(f"{self.args.title} — готово")
                    self.show_done()
        except Exception as exc:
            self.report(exc)
        finally:
            self.root.after(TABLE_MS, self.poll_table)

    def show_done(self) -> None:
        """Набор отработал (пройдены все этапы) — крупное белое DONE посередине лога."""
        char = self.log_font.measure("0") or 7
        columns = max(0, self.log_text.winfo_width() - TEXT_PADDING - SCROLLBAR) // char
        rows = banner("DONE")
        indent = " " * max(0, (columns - max(len(row) for row in rows)) // 2)
        follow = self.at_bottom()
        text = self.log_text
        # █ в Consolas ниже высоты строки — между строками оставались бы тёмные полосы. Фон блоков того же цвета,
        # что и сам символ, закрывает строку целиком: буквы выходят сплошными (и копируются как █).
        text.tag_configure("done", foreground="#F2F2F2", background="#F2F2F2")
        self.append_log("", None)
        self.append_log("", None)
        self.append_log("", None)
        for row in rows:
            text.insert("end", indent)
            for run in re.finditer(r"█+| +", row):
                text.insert("end", run.group(), ("done",) if run.group()[0] == "█" else ())
            text.insert("end", "\n")
            if self.args.capture_log:
                try:
                    with self.args.capture_log.open("a", encoding="utf-8") as handle:
                        handle.write(indent + row + "\n")
                except OSError:
                    pass
        self.append_log("", None)
        self.append_log("", None)
        self.append_log("", None)
        if follow:
            self.log_text.see("end")

    def render_table(self, snapshot: dict) -> None:
        # Все события, включая [LINE] «Начало работы»: по нему таблица сбрасывается на текущий запуск.
        records = self.formatter.from_events(self.events)
        self.back_allowed = self.formatter.can_go_back(records)
        self.stage_providers = self.formatter.providers.get(self.formatter.current_stage(records) or "", [])
        self.switch_allowed = len(self.stage_providers) > 1
        total = snapshot.get("total") or max((int(r.get("expected") or 0) for r in records.values()), default=0)
        lines = self.formatter.table_lines(records, total)
        text = self.table_text
        text.delete("1.0", "end")
        for line, code in lines:
            text.insert("end", line + "\n", (code,) if code else ())
        if self.args.capture_table:
            try:
                self.args.capture_table.write_text("\n".join(line for line, _ in lines) + "\n", encoding="utf-8")
            except OSError:
                pass

    # ---------- управление конвейером: «Остановить»/«Продолжить», «Завершить», закрытие окна ----------

    def make_buttons(self) -> None:
        """Под таблицей — только [←] [→] [Остановить] [Завершить] [↻] [↔]; кнопки окон — в главном окне.
        Маленькие квадратные кнопки действуют только на ЭТОТ набор:
        → — пропустить текущий этап (полезно, если этап заглох);
        ← — вернуться к предыдущему этапу: он доделывает файлы, которых не касались ни он, ни текущий этап, потом
            текущий продолжается. Серая, если предыдущий этап пройден полностью (или текущий — первый);
        ↻ — проверить API этапа сейчас, не дожидаясь конца паузы (провайдер заглох, а баланс уже пополнили);
        ↔ — принудительно перейти к следующему провайдеру из списка этапа. Серая, если провайдер у этапа один.
        Отступы между кнопками — как у кнопок главного окна (по 2 пикселя с каждой стороны)."""
        tk = self.tk
        frame = tk.Frame(self.table_text.master, bg=BACKGROUND, height=BUTTON_SIZE)
        frame.grid(row=1, column=0, sticky="ew", padx=6, pady=(0, 6))
        frame.grid_propagate(False)
        frame.grid_rowconfigure(0, weight=1)
        for column in (0, 1, 4, 5):
            frame.grid_columnconfigure(column, weight=0, minsize=BUTTON_SIZE)
        for column in (2, 3):
            frame.grid_columnconfigure(column, weight=1, uniform="main")
        buttons = [("back_button", "←", self.on_back), ("skip_button", "→", self.on_skip),
                   ("pause_button", "Остановить", self.on_pause), ("stop_button", "Завершить", self.on_stop),
                   ("retry_button", "↻", self.on_retry), ("switch_button", "↔", self.on_switch)]
        for column, (name, text, command) in enumerate(buttons):
            button = tk.Button(frame, text=text, command=command, **BUTTON_STYLE)
            button.grid(row=0, column=column, sticky="nsew",
                        padx=(0 if column == 0 else 2, 0 if column == len(buttons) - 1 else 2))
            setattr(self, name, button)

    def control_path(self) -> Path:
        return self.log_path.parent / "control.json"

    def pids(self) -> dict:
        return self.fmt.read_json(self.log_path.parent / "pids.json")

    def active(self) -> bool:
        """Над набором сейчас работает конвейер (жив процесс engine/dataset.py этого набора)."""
        from engine import winproc

        return winproc.pid_alive(self.pids().get("dataset"))

    def paused(self) -> bool:
        return bool(self.fmt.read_json(self.control_path()).get("paused"))

    def set_control(self, **values) -> None:
        """Поменять поля logs\\control.json, не трогая остальные (пауза и запрос пропуска живут вместе)."""
        path = self.control_path()
        control = {**self.fmt.read_json(path), **values}
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(control), encoding="utf-8")
        temporary.replace(path)

    def set_paused(self, value: bool) -> None:
        self.set_control(paused=value)

    def skip_pending(self) -> bool:
        """Запрос кнопки «→» или «←» ещё не выполнен конвейером."""
        control = self.fmt.read_json(self.control_path())
        return bool(control.get("skip") or control.get("back"))

    def request_pending(self, name: str) -> bool:
        return bool(self.fmt.read_json(self.control_path()).get(name))

    def on_retry(self) -> None:
        """«↻»: разбудить уснувшие API текущего этапа этого набора — следующий файл сразу их проверит."""
        try:
            if self.active() and not self.request_pending("retry"):
                self.set_control(retry=time.strftime("%Y-%m-%d %H:%M:%S"))
                self.batch_note("проверить API сейчас (кнопка «↻» в окне)")
            self.refresh_buttons()
        except Exception as exc:
            self.report(exc)

    def on_switch(self) -> None:
        """«↔»: уйти с текущего провайдера этапа на следующего из его списка."""
        try:
            if self.active() and not self.request_pending("switch") and getattr(self, "switch_allowed", False):
                self.set_control(switch=time.strftime("%Y-%m-%d %H:%M:%S"))
                self.batch_note("смена провайдера (кнопка «↔» в окне)")
            self.refresh_buttons()
        except Exception as exc:
            self.report(exc)

    def on_back(self) -> None:
        """«←»: вернуться к предыдущему этапу ЭТОГО набора (другие наборы не затрагиваются)."""
        try:
            if self.active() and not self.skip_pending() and getattr(self, "back_allowed", False):
                self.set_control(back=time.strftime("%Y-%m-%d %H:%M:%S"))
                self.batch_note("возврат к предыдущему этапу (кнопка «←» в окне)")
            self.refresh_buttons()
        except Exception as exc:
            self.report(exc)

    def on_skip(self) -> None:
        """«→»: пропустить этап, на котором сейчас стоит конвейер ЭТОГО набора (другие наборы не затрагиваются).
        Конвейер замечает запрос между файлами, во время ожидания уснувших API и во время паузы."""
        try:
            if self.active() and not self.skip_pending():
                self.set_control(skip=time.strftime("%Y-%m-%d %H:%M:%S"))
                self.batch_note("пропуск этапа (кнопка «→» в окне)")
            self.refresh_buttons()
        except Exception as exc:
            self.report(exc)

    def batch_note(self, text: str) -> None:
        """Строка в журнал пачки — её показывает главное окно anime-sort."""
        pids = self.pids()
        try:
            with Path(pids["batch_log"]).open("a", encoding="utf-8") as handle:
                handle.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {pids.get('folder', '')}: {text}\n")
        except (KeyError, OSError):
            pass

    def dataset_note(self, message: str) -> None:
        event = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "stage": "PIPELINE", "message": message}
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def refresh_buttons(self) -> None:
        active = self.active()
        if active:
            self.restarting_until = 0.0
        # «→»/«←» — только пока конвейер работает над набором; после нажатия — серые, пока запрос не выполнен.
        # «←» ещё и серая, если предыдущий этап пройден полностью (back_allowed — из таблицы, render_table).
        free = active and not self.skip_pending()
        self.skip_button.configure(state="normal" if free else "disabled")
        self.back_button.configure(state="normal" if free and getattr(self, "back_allowed", False) else "disabled")
        # «↻» — у этапа с API; «↔» — если у этапа больше одного провайдера. Обе серые, пока запрос не выполнен.
        providers = getattr(self, "stage_providers", [])
        self.retry_button.configure(state="normal" if active and providers and not self.request_pending("retry")
                                    else "disabled")
        self.switch_button.configure(state="normal" if active and len(providers) > 1 and not self.request_pending("switch")
                                     else "disabled")
        if time.time() < self.restarting_until:
            self.pause_button.configure(text="Запуск…", state="disabled")
            self.stop_button.configure(state="disabled")
        elif active:
            self.pause_button.configure(text="Продолжить" if self.paused() else "Остановить", state="normal")
            self.stop_button.configure(state="normal")
        elif self.completed or not self.pids():
            # Набор готов (или обработан старым конвейером) — управлять нечем.
            self.pause_button.configure(text="Остановить", state="disabled")
            self.stop_button.configure(state="disabled")
        else:
            # Конвейер остановлен (кнопкой, закончился баланс, упал) — «Продолжить» запускает пачку снова.
            self.pause_button.configure(text="Продолжить", state="normal")
            self.stop_button.configure(state="disabled")

    def poll_control(self) -> None:
        try:
            main_pid = os.environ.get("ANIME_SORT_MAIN_PID")
            if main_pid:
                from engine import winproc

                if not winproc.pid_alive(main_pid):
                    # Главное окно anime-sort закрыто — окна наборов закрываются вместе с ним.
                    self.root.destroy()
                    return
            self.refresh_buttons()
            if self.poll_layout():
                return                      # окно закрыто «✕ Готовые»
        except Exception as exc:
            self.report(exc)
        self.root.after(CONTROL_MS, self.poll_control)

    def on_pause(self) -> None:
        try:
            if self.active():
                paused = not self.paused()
                self.set_paused(paused)
                self.batch_note("пауза (кнопка «Остановить» в окне)" if paused else "продолжение после паузы")
            else:
                self.restart()
            self.refresh_buttons()
        except Exception as exc:
            self.report(exc)

    def restart(self) -> None:
        """Запустить пачку этого конфига снова: она пропустит готовые папки и продолжит этот набор
        с места остановки. Если причина не устранена (например, баланс), пачка снова остановится."""
        import subprocess

        pids = self.pids()
        config = pids.get("config")
        if not config:
            return
        self.set_paused(False)
        self.batch_note("продолжение (кнопка «Продолжить» в окне)")
        python = Path(sys.executable).with_name("pythonw.exe")
        subprocess.Popen([str(python if python.exists() else sys.executable), str(PROJECT / "engine" / "batch.py"),
                          "--config", config], cwd=str(PROJECT), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.restarting_until = time.time() + RESTART_GRACE

    def stop_pipeline(self, why: str) -> None:
        """Остановить конвейер этого набора: пачку, обёртку набора и сам конвейер (окно остаётся).
        Всё сохранённое остаётся — «Продолжить» или start.vbs продолжат с этого места."""
        from engine import winproc

        pids = self.pids()
        self.dataset_note(f"ОСТАНОВКА: завершено пользователем ({why})")
        self.batch_note(f"остановлена пользователем ({why}) — пачка {pids.get('name', '')} остановлена")
        self.set_paused(False)
        for key in ("batch", "dataset"):
            if pids.get(key):
                winproc.kill(pids[key])
        if pids.get("run"):
            winproc.kill_tree(pids["run"])
        self.restarting_until = 0.0

    def on_stop(self) -> None:
        try:
            if self.active():
                self.stop_pipeline("кнопка «Завершить»")
            self.refresh_buttons()
        except Exception as exc:
            self.report(exc)

    def on_close(self) -> None:
        """Закрытие окна активного набора останавливает его конвейер; окна готовых наборов просто закрываются."""
        try:
            if self.active():
                self.stop_pipeline("окно набора закрыто")
        except Exception as exc:
            crash_log(f"stop on close failed: {exc}")
        self.root.destroy()

    def run(self) -> None:
        self.root.update()
        try:
            self.settle_window()
        except Exception as exc:
            self.report(exc)
        self.root.after(0, self.poll_log)
        self.root.after(0, self.poll_table)
        self.root.after(0, self.poll_control)
        self.root.mainloop()


def acquire_single_instance(log: Path):
    """Именованный мьютекс Windows по пути лога. None — окно для этого лога уже открыто."""
    try:
        import ctypes
        import hashlib

        key = hashlib.sha1(str(log.resolve()).casefold().encode("utf-8")).hexdigest()[:16]
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, False, f"Local\\anime-sort-gui-{key}")
        if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            return None
        return handle  # держим до конца процесса
    except Exception as exc:
        crash_log(f"single instance check failed: {exc}")
        return True


def focus_existing_window(title: str) -> None:
    try:
        import ctypes

        user32 = ctypes.windll.user32
        found = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def callback(hwnd, _):
            length = user32.GetWindowTextLengthW(hwnd)
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            if buffer.value == title or buffer.value.startswith(title + " "):
                found.append(hwnd)
            return True

        user32.EnumWindows(callback, None)
        for hwnd in found:
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
    except Exception as exc:
        crash_log(f"focus existing window failed: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, default=None)
    parser.add_argument("--layout", type=Path, default=None)
    parser.add_argument("--title", default="pipeline")
    parser.add_argument("--capture-log", type=Path, default=None)
    parser.add_argument("--capture-table", type=Path, default=None)
    # Место окна вручную (для отладки); без них — по раскладке logs\windows.json.
    parser.add_argument("--x", type=int, default=None)
    parser.add_argument("--y", type=int, default=None)
    args = parser.parse_args()
    # Одно окно на набор: при перезапуске набора (падение, повтор в конце пачки) конвейер
    # снова запускает GUI — второй экземпляр для того же лога не открывается.
    instance = acquire_single_instance(args.log)
    if instance is None:
        focus_existing_window(args.title)
        return 0
    import tkinter as tk

    window = PipelineWindow(tk, args)
    window.run()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        crash_log(traceback.format_exc())
        raise SystemExit(1)
