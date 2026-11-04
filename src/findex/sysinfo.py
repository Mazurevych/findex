"""Пік резидентної пам'яті процесу — однаково на Linux, macOS і Windows (лаби 5–6)."""

from __future__ import annotations

import ctypes
import sys
from typing import Any


def peak_rss_bytes() -> int:
    """Пік резидентної пам'яті (RSS) ЦЬОГО процесу в байтах."""
    if sys.platform == "win32":
        return _peak_rss_windows()
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024  # Linux: КіБ, macOS: байти


class _Counters(ctypes.Structure):
    """PROCESS_MEMORY_COUNTERS з psapi.h."""

    _fields_ = [  # noqa: RUF012 — так оголошуються структури ctypes
        ("cb", ctypes.c_uint32),
        ("PageFaultCount", ctypes.c_uint32),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _peak_rss_windows() -> int:
    windll: Any = getattr(ctypes, "windll")  # noqa: B009 — існує лише на Windows
    counters = _Counters()
    counters.cb = ctypes.sizeof(_Counters)
    handle = windll.kernel32.GetCurrentProcess()
    windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
    return int(counters.PeakWorkingSetSize)
