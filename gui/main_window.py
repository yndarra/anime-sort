r"""Главное окно anime-sort (вместо консоли): слева журнал всех пачек, справа статистика, под ней кнопки окон.

Открывает engine\main.py (start.vbs → pythonw main.py): окно живёт в главном потоке, диспетчер пачек — в фоновом.
Журнал — строки say() диспетчера (они же в logs\console.log), цвета — как в консоли.
Статистика — gui\stats.py: что делает каждая пачка, папки, скорость за час, модели и API за сегодня, последний сбой.
Кнопки (только здесь; у окон наборов — лишь «Остановить» и «Завершить»):
    [Сетап 1] [Сетап 2] [Сетап 3] [Режим 1] [Режим 2]
    [Наверх] [Вниз] [Лишние ✕] [Монитор →]
«Сетап N» применяет и действующий режим; после любого сетапа, режима и «Монитор →» (и при запуске) все окна
anime-sort поднимаются над окнами других программ.
Закрыть это окно — остановить все пачки и окна наборов (как раньше закрыть консоль); спрашивает подтверждение.
"""
from __future__ import annotations

import queue
import threading
import time

import window
from window import (ACTIVE_SETUP_BG, BACKGROUND, BUTTON_BG, BUTTON_SIZE, BUTTON_STYLE, MANUAL_SETUP_BG, BaseWindow,
                    crash_log)

LOG_MS = 300
STATS_MS = 3000


