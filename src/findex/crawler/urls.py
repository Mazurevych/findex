"""Канонізація URL: одна сторінка — один рядок, щоб `seen` не пропускав дублікати.

    HTTPS://Docs.Example.org:443/a/./b/?z=1&a=2#part  ->  https://docs.example.org/a/b/?a=2&z=1

* схема і хост — малими літерами, порт за замовчуванням (80/443) прибираємо;
* `#фрагмент` прибираємо: це та сама сторінка;
* параметри запиту сортуємо: `?b=2&a=1` == `?a=1&b=2`;
* порожній шлях -> `/`, `.` і `..` розкриваються;
* кінцевий слеш: якщо в останньому сегменті немає крапки (`/guide`), це «каталог» ->
  `/guide/`; а `/page.html` лишається без слеша. Так `/guide` і `/guide/` не стають двома
  сторінками і не витрачають зайвий запит на переадресацію.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

DEFAULT_PORTS = {"http": 80, "https": 443}
SKIP_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".pdf", ".zip", ".gz", ".tgz",
    ".tar", ".whl", ".exe", ".dmg", ".mp3", ".mp4", ".webm", ".css", ".js", ".woff", ".woff2",
    ".ttf", ".epub", ".json", ".xml",
)  # fmt: skip


def canonicalize(url: str, base: str | None = None) -> str | None:
    """Канонічний URL або None, якщо це не http(s)-сторінка (mailto:, javascript:, …)."""
    absolute = urljoin(base, url.strip()) if base else url.strip()
    try:
        parts = urlsplit(absolute)
        port = parts.port
    except ValueError:  # кривий порт чи IPv6
        return None
    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS or not parts.hostname:
        return None
    host = parts.hostname.lower()
    netloc = host if port in (None, DEFAULT_PORTS[scheme]) else f"{host}:{port}"
    path = urlsplit(urljoin(f"{scheme}://{netloc}/", parts.path or "/")).path or "/"
    last = path.rsplit("/", 1)[-1]
    if last and "." not in last:
        path += "/"
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return urlunsplit((scheme, netloc, path, query, ""))


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def host_of(url: str) -> str:
    return urlsplit(url).netloc


def looks_like_page(url: str) -> bool:
    """Відсіюємо очевидні не-HTML за розширенням ще до запиту."""
    return not urlsplit(url).path.lower().endswith(SKIP_EXTENSIONS)


def in_scope(url: str, domains: frozenset[str], prefixes: tuple[str, ...]) -> bool:
    """URL дозволено обходити: хост з allowlist (або його піддомен) і, якщо задано, префікс."""
    host = urlsplit(url).hostname or ""
    if domains and not any(host == d or host.endswith("." + d) for d in domains):
        return False
    return not prefixes or url.startswith(prefixes)
