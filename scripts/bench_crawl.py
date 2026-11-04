"""Лаба 6, M4: краулер на однаковому seed і бюджеті при concurrency 1 / 5 / 20.

    uv run python scripts/bench_crawl.py data/

Ціль — локальне дзеркало корпусу (scripts/mirror_site.py) з затримкою 100 мс ± 50 % на
запит: це НАШ сервер, тож краулити його дозволено, а результат відтворюваний. Для кожного
прогону сервер перезапускається (щоб «пастки» на кшталт /flaky.html поводились однаково).

Рядки:
  * concurrency 1 / 5 / 20, per-host = concurrency, без паузи — скільки дає асинхронність;
  * concurrency 20, per-host 2, пауза 50 мс — «ввічливий» режим: що коштують ліміти на хост;
  * ще один прогін з asyncio debug=True — рахуємо попередження про блокування циклу.
"""

from __future__ import annotations

import argparse
import json
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def start_mirror(corpus: str, latency: float) -> tuple[subprocess.Popen[str], str]:
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "mirror_site.py"), corpus, "--port", str(port),
         "--latency", str(latency)],
        stdout=subprocess.PIPE, text=True,
    )  # fmt: skip
    assert proc.stdout is not None
    proc.stdout.readline()  # «дзеркало: ... сторінок» — сервер готовий
    for _ in range(50):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                break
        time.sleep(0.1)
    return proc, f"http://127.0.0.1:{port}/"


def crawl_once(corpus: str, latency: float, pages: int, c: int, per_host: int, delay: float,
               tmp: Path, debug: bool = False) -> dict[str, float]:  # fmt: skip
    proc, seed = start_mirror(corpus, latency)
    try:
        cmd = [sys.executable, "-m", "findex", "crawl", seed, "--max-pages", str(pages),
               "--concurrency", str(c), "--per-host", str(per_host), "--delay", str(delay),
               "--out", str(tmp / "bench.jsonl"), "--log", str(tmp / f"crawl-c{c}.log"),
               "--json", "--no-live"]  # fmt: skip
        if debug:
            cmd.append("--debug")
        res = subprocess.run(cmd, check=True, capture_output=True, text=True)
        summary = json.loads(res.stdout.strip().splitlines()[-1])
        summary["slow_callbacks"] = res.stderr.count("took ")  # asyncio: "Executing ... took"
        return summary
    finally:
        proc.terminate()
        proc.wait()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("corpus")
    p.add_argument("--pages", type=int, default=200)
    p.add_argument("--latency", type=float, default=0.1)
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--tmp", type=Path, default=Path(tempfile.mkdtemp(prefix="findex-crawl-")))
    args = p.parse_args()
    args.tmp.mkdir(parents=True, exist_ok=True)

    configs = [(1, 1, 0.0, "1 (послідовно)"), (5, 5, 0.0, "5"), (20, 20, 0.0, "20"),
               (20, 2, 0.05, "20, але per-host 2 + пауза 50 мс")]  # fmt: skip
    print(f"Дзеркало: затримка {args.latency * 1000:.0f} мс ± 50 %, бюджет {args.pages} "
          f"сторінок, медіана з {args.runs}\n")  # fmt: skip
    print("| Concurrency | Сторінок | Wall, с | Сторінок/с | Помилок | Макс. у польоті | Пік RSS |")
    print("|---|---:|---:|---:|---:|---:|---:|")
    for c, per_host, delay, label in configs:
        runs = [crawl_once(args.corpus, args.latency, args.pages, c, per_host, delay, args.tmp)
                for _ in range(args.runs)]  # fmt: skip
        med = {k: statistics.median(r[k] for r in runs)
               for k in ("pages", "wall_s", "pages_per_s", "errors", "max_in_flight",
                         "peak_rss_bytes")}  # fmt: skip
        print(f"| {label} | {med['pages']:.0f} | {med['wall_s']:.2f} | {med['pages_per_s']:.1f} "
              f"| {med['errors']:.0f} | {med['max_in_flight']:.0f} "
              f"| {med['peak_rss_bytes'] / 2**20:.0f} MiB |", flush=True)  # fmt: skip

    dbg = crawl_once(args.corpus, args.latency, args.pages, 20, 20, 0.0, args.tmp, debug=True)
    print(
        f"\nasyncio debug=True, concurrency 20: {dbg['pages']} сторінок, "
        f"попереджень про блокування циклу (> 100 мс): {dbg['slow_callbacks']}"
    )


if __name__ == "__main__":
    main()
