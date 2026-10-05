"""Отключает WMI-запросы модуля platform (Python 3.12 на Windows).

onnxruntime при импорте вызывает platform.system(), а тот в Python 3.12 спрашивает версию
Windows через WMI (platform._wmi_query). Когда служба WMI отвечает нестабильно, этот запрос
роняет весь процесс внутри ntdll.dll с кодом 0xC000070A (STATUS_THREADPOOL_HANDLE_EXCEPTION) —
так падал конвейер на этапе WD-14. Без WMI platform берёт те же данные из переменных
окружения и команды ver, это штатный запасной путь.

Импортировать ДО onnxruntime / imgutils (первой строкой).
"""
import sys

# Если platform ещё не импортирован — он увидит, что _wmi нет, и сразу выберет запасной путь.
sys.modules.setdefault("_wmi", None)

import platform  # noqa: E402


def _no_wmi(*_args, **_kwargs):
    raise OSError("WMI disabled (nowmi)")


# Если platform уже был импортирован раньше — подменяем сам запрос.
platform._wmi_query = _no_wmi
