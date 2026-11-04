"""Декоратор @timed: логує час виконання функції в мілісекундах.

    @timed
    def build_index(...): ...

    # лог (рівень INFO, видно з `findex -v ...`):
    # INFO findex.timing: build_index: 2140.3 ms

Без `functools.wraps` обгортка мала б ім'я `wrapper`, порожній docstring і чужий
`__module__`: зламались би логи, `help()`, інтроспекція і pickle за іменем.
"""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import Callable

log = logging.getLogger(__name__)


def timed[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
    """Обгортає fn: міряє perf_counter до і після виклику, пише в лог."""

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        start = time.perf_counter()
        try:
            return fn(*args, **kwargs)
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000
            # %-форматування: рядок збирається лише якщо рівень INFO увімкнено
            log.info("%s: %.1f ms", fn.__qualname__, elapsed_ms)

    return wrapper
