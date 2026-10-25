"""Порівняння eager vs lazy на кількох розмірах корпусу.

Кожен прогін — окремий процес (`python -m findex.stats ... --json`), щоб
алокації одного прогону не впливали на інший. Пам'ять міряється tracemalloc,
час — окремим прогоном без tracemalloc (він сповільнює код у рази).
Береться медіана з --repeat прогонів.

    uv run python scripts/benchmark.py data/ --limits 100 500 0 --repeat 3

Друкує Markdown-таблицю, готову для README. limit 0 = весь корпус.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys


def run_once(root: str, mode: str, limit: int, trace: bool) -> dict:
    cmd = [sys.executable, "-m", "findex.stats", root, "--mode", mode, "--json", "--top", "0"]
    if limit:
        cmd += ["--limit", str(limit)]
    if not trace:
        cmd.append("--no-trace")
    out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    return json.loads(out)


def mib(n: float) -> str:
    return f"{n / 1024 / 1024:.1f} MiB"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("root")
    p.add_argument("--limits", type=int, nargs="+", default=[100, 500, 0])
    p.add_argument("--repeat", type=int, default=3)
    args = p.parse_args()

    print("| Версія | Документів | Токенів | Пікова пам'ять | Час | Час під tracemalloc |")
    print("|---|---:|---:|---:|---:|---:|")
    for limit in args.limits:
        for mode in ("eager", "lazy"):
            traced = [run_once(args.root, mode, limit, trace=True) for _ in range(args.repeat)]
            plain = [run_once(args.root, mode, limit, trace=False) for _ in range(args.repeat)]
            r = traced[0]
            peak = statistics.median(x["peak_bytes"] for x in traced)
            t_traced = statistics.median(x["elapsed_s"] for x in traced)
            t_plain = statistics.median(x["elapsed_s"] for x in plain)
            label = "eager (списки)" if mode == "eager" else "lazy (генератори)"
            print(
                f"| {label} | {r['documents']:,} | {r['tokens']:,} | {mib(peak)} "
                f"| {t_plain:.2f} с | {t_traced:.2f} с |",
                flush=True,
            )


if __name__ == "__main__":
    main()
