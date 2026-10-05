"""Процессы Windows без сторонних библиотек (ctypes): жив ли процесс, остановка, job object, ядра, цвета консоли."""
from __future__ import annotations

import ctypes
import os
import subprocess
from ctypes import wintypes

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

PROCESS_TERMINATE = 0x0001
PROCESS_SET_INFORMATION = 0x0200
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259

kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
kernel32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
kernel32.GetCurrentProcess.restype = wintypes.HANDLE
kernel32.SetProcessAffinityMask.argtypes = (wintypes.HANDLE, ctypes.c_size_t)
kernel32.CreateJobObjectW.restype = wintypes.HANDLE
kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
kernel32.GetStdHandle.restype = wintypes.HANDLE
kernel32.GetConsoleMode.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
kernel32.SetConsoleMode.argtypes = (wintypes.HANDLE, wintypes.DWORD)


def pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def kill(pid) -> None:
    """Остановить один процесс (без дочерних)."""
    try:
        handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, int(pid))
    except (TypeError, ValueError):
        return
    if handle:
        kernel32.TerminateProcess(handle, 1)
        kernel32.CloseHandle(handle)


def kill_tree(pid) -> None:
    """Остановить процесс вместе со всеми живыми потомками."""
    try:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(int(pid))], capture_output=True, check=False,
                       creationflags=NO_WINDOW)
    except (OSError, TypeError, ValueError):
        pass


# ---------------------------------------------------------------- job object: закрыли консоль — остановилось всё

class _BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in ("Read", "Write", "Other", "ReadBytes", "WriteBytes", "OtherBytes")]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JobObjectExtendedLimitInformation = 9


def kill_children_with_me():
    """Job object с KILL_ON_JOB_CLOSE: когда этот процесс завершится (в т.ч. закрыли его консоль),
    Windows остановит все процессы, запущенные из него и из его потомков. Возвращает handle (держать до конца)."""
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = _ExtendedLimits()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(job, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)):
        return None
    if not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
        return None
    return job


# ---------------------------------------------------------------- ядра

def limit_cores(free: int) -> int:
    """Привязать процесс к первым (всего - free) логическим процессорам. Все конвейеры получают ОДИН И ТОТ ЖЕ
    набор, поэтому даже несколько параллельных WD-14/Camie вместе не трогают последние free ядер.
    Возвращает, сколько ядер доступно процессу."""
    total = os.cpu_count() or 1
    allowed = max(1, total - max(0, free))
    mask = (1 << allowed) - 1
    kernel32.SetProcessAffinityMask(kernel32.GetCurrentProcess(), mask)
    return allowed


# ---------------------------------------------------------------- консоль

def enable_ansi() -> None:
    """Цветной вывод (ANSI) в обычной консоли Windows."""
    handle = kernel32.GetStdHandle(-11)
    mode = wintypes.DWORD()
    if handle and kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)
