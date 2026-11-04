"""Ввічливий асинхронний краулер (лаба 6): `findex crawl`.

fetch.py   — одне завантаження: таймаут, повтори з backoff + jitter, Retry-After
robots.py  — RobotsCache: robots.txt кожного хоста завантажується один раз
urls.py    — канонізація URL і межі обходу
parse.py   — заголовок, текст і посилання зі сторінки (html.parser зі stdlib)
crawl.py   — crawl(): черга-фронтир, робітники в TaskGroup, семафори, async-генератор
"""

from findex.crawler.crawl import CrawlStats, Page, crawl
from findex.crawler.fetch import USER_AGENT, FetchResult, fetch

__all__ = ["USER_AGENT", "CrawlStats", "FetchResult", "Page", "crawl", "fetch"]
