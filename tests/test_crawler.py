"""Лаба 6: краулер на httpx.MockTransport — жодної справжньої мережі в тестах."""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from findex.crawler import USER_AGENT, crawl, fetch
from findex.crawler.crawl import CrawlStats, Page
from findex.crawler.fetch import backoff_delay, parse_retry_after
from findex.crawler.parse import parse_html
from findex.crawler.robots import RobotsCache
from findex.crawler.urls import canonicalize, in_scope

type Handler = Callable[[httpx.Request], Awaitable[httpx.Response]]


def client(handler: Handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
        max_redirects=5,
    )


class FakeSleep:
    """Замість справжніх пауз — записуємо, скільки нас просили чекати."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def run[T](coro: Awaitable[T]) -> T:
    async def main() -> T:
        return await coro

    return asyncio.run(main())


# --- fetch ---------------------------------------------------------------------------


def test_fetch_ok_records_status_bytes_elapsed() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["user-agent"] == USER_AGENT  # чесний User-Agent
        return httpx.Response(200, html="<title>Hi</title>")

    async def go() -> None:
        async with client(handler) as c:
            res = await fetch(c, "https://ex.test/")
        assert res.ok and res.is_html and res.attempts == 1 and res.nbytes > 0
        assert res.elapsed >= 0

    run(go())


def test_fetch_retries_5xx_with_backoff() -> None:
    hits = Counter[str]()

    async def handler(request: httpx.Request) -> httpx.Response:
        hits["n"] += 1
        return httpx.Response(503 if hits["n"] < 3 else 200, html="ok")

    sleep = FakeSleep()

    async def go() -> None:
        async with client(handler) as c:
            res = await fetch(c, "https://ex.test/", sleep=sleep, rng=lambda: 0.0)
        assert res.ok and res.attempts == 3
        assert sleep.calls == [0.5, 1.0]  # 0.5 · 2^0, 0.5 · 2^1 (jitter = 0)

    run(go())


def test_fetch_honors_retry_after_and_gives_up_after_3() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "7"})

    sleep = FakeSleep()

    async def go() -> None:
        async with client(handler) as c:
            res = await fetch(c, "https://ex.test/", sleep=sleep)
        assert not res.ok and res.status == 429 and res.attempts == 3
        assert sleep.calls == [7.0, 7.0]

    run(go())


def test_fetch_does_not_retry_404() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    sleep = FakeSleep()

    async def go() -> None:
        async with client(handler) as c:
            res = await fetch(c, "https://ex.test/missing", sleep=sleep)
        assert res.attempts == 1 and res.error == "HTTP 404" and sleep.calls == []

    run(go())


def test_fetch_timeout_is_retried() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)  # довше за таймаут
        return httpx.Response(200)

    async def go() -> None:
        async with client(handler) as c:
            res = await fetch(c, "https://ex.test/", timeout=0.02, sleep=FakeSleep())
        assert res.status is None and res.error is not None and "timeout" in res.error
        assert res.attempts == 3

    run(go())


def test_fetch_redirect_loop_is_an_error_without_retry() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        nxt = "/b" if request.url.path == "/a" else "/a"
        return httpx.Response(302, headers={"Location": nxt})

    async def go() -> None:
        async with client(handler) as c:
            res = await fetch(c, "https://ex.test/a", sleep=FakeSleep())
        assert res.error == "too many redirects" and res.attempts == 1

    run(go())


def test_backoff_and_retry_after_parsing() -> None:
    assert [backoff_delay(n, 0.5, 3.0, lambda: 0.0) for n in (1, 2, 3, 4)] == [0.5, 1, 2, 3]
    assert backoff_delay(1, 0.5, 10, lambda: 1.0) == 1.0  # jitter до base
    assert parse_retry_after("120") == 120.0
    now = 1_800_000_000.0  # 2027-01-15 08:00:00 UTC
    assert parse_retry_after("Fri, 15 Jan 2027 08:00:30 GMT", now=now) == pytest.approx(30)
    assert parse_retry_after("nonsense") is None and parse_retry_after(None) is None


# --- URL, HTML, robots -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "canon"),
    [
        (
            "HTTPS://Docs.Example.ORG:443/a/./b/../c?z=1&a=2#frag",
            "https://docs.example.org/a/c/?a=2&z=1",
        ),
        ("http://ex.test", "http://ex.test/"),
        ("http://ex.test:8080/page.html#x", "http://ex.test:8080/page.html"),
        ("https://ex.test/guide", "https://ex.test/guide/"),
        ("mailto:me@ex.test", None),
        ("javascript:void(0)", None),
    ],
)
def test_canonicalize(raw: str, canon: str | None) -> None:
    assert canonicalize(raw) == canon


def test_scope() -> None:
    domains = frozenset({"ex.test"})
    assert in_scope("https://docs.ex.test/x", domains, ())
    assert not in_scope("https://evil.test/x", domains, ())
    assert not in_scope("https://ex.test/blog/", domains, ("https://ex.test/docs/",))


def test_parse_html_extracts_title_text_links() -> None:
    html = """<html><head><title> Hello
      World </title><script>var hidden = 1;</script><base href="https://ex.test/docs/"></head>
      <body><nav><a href="a.html">A</a> <a href="/b" rel="nofollow">B</a></nav>
      <p>Visible &amp; text</p><style>.x{}</style><a href="#top">top</a></body></html>"""
    page = parse_html(html, "https://ex.test/")
    assert page.title == "Hello World"
    assert "Visible & text" in page.text and "hidden" not in page.text
    assert page.links == ["https://ex.test/docs/a.html", "https://ex.test/docs/#top"]


def test_parse_meta_robots() -> None:
    page = parse_html('<meta name="robots" content="noindex, nofollow"><p>x</p>', "https://e/")
    assert page.noindex and page.nofollow


def test_robots_cache_fetches_once_per_host_and_obeys_rules() -> None:
    hits = Counter[str]()

    async def handler(request: httpx.Request) -> httpx.Response:
        hits[request.url.host] += 1
        await asyncio.sleep(0.01)  # щоб 20 задач справді «зустрілись» на одному хості
        if request.url.host == "gone.test":
            return httpx.Response(404)
        if request.url.host == "down.test":
            return httpx.Response(500)
        return httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")

    async def go() -> None:
        async with client(handler) as c:
            robots = RobotsCache(c)
            urls = [f"https://ex.test/page{i}" for i in range(20)]
            assert all(await asyncio.gather(*(robots.allowed(u) for u in urls)))
            assert not await robots.allowed("https://ex.test/private/x")
            assert await robots.allowed("https://gone.test/anything")  # 404 -> усе можна
            assert not await robots.allowed("https://down.test/anything")  # 5xx -> нічого
        assert hits["ex.test"] == 1  # Lock: лише один запит robots.txt на хост

    run(go())


# --- crawl ----------------------------------------------------------------------------


def make_site(n: int = 30) -> tuple[Handler, Counter[str], dict[str, int]]:
    """Сайт: /p0 ... /p{n-1}; кожна сторінка посилається на дві наступні, на себе з
    #фрагментом, на заборонену /private/, на чужий домен і на 404."""
    fetched = Counter[str]()
    gauge = {"now": 0, "max": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")
        fetched[path] += 1
        gauge["now"] += 1
        gauge["max"] = max(gauge["max"], gauge["now"])
        try:
            await asyncio.sleep(0.005)
            if path.startswith("/p"):
                i = int(path.removeprefix("/p").strip("/") or 0)
                links = [
                    f"/p{(i + 1) % n}",
                    f"/p{(i + 2) % n}",
                    f"/p{i}#top",
                    "/private/x",
                    "https://other.test/",
                    "/missing",
                ]
                body = "".join(f'<a href="{h}">{h}</a>' for h in links)
                return httpx.Response(200, html=f"<title>Page {i}</title><p>text {i}</p>{body}")
            return httpx.Response(404)
        finally:
            gauge["now"] -= 1

    return handler, fetched, gauge


async def collect(pages: object) -> list[Page]:
    from collections.abc import AsyncGenerator
    from contextlib import aclosing

    assert isinstance(pages, AsyncGenerator)
    out: list[Page] = []
    async with aclosing(pages) as gen:
        async for page in gen:
            out.append(page)
    return out


def test_crawl_respects_budget_robots_domain_and_never_refetches() -> None:
    handler, fetched, _ = make_site(30)
    stats = CrawlStats()

    async def go() -> list[Page]:
        async with client(handler) as c:
            gen = crawl(
                ["https://ex.test/p0"],
                max_pages=12,
                concurrency=5,
                per_host=5,
                min_delay=0,
                client=c,
                stats=stats,
            )
            return await collect(gen)

    pages = run(go())
    assert len(pages) == 12 and stats.pages == 12  # рівно бюджет
    assert len({p.url for p in pages}) == 12
    assert all(count == 1 for count in fetched.values())  # жодної сторінки двічі
    assert not any(p.startswith("/private/") for p in fetched)  # robots.txt
    assert sum(fetched.values()) <= 12 + 1  # не більше бюджету (+ можливий 404 /missing)
    assert pages[0].title == "Page 0" and "text 0" in pages[0].text


def test_crawl_drains_small_site_and_stops_cleanly() -> None:
    handler, fetched, _ = make_site(8)

    async def go() -> list[Page]:
        async with client(handler) as c:
            pages = await collect(
                crawl(
                    ["https://ex.test/p0"],
                    max_pages=100,
                    concurrency=4,
                    per_host=4,
                    min_delay=0,
                    client=c,
                )
            )
            assert len(asyncio.all_tasks()) == 1  # лишилась тільки ця задача — ніхто не висить
            return pages

    pages = run(go())
    assert len(pages) == 8 and fetched["/missing/"] == 1  # канонізовано: /missing -> /missing/


def test_crawl_concurrency_limits() -> None:
    handler, _, gauge = make_site(40)

    async def go(concurrency: int, per_host: int) -> int:
        gauge["max"] = 0
        async with client(handler) as c:
            await collect(
                crawl(
                    ["https://ex.test/p0"],
                    max_pages=30,
                    concurrency=concurrency,
                    per_host=per_host,
                    min_delay=0,
                    client=c,
                )
            )
        return gauge["max"]

    assert run(go(8, 2)) <= 2  # ліміт на хост
    assert run(go(3, 10)) <= 3  # глобальний ліміт


def test_crawl_min_delay_spaces_requests() -> None:
    handler, _, _ = make_site(10)

    async def go() -> float:
        loop = asyncio.get_running_loop()
        start = loop.time()
        async with client(handler) as c:
            await collect(
                crawl(
                    ["https://ex.test/p0"],
                    max_pages=5,
                    concurrency=5,
                    per_host=5,
                    min_delay=0.05,
                    client=c,
                )
            )
        return loop.time() - start

    assert run(go()) >= 4 * 0.05  # 5 запитів до хоста, між початками ≥ 50 мс


def test_crawl_worker_bug_propagates() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        raise RuntimeError("bug in transport")  # не мережева помилка, а баг

    async def go() -> None:
        async with client(handler) as c:
            await collect(
                crawl(["https://ex.test/"], max_pages=5, concurrency=2, client=c, min_delay=0)
            )

    with pytest.raises(ExceptionGroup) as info:
        run(go())
    assert info.group_contains(RuntimeError, match="bug in transport")


# --- CLI ------------------------------------------------------------------------------


def test_crawl_cli_writes_jsonl_that_index_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from findex.cli import app
    from findex.corpus import iter_documents

    handler, _, _ = make_site(6)
    monkeypatch.setattr("findex.cli.make_client", lambda ua, n: client(handler))
    out, log = tmp_path / "crawl.jsonl", tmp_path / "crawl.log"
    result = CliRunner().invoke(
        app,
        [
            "crawl",
            "https://ex.test/p0",
            "--max-pages",
            "4",
            "--delay",
            "0",
            "--out",
            str(out),
            "--log",
            str(log),
            "--json",
            "--scope",
            "domain",
        ],
    )
    assert result.exit_code == 0, result.output
    summary = json.loads(result.stdout)
    assert summary["pages"] == 4
    docs = list(iter_documents(out))
    assert len(docs) == 4 and docs[0].title == "Page 0" and docs[0].doc_id.startswith("https://")
    assert "robots.txt" in log.read_text("utf-8")  # журнал по кожному URL
