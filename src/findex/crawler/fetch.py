"""Одне ввічливе завантаження сторінки з повторами.

* Кожен запит — під `asyncio.timeout`: зависле з'єднання не тримає робітника вічно.
* Повторюємо лише ТИМЧАСОВІ збої: 429, 5xx, таймаут, обрив з'єднання; максимум
  `attempts` спроб. 404 та інші 4xx — не повторюємо: сторінки просто немає.
* Пауза між спробами: експоненційний backoff + jitter,
  `min(cap, base · 2^(n−1)) + random(0, base)`. Jitter розводить у часі робітників, що
  впали одночасно, щоб вони не «вдарили» сервер знову всі разом.
* `Retry-After` (секунди або HTTP-дата) має пріоритет над backoff: сервер сам сказав,
  скільки чекати. Обмежуємо `max_retry_after`, щоб не заснути на годину.
* `except` ловить лише конкретні помилки httpx і TimeoutError. `asyncio.CancelledError` —
  `BaseException`, тож скасування (Ctrl-C, зупинка за бюджетом) проходить крізь нас.
"""

from __future__ import annotations

import asyncio
import email.utils
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

log = logging.getLogger("findex.crawler")

# Заголовки HTTP — лише ASCII. Чесно: хто ми і куди писати, якщо щось не так.
USER_AGENT = (
    "findex-crawler/0.6 (+https://github.com/Mazurevych/findex; "
    "student search engine project, KAI; respects robots.txt)"
)
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
HTML_TYPES = ("text/html", "application/xhtml+xml")
MAX_BYTES = 5 * 1024 * 1024  # більші сторінки пропускаємо

type Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class FetchResult:
    url: str  # що просили
    final_url: str  # куди привели переадресації
    status: int | None  # None — відповіді не було (таймаут, обрив)
    content_type: str
    text: str | None  # лише для HTML/тексту
    nbytes: int
    elapsed: float  # секунд разом з усіма спробами й паузами
    attempts: int
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.status == 200

    @property
    def is_html(self) -> bool:
        return self.content_type.startswith(HTML_TYPES)


def backoff_delay(attempt: int, base: float, cap: float, rng: Callable[[], float]) -> float:
    """Пауза перед спробою attempt+1 (attempt рахується з 1)."""
    return min(cap, base * 2 ** (attempt - 1)) + rng() * base


def parse_retry_after(value: str | None, now: float | None = None) -> float | None:
    """`Retry-After: 120` або `Retry-After: Wed, 21 Oct 2026 07:28:00 GMT` -> секунди."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    current = datetime.fromtimestamp(now if now is not None else time.time(), UTC)
    return max(0.0, (when - current).total_seconds())


async def fetch(
    client: httpx.AsyncClient,
    url: str,
    *,
    timeout: float = 10.0,
    attempts: int = 3,
    backoff: float = 0.5,
    max_backoff: float = 10.0,
    max_retry_after: float = 60.0,
    sleep: Sleep = asyncio.sleep,
    rng: Callable[[], float] = random.random,
) -> FetchResult:
    """Завантажує url з повторами. Не кидає винятків на мережевих збоях — повертає
    FetchResult з `error`; кидає лише CancelledError (скасування) і справжні баги."""
    started = time.perf_counter()
    status: int | None = None
    error: str | None = None
    final_url, ctype, text, nbytes = url, "", None, 0
    attempt = 0
    for attempt in range(1, attempts + 1):
        retry_after: float | None = None
        try:
            async with asyncio.timeout(timeout):
                resp = await client.get(url)
        except TimeoutError:
            status, error = None, f"timeout {timeout:.0f}s"
        except httpx.TooManyRedirects:
            status, error = None, "too many redirects"
            log.warning("%s %s", error, url)
            break  # цикл переадресацій не мине від повтору
        except httpx.TransportError as exc:
            status, error = None, f"{type(exc).__name__}: {exc}"
        else:
            status, error = resp.status_code, None
            final_url = str(resp.url)
            ctype = resp.headers.get("content-type", "").lower()
            nbytes = len(resp.content)
            if (
                status == 200
                and nbytes <= MAX_BYTES
                and (ctype.startswith(HTML_TYPES) or ctype.startswith("text/"))
            ):
                text = resp.text
            if status in RETRY_STATUSES:
                error = f"HTTP {status}"
                retry_after = parse_retry_after(resp.headers.get("retry-after"))

        retryable = error is not None and (status is None or status in RETRY_STATUSES)
        if not retryable or attempt == attempts:
            break
        if retry_after is not None:
            delay = min(retry_after, max_retry_after)
            why = f"Retry-After {retry_after:g}s"
        else:
            delay = backoff_delay(attempt, backoff, max_backoff, rng)
            why = "backoff"
        log.info(
            "retry %s attempt=%d/%d after %.2fs (%s; %s)",
            url,
            attempt + 1,
            attempts,
            delay,
            error,
            why,
        )
        await sleep(delay)

    elapsed = time.perf_counter() - started
    if error is None and status is not None and status >= 400:
        error = f"HTTP {status}"
    log.info(
        "%s %s %dB %.3fs attempts=%d%s",
        status if status is not None else "---",
        url if final_url == url else f"{url} -> {final_url}",
        nbytes,
        elapsed,
        attempt,
        f" error={error}" if error else "",
    )
    return FetchResult(url, final_url, status, ctype, text, nbytes, elapsed, attempt, error)
