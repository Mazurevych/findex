"""crawl(): асинхронний генератор сторінок — як iter_documents у лабі 1, але з мережі.

    async with aclosing(crawl(["https://example.org/docs/"], max_pages=200)) as pages:
        async for page in pages:
            ...  # сторінки надходять у міру завантаження, а не всі наприкінці

Будова:

    фронтир (asyncio.Queue URL)  ──►  N робітників у TaskGroup  ──►  out (Queue Page)  ──►  yield
            ▲                              │
            └───── нові посилання ◄────────┘

* `seen` — множина канонічних URL: кожну сторінку ставимо в чергу лише раз.
* Глобальний семафор обмежує запити в польоті загалом, семафор на хост — до одного
  сервера; плюс мінімальна пауза між ПОЧАТКАМИ запитів до хоста (або Crawl-delay з robots).
* Бюджет `max_pages`: робітник «резервує» місце перед запитом, тож навіть при 20 робітниках
  ми не завантажимо зайвих сторінок, а рівно max_pages успішних.
* Усе однопотокове: між перевіркою і зміною лічильника немає `await`, тож замки не потрібні
  (крім robots.txt — див. robots.py).
* Розбір HTML — у `asyncio.to_thread`, індексація — взагалі ПІСЛЯ обходу (`findex index`).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncGenerator, Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from findex.crawler.fetch import USER_AGENT, fetch
from findex.crawler.parse import parse_html
from findex.crawler.robots import RobotsCache
from findex.crawler.urls import canonicalize, host_of, in_scope, looks_like_page

log = logging.getLogger("findex.crawler")


@dataclass(frozen=True, slots=True)
class Page:
    url: str
    title: str
    text: str
    fetched_at: str  # ISO 8601, UTC
    status: int = 200


@dataclass(slots=True)
class CrawlStats:
    """Живі лічильники — їх читає індикатор `findex crawl`."""

    pages: int = 0
    fetched: int = 0  # усі завершені запити (і невдалі)
    errors: int = 0
    robots_blocked: int = 0
    skipped: int = 0  # не HTML, noindex, поза межами
    in_flight: int = 0
    max_in_flight: int = 0
    queued: int = 0
    bytes: int = 0
    started: float = field(default_factory=time.perf_counter)
    error_samples: list[str] = field(default_factory=list[str])

    @property
    def elapsed(self) -> float:
        return time.perf_counter() - self.started

    @property
    def pages_per_s(self) -> float:
        return self.pages / self.elapsed if self.elapsed > 0 else 0.0


def make_client(user_agent: str = USER_AGENT, concurrency: int = 10) -> httpx.AsyncClient:
    """Один клієнт на весь обхід: пул з'єднань перевикористовує TCP/TLS між запитами."""
    return httpx.AsyncClient(
        headers={"User-Agent": user_agent},
        follow_redirects=True,
        max_redirects=5,
        timeout=None,  # таймаути ставить fetch() через asyncio.timeout
        limits=httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency),
    )


