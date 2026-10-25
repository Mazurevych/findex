"""Лаба 2, M2: merge vs set на найчастіших, найрідкісніших і змішаних термах.

    uv run python -m findex.index data/ --out index.bin
    uv run python scripts/bench_search.py index.bin

Час — найкращий з 5 серій timeit (мікросекунди на один запит).
"""

from __future__ import annotations

import argparse
import random
import timeit

from findex.search import ENGINES
from findex.store import load


def best_us(fn, a, b) -> float:
    timer = timeit.Timer(lambda: fn(a, b))
    n, _ = timer.autorange()
    return min(timer.repeat(repeat=5, number=n)) / n * 1e6


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("index")
    args = p.parse_args()

    ix = load(args.index)
    by_df = sorted(ix.postings, key=lambda t: (ix.df(t), t))
    common = by_df[-2:][::-1]
    # рідкісні: два випадкові (seed фіксований) слова з мінімальним df — лише літери,
    # 6–10 символів, щоб не потрапили артефакти розмітки на кшталт «0'__» чи «aaaaa»
    min_df = ix.df(by_df[0])
    candidates = [t for t in by_df if ix.df(t) == min_df and t.isalpha() and 6 <= len(t) <= 10]
    rare = random.Random(42).sample(candidates, 2)
    cases = [
        ("2 найчастіші", common[0], common[1]),
        ("2 найрідкісніші", rare[0], rare[1]),
        ("частий + рідкісний", common[0], rare[0]),
    ]

    print("| Терми | df | Операція | merge | set | Хто швидший |")
    print("|---|---|---|---:|---:|---|")
    for label, t1, t2 in cases:
        a, b = ix.doc_ids(t1), ix.doc_ids(t2)
        for op in ("and_", "or_", "not_"):
            m = best_us(getattr(ENGINES["merge"], op), a, b)
            s = best_us(getattr(ENGINES["set"], op), a, b)
            name = {"and_": "AND", "or_": "OR", "not_": "NOT"}[op]
            winner = f"merge ×{s / m:.1f}" if m < s else f"set ×{m / s:.1f}"
            print(
                f"| {label}: `{t1}`, `{t2}` | {len(a)}, {len(b)} | {name} "
                f"| {m:,.1f} мкс | {s:,.1f} мкс | {winner} |"
            )


if __name__ == "__main__":
    main()
