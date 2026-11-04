"""robots.txt: завантажуємо один раз на хост, далі відповідаємо з кешу.

Правила для відсутнього чи недоступного robots.txt (як у Google):
  * 200        — розбираємо і виконуємо;
  * 4xx        — обмежень немає (файла просто немає);
  * 5xx/збій   — вважаємо, що заборонено ВСЕ: сервер у біді, краще не навантажувати.

Тут потрібен asyncio.Lock — рідкісний випадок замка в asyncio. Між «перевірили, що
robots.txt ще немає в кеші» і «поклали в кеш» стоїть `await fetch(...)`, а на кожному
`await` цикл подій може переключитися на іншу задачу. Без замка 20 робітників, що
одночасно прийшли на новий хост, завантажили б robots.txt 20 разів.
"""

from __future__ import annotations

import asyncio
from urllib.robotparser import RobotFileParser

import httpx

from findex.crawler.fetch import USER_AGENT, fetch
from findex.crawler.urls import origin_of


class RobotsCache:
    def __init__(self, client: httpx.AsyncClient, user_agent: str = USER_AGENT) -> None:
        self._client = client
        self._agent = user_agent
        self._parsers: dict[str, RobotFileParser] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.fetches = 0  # скільки разів реально завантажили robots.txt

    async def _parser(self, url: str) -> RobotFileParser:
        origin = origin_of(url)
        cached = self._parsers.get(origin)
        if cached is not None:
            return cached
        lock = self._locks.setdefault(origin, asyncio.Lock())
        async with lock:
            cached = self._parsers.get(origin)
            if cached is not None:  # хтось завантажив, поки ми чекали на замок
                return cached
            self.fetches += 1
            res = await fetch(self._client, origin + "/robots.txt", attempts=2)
            parser = RobotFileParser(origin + "/robots.txt")
            if res.status == 200 and res.text is not None:
                parser.parse(res.text.splitlines())
            elif res.status is not None and 400 <= res.status < 500:
                parser.parse([])  # порожні правила — дозволено все
            else:
                parser.parse(["User-agent: *", "Disallow: /"])  # заборонено все
            self._parsers[origin] = parser
            return parser

    async def allowed(self, url: str) -> bool:
        return (await self._parser(url)).can_fetch(self._agent, url)

    async def crawl_delay(self, url: str) -> float | None:
        delay = (await self._parser(url)).crawl_delay(self._agent)
        return float(delay) if delay is not None else None
