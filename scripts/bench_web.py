"""Лаба 7, M2: `def` чи `async def` для /search — вирішуємо замірами.

    uv run python scripts/bench_web.py index.bin

Для кожного варіанту запускає `findex serve --handler sync|async` і одночасно шле
20 запитів /search (різні запити — щоб кеш не підміняв роботу) та «пінгує» /health
кожні 10 мс. Якщо обробник блокує цикл подій, /health чекає в черзі за пошуками.
"""

import argparse
import asyncio
import os
import socket
import statistics
import subprocess
import sys
import time
from pathlib import Path

import httpx

QUERIES = [
    "python", "asyncio", "generator", "type hints", "dataclass", "unicode", "thread",
    "context manager", "import", "exception", "decorator", "iterator", "module", "class",
    "function", "string", "file", "pattern", "lock", "memory",
]  # fmt: skip


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def measure(base: str) -> dict[str, float]:
    async with httpx.AsyncClient(base_url=base, timeout=60) as client:
        await client.get("/search", params={"q": "warmup"})
        stop = asyncio.Event()
        health: list[float] = []

        async def ping() -> None:
            while not stop.is_set():
                t0 = time.perf_counter()
                await client.get("/health")
                health.append((time.perf_counter() - t0) * 1000)
                await asyncio.sleep(0.01)

        async def one(q: str) -> float:
            t0 = time.perf_counter()
            r = await client.get("/search", params={"q": q, "k": 10})
            r.raise_for_status()
            return (time.perf_counter() - t0) * 1000

        pinger = asyncio.create_task(ping())
        await asyncio.sleep(0.05)
        t0 = time.perf_counter()
        lat = await asyncio.gather(*(one(q) for q in QUERIES))
        wall = time.perf_counter() - t0
        stop.set()
        await pinger
    return {
        "wall_s": wall,
        "search_p50": statistics.median(lat),
        "search_max": max(lat),
        "health_p50": statistics.median(health),
        "health_max": max(health),
    }


def run(index: Path, handler: str) -> dict[str, float]:
    port = free_port()
    env = {**os.environ, "INDEX_PATH": str(index), "LOG_LEVEL": "warning"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "findex", "serve", "--port", str(port), "--handler", handler],
        env=env, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                if httpx.get(base + "/health", timeout=1).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            time.sleep(0.1)
        return asyncio.run(measure(base))
    finally:
        proc.terminate()
        proc.wait()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("index", type=Path)
    p.add_argument("--runs", type=int, default=3)
    args = p.parse_args()
    print("| Обробник | 20 запитів, wall, с | /search p50, мс | /search max, мс "
          "| /health p50, мс | /health max, мс |")  # fmt: skip
    print("|---|---:|---:|---:|---:|---:|")
    for handler, label in (("sync", "`def` (пул потоків)"), ("async", "`async def` (цикл подій)")):
        runs = [run(args.index, handler) for _ in range(args.runs)]
        med = {k: statistics.median(r[k] for r in runs) for k in runs[0]}
        cells = [f"{med['wall_s']:.2f}", f"{med['search_p50']:.0f}", f"{med['search_max']:.0f}",
                 f"{med['health_p50']:.1f}", f"{med['health_max']:.0f}"]  # fmt: skip
        print(f"| {label} | " + " | ".join(cells) + " |", flush=True)


if __name__ == "__main__":
    main()
