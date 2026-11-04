"""Розбір HTML: заголовок, видимий текст і посилання. Лише стандартна бібліотека.

Розбір — це робота процесора, тож краулер викликає `parse_html` через
`asyncio.to_thread`, а не прямо в корутині: цикл подій тим часом обслуговує інші запити.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin

_SKIP = frozenset({"script", "style", "noscript", "template", "svg", "head"})
_BLOCK = frozenset({
    "p", "div", "section", "article", "li", "ul", "ol", "br", "h1", "h2", "h3", "h4", "h5",
    "h6", "pre", "table", "tr", "td", "th", "blockquote", "dt", "dd", "header", "footer",
    "nav", "main", "aside", "hr",
})  # fmt: skip
_SPACES = re.compile(r"[ \t\r\f\v]+")
_LINES = re.compile(r"\n\s*\n+")


@dataclass(slots=True)
class ParsedPage:
    title: str = ""
    text: str = ""
    links: list[str] = field(default_factory=list[str])
    noindex: bool = False  # <meta name="robots" content="noindex"> — не додаємо в корпус
    nofollow: bool = False  # ... "nofollow" — не йдемо за посиланнями


class _Extractor(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base_url
        self.page = ParsedPage()
        self._skip_depth = 0
        self._in_title = False
        self._title: list[str] = []
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        if tag == "title":
            self._in_title = True
        elif tag in _SKIP:
            self._skip_depth += 1
        elif tag == "base" and a.get("href"):
            self.base = urljoin(self.base, a["href"])
        elif tag == "meta" and a.get("name", "").lower() == "robots":
            content = a.get("content", "").lower()
            self.page.noindex |= "noindex" in content or "none" in content
            self.page.nofollow |= "nofollow" in content or "none" in content
        elif tag == "a" and a.get("href") and "nofollow" not in a.get("rel", "").lower():
            self.page.links.append(urljoin(self.base, a["href"]))
        if tag in _BLOCK:
            self._text.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag in _SKIP and self._skip_depth:
            self._skip_depth -= 1
        if tag in _BLOCK:
            self._text.append("\n")

    def result(self) -> ParsedPage:
        self.page.title = " ".join("".join(self._title).split())
        text = _SPACES.sub(" ", "".join(self._text))
        lines = "\n".join(line.strip() for line in text.split("\n"))
        self.page.text = _LINES.sub("\n\n", lines).strip()
        return self.page

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title.append(data)
        elif not self._skip_depth:
            self._text.append(data)


def parse_html(html: str, base_url: str) -> ParsedPage:
    parser = _Extractor(base_url)
    parser.feed(html)
    parser.close()
    return parser.result()
