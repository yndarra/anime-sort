r"""Пульт anime-sort — одно окно, из которого делается всё, что умеет проект (запуск: control.vbs).

Окно — pywebview (встроенный в Windows движок Edge WebView2), внутри — страница gui\control\web\ (HTML/CSS/JS без
сборки). Всё, что делает страница, она просит у Python через window.pywebview.api (gui\control\api.py):

    Обзор           наборы с миниатюрами и прогрессом, Waifu, модели и API за сегодня, история показателей
    Конвейеры       обычный режим по шагам: агент конфигов → подготовить → запустить / остановить → finish
    Второй круг     Other → временные dataN-other со своими конфигами (configs\other\)
    Конфиги         шаблоны пачек формой (этапы, модели, API, запасные модели) и все конфиги как JSON
    Имена           names.json: правила переименования тайтлов и персонажей
    API и ключи     провайдеры, ключи (значения в страницу не попадают), проверка всех API, балансы
    Инструменты     все инструменты tools\ с пробным прогоном
Внизу — «Задачи»: вывод запущенных инструментов, их вопросы — кнопками.

Конвейеры по-прежнему открывают своё главное окно и окна наборов (engine\main.py, Tkinter); Пульт ими управляет.

    --page <имя>                 открыть страницу (overview, pipelines, other, configs, names, apis, tools)
    env ANIME_SORT_CONTROL_TEST  проверочный экземпляр: мимо «один Пульт», «(проверка)» в заголовке
"""
from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
for path in (PROJECT / "gui", PROJECT / "tools", PROJECT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

TITLE = "anime-sort — Пульт"
WEB = Path(__file__).resolve().parent / "web"


def single_instance():
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, "Local\\anime-sort-control")
    return None if kernel32.GetLastError() == 183 else handle


def focus_existing(title: str) -> None:
    user32 = ctypes.windll.user32
    hwnd = user32.FindWindowW(None, title)
    if hwnd:
        user32.ShowWindow(hwnd, 9)          # SW_RESTORE
        user32.SetForegroundWindow(hwnd)


def main() -> int:
    import webview

    import modes
    from control.api import Api

    title = TITLE
    if os.environ.get("ANIME_SORT_CONTROL_TEST"):
        title = f"{TITLE} (проверка)"
    elif single_instance() is None:
        focus_existing(TITLE)
        return 0
    modes.migrate()   # старая раскладка configs\template*.json → configs\main|other\
    page = sys.argv[sys.argv.index("--page") + 1] if "--page" in sys.argv else "overview"
    # Страница отдаётся встроенным http-сервером pywebview (http_server=True): со страницы, открытой как файл,
    # движок не грузит часть ресурсов.
    webview.create_window(title, str(WEB / "index.html"), js_api=Api(page), width=1560, height=980,
                          min_size=(1100, 700), background_color="#0e0f13", text_select=True)
    webview.start(gui="edgechromium", http_server=True, private_mode=False,
                  storage_path=str(PROJECT / "logs" / "webview"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