async def crawl(
    seeds: Iterable[str],
    *,
    max_pages: int = 500,
    concurrency: int = 10,
    per_host: int = 2,
    min_delay: float = 0.5,
    allowed_domains: Iterable[str] | None = None,
    scope_prefixes: Iterable[str] | None = None,
    client: httpx.AsyncClient | None = None,
    user_agent: str = USER_AGENT,
    timeout: float = 10.0,
    stats: CrawlStats | None = None,
) -> AsyncGenerator[Page]:
    """Обходить сайт(и) від seeds у ширину і віддає сторінки по одній."""
    seed_urls = [u for u in (canonicalize(s) for s in seeds) if u]
    if not seed_urls:
        raise ValueError("немає жодного коректного http(s) seed-URL")
    domains = frozenset(
        d.lower() for d in (allowed_domains or [host_of(u).split(":")[0] for u in seed_urls])
    )
    prefixes = tuple(scope_prefixes or ())
    stats = stats if stats is not None else CrawlStats()
    own_client = client is None
    http = client if client is not None else make_client(user_agent, concurrency)

    robots = RobotsCache(http, user_agent)
    frontier: asyncio.Queue[str] = asyncio.Queue()
    out: asyncio.Queue[Page] = asyncio.Queue(maxsize=max(1, concurrency))  # зворотний тиск
    seen: set[str] = set()
    global_sem = asyncio.Semaphore(concurrency)
    host_sems: dict[str, asyncio.Semaphore] = {}
    next_start: dict[str, float] = {}
    stop = asyncio.Event()
    released = asyncio.Event()
    reserved = 0  # сторінки «в роботі» + вже віддані; не більше max_pages
    loop = asyncio.get_running_loop()

    def enqueue(url: str) -> None:
        canon = canonicalize(url)
        if canon is None or canon in seen:
            return
        if not in_scope(canon, domains, prefixes) or not looks_like_page(canon):
            return
        seen.add(canon)
        frontier.put_nowait(canon)
        stats.queued = frontier.qsize()

    async def polite_pause(url: str) -> None:
        host = host_of(url)
        delay = max(min_delay, await robots.crawl_delay(url) or 0.0)
        now = loop.time()
        start = max(now, next_start.get(host, now))
        next_start[host] = start + delay  # без await між читанням і записом — атомарно
        if start > now:
            await asyncio.sleep(start - now)  # asyncio.sleep не блокує інших робітників

    async def handle(url: str) -> None:
        nonlocal reserved
        if stop.is_set():
            return
        if not await robots.allowed(url):
            stats.robots_blocked += 1
            log.info("robots.txt disallows %s", url)
            return
        while reserved >= max_pages and not stop.is_set():
            released.clear()
            await released.wait()  # чекаємо, поки звільниться місце в бюджеті
        if stop.is_set():
            return
        reserved += 1
        emitted = False
        try:
            host_sem = host_sems.setdefault(host_of(url), asyncio.Semaphore(per_host))
            async with global_sem, host_sem:
                await polite_pause(url)
                stats.in_flight += 1
                stats.max_in_flight = max(stats.max_in_flight, stats.in_flight)
                try:
                    res = await fetch(http, url, timeout=timeout)
                finally:
                    stats.in_flight -= 1
            stats.fetched += 1
            stats.bytes += res.nbytes
            final = canonicalize(res.final_url)
            if final:
                seen.add(final)  # після переадресації — щоб не завантажити ціль ще раз
            if not res.ok:
                stats.errors += 1
                if len(stats.error_samples) < 20:
                    stats.error_samples.append(f"{res.error}: {url}")
                return
            if res.text is None or not res.is_html:
                stats.skipped += 1
                return
            parsed = await asyncio.to_thread(parse_html, res.text, res.final_url)
            if not parsed.nofollow:
                for link in parsed.links:
                    enqueue(link)
            if parsed.noindex or stop.is_set():
                stats.skipped += 1
                return
            page = Page(
                url=final or res.final_url,
                title=parsed.title or (final or url),
                text=parsed.text,
                fetched_at=datetime.now(UTC).isoformat(timespec="seconds"),
                status=res.status or 200,
            )
            emitted = True
            stats.pages += 1
            await out.put(page)
            if stats.pages >= max_pages:
                stop.set()
                released.set()
        finally:
            if not emitted:
                reserved -= 1
                released.set()

    async def worker() -> None:
        while True:
            url = await frontier.get()
            stats.queued = frontier.qsize()
            try:
                await handle(url)
            finally:
                frontier.task_done()

    async def run() -> None:
        async with asyncio.TaskGroup() as tg:
            workers = [tg.create_task(worker(), name=f"crawl-{i}") for i in range(concurrency)]
            drained = asyncio.ensure_future(frontier.join())
            budget = asyncio.ensure_future(stop.wait())
            try:
                await asyncio.wait({drained, budget}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                drained.cancel()
                budget.cancel()
                for w in workers:  # робітники нескінченні — зупиняємо явно
                    w.cancel()

    for url in seed_urls:
        enqueue(url)
    runner = asyncio.create_task(run(), name="crawl-runner")
    try:
        while True:
            getter = asyncio.ensure_future(out.get())
            await asyncio.wait({getter, runner}, return_when=asyncio.FIRST_COMPLETED)
            if getter.done():
                yield getter.result()
                continue
            getter.cancel()
            with suppress(asyncio.CancelledError):
                await getter  # дочекатися скасування, щоб не лишити «висячу» задачу
            while not out.empty():  # обхід завершився — віддаємо те, що лишилось у черзі
                yield out.get_nowait()
            break
        await runner  # якщо робітник упав — тут підніметься ExceptionGroup
    finally:
        if not runner.done():  # споживач перервав цикл (break) або виняток
            runner.cancel()
            with suppress(asyncio.CancelledError):
                await runner
        if own_client:
            await http.aclose()
        log.info(
            "crawl done: %d pages, %d requests, %d errors, %d robots-blocked in %.1fs",
            stats.pages,
            stats.fetched,
            stats.errors,
            stats.robots_blocked,
            stats.elapsed,
        )