class MainWindow(BaseWindow):
    def __init__(self, tk, dispatcher, state: dict, log_queue: queue.Queue, note, log_chars: int, table_chars: int):
        # Ширины в символах для "auto" — как у окон наборов (столбцы лога и таблицы из config.json).
        self.log_chars, self.table_chars = log_chars, table_chars
        self.dispatcher = dispatcher
        self.queue = log_queue
        self.note = note                        # строка в журнал (main.say): в это окно и в logs\console.log
        self.setup_buttons: list = []
        self.arrange_buttons: list = []
        # Хоть одно окно anime-sort сдвинули / растянули вручную (по отметкам в logs\windows.json).
        self.any_moved = self.any_resized = False
        super().__init__(tk, "anime-sort", state, 0)
        # Строки журнала длинные и разные — переносим по словам, а не режем.
        self.log_text.configure(wrap="word")
        from stats import Stats

        self.stats = Stats(dispatcher, width=max(40, table_chars))
        self.stats_lines: list | None = None
        self.stats_busy = False
        self.make_buttons()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------- журнал ----------

    def append(self, text: str, code: str | None) -> None:
        self.log_text.insert("end", text + "\n", (code,) if code else ())

    def show_error(self, message: str, kind: str) -> None:
        self.append(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {kind}: {message}", "31")
        self.log_text.see("end")

    def poll_log(self) -> None:
        try:
            follow = self.at_bottom()
            added = False
            while True:
                try:
                    text, code = self.queue.get_nowait()
                except queue.Empty:
                    break
                self.append(text, code)
                added = True
            if added and follow:
                self.log_text.see("end")
        except Exception as exc:
            self.report(exc)
        finally:
            self.root.after(LOG_MS, self.poll_log)

    # ---------- статистика ----------

    def collect_stats(self) -> None:
        """Фоновый поток: читает журналы наборов (может занять секунду) — окно в это время не подвисает."""
        try:
            self.stats_lines = self.stats.collect()
        except Exception as exc:
            crash_log(f"stats failed: {exc!r}")
            self.stats_lines = [(f"статистика: сбой — {exc}", "31")]
        finally:
            self.stats_busy = False

    def poll_stats(self) -> None:
        try:
            if self.stats_lines is not None and not self.table_text.tag_ranges("sel"):
                lines, self.stats_lines = self.stats_lines, None
                top = self.table_text.yview()[0]
                self.table_text.delete("1.0", "end")
                for text, code in lines:
                    self.table_text.insert("end", text + "\n", (code,) if code else ())
                self.table_text.yview_moveto(top)
            if not self.stats_busy:
                self.stats_busy = True
                threading.Thread(target=self.collect_stats, daemon=True).start()
        except Exception as exc:
            self.report(exc)
        finally:
            self.root.after(STATS_MS, self.poll_stats)

    # ---------- кнопки окон ----------

    def make_buttons(self) -> None:
        tk = self.tk
        frame = tk.Frame(self.table_text.master, bg=BACKGROUND)
        frame.grid(row=1, column=0, sticky="ew", padx=6, pady=(0, 6))
        frame.grid_columnconfigure(0, weight=1)
        rows = [
            [(f"Сетап {n}", lambda n=n: self.on_setup(n), self.setup_buttons) for n in (1, 2, 3)]
            + [(f"Режим {n}", lambda n=n: self.on_arrange(n), self.arrange_buttons) for n in (1, 2)],
            [("Наверх", lambda: self.on_layer(True), None), ("Вниз", lambda: self.on_layer(False), None),
             ("Лишние ✕", self.on_close_done, None), ("Монитор →", self.on_next_monitor, None)],
        ]
        for row_index, buttons in enumerate(rows):
            line = tk.Frame(frame, bg=BACKGROUND, height=BUTTON_SIZE)
            line.grid(row=row_index, column=0, sticky="nsew", pady=(0 if row_index == 0 else 4, 0))
            line.grid_rowconfigure(0, weight=1)
            line.grid_propagate(False)
            for index, (text, command, group) in enumerate(buttons):
                line.grid_columnconfigure(index, weight=1, uniform=f"windows{row_index}")
                button = tk.Button(line, text=text, command=command, **BUTTON_STYLE)
                button.grid(row=0, column=index, sticky="nsew",
                            padx=(0 if index == 0 else 2, 0 if index == len(buttons) - 1 else 2))
                if group is not None:
                    group.append(button)
        self.paint_setup_buttons()

    def raise_all_later(self, delay_ms: int = 1500) -> None:
        """Через мгновение (окна успеют встать по местам) — все окна anime-sort над окнами других программ."""
        self.root.after(delay_ms, lambda: self.layout.layer_all(True))

    def paint_setup_buttons(self) -> None:
        """Действующие сетап и режим — синие. Какое-то окно (любое, и это тоже) растянули вручную — кнопка сетапа
        светло-серая; сдвинули — кнопка режима светло-серая. Снова нажать сетап/режим — опять синие."""
        resized = self.any_resized or self.resized
        moved = self.any_moved or self.moved
        for number, button in enumerate(self.setup_buttons, 1):
            active = number == self.current_setup
            button.configure(bg=(MANUAL_SETUP_BG if resized else ACTIVE_SETUP_BG) if active else BUTTON_BG)
        for number, button in enumerate(self.arrange_buttons, 1):
            active = number == self.current_arrangement
            button.configure(bg=(MANUAL_SETUP_BG if moved else ACTIVE_SETUP_BG) if active else BUTTON_BG)

    def show_problems(self, problems: list[str]) -> None:
        for text in problems[:5]:
            first, _, fix = text.partition("\n")
            self.show_error(f"{first} — {fix.strip()}" if fix else first, "live.json")

    def on_setup(self, number: int) -> None:
        try:
            config, problems = self.layout.read_config()
            if problems:
                self.show_problems(problems)
                return
            self.layout.choose_setup(number, config)
            self.note(f"окна — сетап {number} ({self.layout.describe_setup(config['setups'][str(number)])}) и режим {self.current_arrangement}")
            self.poll_layout()
            self.raise_all_later()
        except Exception as exc:
            self.report(exc)

    def on_arrange(self, number: int) -> None:
        try:
            config, problems = self.layout.read_config()
            if problems:
                self.show_problems(problems)
                return
            self.layout.arrange(number, config, self.layout.work_area(self.hwnd()))
            self.note(f"окна расставлены — режим {number}: {self.layout.describe_arrangement(config['arrangements'][str(number)])}")
            self.poll_layout()
            self.raise_all_later()
        except Exception as exc:
            self.report(exc)

    def on_next_monitor(self) -> None:
        try:
            _, target = self.layout.next_monitor()
            if target:
                self.note(f"окна перенесены на монитор {target['number']} ({target['size'][0]}×{target['size'][1]})")
            self.poll_layout()
            self.raise_all_later()
        except Exception as exc:
            self.report(exc)

    def on_layer(self, raised: bool) -> None:
        try:
            self.layout.layer_all(raised)
            self.note("окна подняты над остальными" if raised else "окна спрятаны под остальные")
        except Exception as exc:
            self.report(exc)

    def on_close_done(self) -> None:
        try:
            self.layout.request_close_done()
            self.note("закрыты окна наборов, над которыми конвейер не работает (кнопка «Лишние ✕»)")
        except Exception as exc:
            self.report(exc)

    # ---------- цикл окна ----------

    def poll_control(self) -> None:
        if getattr(self.dispatcher, "quit_requested", False):
            # Пульт: «Остановить всё» — закрыться без вопроса (окна наборов и пачки закроются вместе с главным).
            self.root.destroy()
            return
        try:
            self.poll_layout()
            flags = self.layout.manual_flags(self.layout.read_state())
            if flags != (self.any_moved, self.any_resized):
                self.any_moved, self.any_resized = flags
                self.paint_setup_buttons()
        except Exception as exc:
            self.report(exc)
        self.root.after(window.CONTROL_MS, self.poll_control)

    def on_close(self) -> None:
        from tkinter import messagebox

        if messagebox.askyesno("anime-sort", "Закрыть anime-sort?\n\nОстановятся все пачки и окна наборов "
                                             "(всё сохранённое останется, start.vbs продолжит с того же места).",
                               parent=self.root, icon="warning"):
            self.root.destroy()

    def run(self) -> None:
        self.root.update()
        try:
            self.settle_window()
        except Exception as exc:
            self.report(exc)
        # При запуске — все окна anime-sort наверх (окна наборов открываются следом и тоже встают наверх сами).
        self.raise_all_later(3000)
        self.root.after(0, self.poll_log)
        self.root.after(200, self.poll_stats)
        self.root.after(0, self.poll_control)
        self.root.mainloop()
