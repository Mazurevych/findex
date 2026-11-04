"""Локальне HTML-дзеркало корпусу — «власний сайт» для чесного бенчмарку краулера (лаба 6).

    uv run python scripts/mirror_site.py data/ --port 8765 --latency 0.1

Кожен .rst корпусу стає сторінкою /peps/pep-0484.html або /docs/library/os.html; посилання
між сторінками беруться з тексту (:pep:`484`, «PEP 484», :mod:`os`) плюс «попередня /
наступна». Затримка `--latency` (± 50 %) імітує мережу: сервер багатопотоковий, тож
одночасні запити чекають паралельно — як справжній сервер в інтернеті.

Навмисні «аварії», щоб краулеру було що обробляти:
  /robots.txt          Disallow: /private/
  /flaky.html          503 двічі, потім 200       -> повтори з backoff
  /busy.html           429 Retry-After: 2 раз     -> чекаємо стільки, скільки просить сервер
  /loop-a.html         302 -> /loop-b.html -> 302 -> /loop-a.html   -> цикл переадресацій
  /missing.html        404                        -> без повторів
  /private/secret.html заборонено robots.txt      -> не завантажується взагалі
"""

from __future__ import annotations

import argparse
import contextlib
import html
import random
import re
import threading
import time
from collections import Counter
from functools import cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from findex.corpus import iter_documents
from findex.index import extract_title

_PEP_REF = re.compile(r":pep:`(?:[^`<]*<)?(\d+)|\bPEP[ -](\d{1,4})\b")
_MOD_REF = re.compile(r":mod:`!?~?([\w.]+)`")
TRAPS = ["/flaky.html", "/busy.html", "/loop-a.html", "/missing.html", "/private/secret.html"]


class Site:
    def __init__(self, root: Path) -> None:
        self.pages: dict[str, tuple[str, str]] = {}  # url-шлях -> (заголовок, текст)
        for doc in iter_documents(root):
            path = doc.doc_id
            if path.startswith("peps/"):
                url = "/" + path.removesuffix(".rst") + ".html"
            else:
                url = "/docs/" + path.removeprefix("python-docs/").removesuffix(".rst") + ".html"
            self.pages[url] = (extract_title(doc.text, path), doc.text)
        self.order = sorted(self.pages)
        self.modules = {
            u.removeprefix("/docs/library/").removesuffix(".html"): u
            for u in self.order
            if u.startswith("/docs/library/")
        }
        self.hits: Counter[str] = Counter()
        self.lock = threading.Lock()  # Counter спільний для потоків сервера -> замок

    @cache  # noqa: B019 — сайт живе весь час роботи сервера
    def render(self, url: str) -> str:
        title, text = self.pages[url]
        i = self.order.index(url)
        links: dict[str, None] = {}
        for m in _PEP_REF.finditer(text):
            target = f"/peps/pep-{int(m.group(1) or m.group(2)):04d}.html"
            if target in self.pages:
                links[target] = None
        for m in _MOD_REF.finditer(text):
            target = self.modules.get(m.group(1).split(".")[0])
            if target:
                links[target] = None
        nav = [self.order[(i - 1) % len(self.order)], self.order[(i + 1) % len(self.order)]]
        if i % 50 == 0:  # кожна 50-та сторінка має «пастки»
            nav += [*TRAPS, "mailto:docs@example.org", f"{url}#top", f"{url}?b=2&a=1"]
        body = "".join(f"<p>{html.escape(p)}</p>" for p in text.split("\n\n")[:400])
        anchors = "".join(f'<li><a href="{h}">{h}</a></li>' for h in [*nav, *links])
        return (
            f"<!doctype html><html><head><title>{html.escape(title)}</title>"
            f'<style>body{{font-family:sans-serif}}</style><script>var x="skip me";</script>'
            f"</head><body><nav><ul>{anchors}</ul></nav><h1>{html.escape(title)}</h1>"
            f"{body}</body></html>"
        )

    def index_page(self) -> str:
        hubs = self.order[:: max(1, len(self.order) // 40)]
        items = "".join(f'<li><a href="{u}">{html.escape(self.pages[u][0])}</a></li>' for u in hubs)
        return (
            f"<html><head><title>findex mirror</title></head><body><ul>{items}</ul></body></html>"
        )


def make_handler(site: Site, latency: float) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"  # keep-alive: клієнт перевикористовує з'єднання

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass  # тихо

        def send(
            self,
            status: int,
            body: str = "",
            ctype: str = "text/html; charset=utf-8",
            headers: dict[str, str] | None = None,
        ) -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802 — так вимагає http.server
            if latency:
                time.sleep(latency * random.uniform(0.5, 1.5))
            path = self.path.split("?", 1)[0]
            with site.lock:
                site.hits[path] += 1
                seen = site.hits[path]
            if path == "/robots.txt":
                return self.send(200, "User-agent: *\nDisallow: /private/\n", "text/plain")
            if path == "/":
                return self.send(200, site.index_page())
            if path == "/flaky.html":
                if seen <= 2:
                    return self.send(503, "try later")
                return self.send(200, "<title>Flaky</title><p>finally ok</p>")
            if path == "/busy.html":
                if seen == 1:
                    return self.send(429, "slow down", headers={"Retry-After": "2"})
                return self.send(200, "<title>Busy</title><p>thanks for waiting</p>")
            if path == "/loop-a.html":
                return self.send(302, headers={"Location": "/loop-b.html"})
            if path == "/loop-b.html":
                return self.send(302, headers={"Location": "/loop-a.html"})
            if path in site.pages:
                return self.send(200, site.render(path))
            return self.send(404, "<title>Not found</title>")

    return Handler


def serve(root: Path, port: int, latency: float) -> tuple[ThreadingHTTPServer, Site]:
    site = Site(root)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(site, latency))
    server.daemon_threads = True
    print(
        f"дзеркало: {len(site.pages):,} сторінок на http://127.0.0.1:{server.server_port}/ "
        f"(затримка {latency * 1000:.0f} мс ± 50 %)",
        flush=True,
    )
    return server, site


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("root", type=Path)
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--latency", type=float, default=0.1, help="секунд на запит (± 50 %)")
    args = p.parse_args()
    server, _ = serve(args.root, args.port, args.latency)
    with contextlib.suppress(KeyboardInterrupt):  # Ctrl-C — тиха зупинка
        server.serve_forever()


if __name__ == "__main__":
    main()
