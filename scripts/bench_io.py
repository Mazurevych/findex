"""Лаба 5, бонус: I/O-обмежена задача — тут потоки З GIL масштабуються.

    uv run python scripts/bench_io.py data/

Запускає локальне дзеркало (scripts/mirror_site.py) із затримкою 100 мс на запит і
завантажує 64 сторінки звичайним блокуючим urllib у ThreadPoolExecutor з 1…32 потоками.
Поки потік чекає відповіді з сокета, він ВІДПУСКАЄ GIL — тож потоки чекають паралельно.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from mirror_site import serve  # noqa: E402


def fetch(url: str) -> int:
    with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 — лише localhost
        return len(resp.read())


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("root", type=Path)
    p.add_argument("--pages", type=int, default=64)
    p.add_argument("--latency", type=float, default=0.1)
    args = p.parse_args()

    server, site = serve(args.root, 0, args.latency)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    urls = [base + u for u in site.order[: args.pages]]

    print(f"\n{len(urls)} сторінок, затримка {args.latency * 1000:.0f} мс ± 50 % на запит\n")
    print("| Потоків | Wall, с (медіана з 3) | Сторінок/с | Speedup |")
    print("|---:|---:|---:|---:|")
    base_wall = None
    for workers in (1, 2, 4, 8, 16, 32):
        walls = []
        for _ in range(3):
            t0 = time.perf_counter()
            with ThreadPoolExecutor(max_workers=workers) as pool:
                total = sum(pool.map(fetch, urls))
            walls.append(time.perf_counter() - t0)
        wall = statistics.median(walls)
        base_wall = base_wall or wall
        print(f"| {workers} | {wall:.2f} | {len(urls) / wall:.1f} | {base_wall / wall:.1f}× |")
        assert total > 0
    server.shutdown()


if __name__ == "__main__":
    main()
