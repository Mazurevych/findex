"""Статистика корпусу за ОДИН прохід + вимірювання часу й пікової пам'яті.

Запуск:
    python -m findex.stats data/                 # лінива версія (за замовчуванням)
    python -m findex.stats data/ --limit 100     # лише перші 100 документів (islice)
    python -m findex.stats data/ --mode eager    # «жадібна» версія на списках
    python -m findex.stats data/ --json          # машиночитаний вивід (для бенчмарку)

Лінивий конвеєр:
    iter_documents(root)  ->  islice(limit)  ->  tokenize(doc.text)  ->  Counter
    (генератор)               (генератор)        (генератор)             (єдине, що росте)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import tracemalloc
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from itertools import islice
from pathlib import Path

from findex.corpus import Document, iter_documents
from findex.tokenize import tokenize


@dataclass
class CorpusStats:
    documents: int
    tokens: int
    counts: Counter[str]

    @property
    def vocabulary(self) -> int:
        return len(self.counts)

    def top(self, n: int = 50) -> list[tuple[str, int]]:
        return self.counts.most_common(n)


# --- лінива версія ------------------------------------------------------------


def compute_stats(docs: Iterable[Document]) -> CorpusStats:
    """Один прохід по потоку документів.

    У пам'яті одночасно: поточний документ, його нормалізована копія,
    один токен — і Counter, що росте разом зі словником (не з корпусом).
    """
    counts: Counter[str] = Counter()
    n_docs = 0
    for doc in docs:
        n_docs += 1
        counts.update(tokenize(doc.text))  # Counter.update приймає будь-який ітерабельний
    return CorpusStats(documents=n_docs, tokens=counts.total(), counts=counts)


def run_lazy(root: Path, limit: int | None = None) -> CorpusStats:
    return compute_stats(islice(iter_documents(root), limit))


# --- «жадібна» версія (навмисно погана, для порівняння) ----------------------


def run_eager(root: Path, limit: int | None = None) -> CorpusStats:
    """Те саме, але кожен етап матеріалізується у список."""
    docs = list(islice(iter_documents(root), limit))  # 1) увесь корпус (зріз) у пам'яті
    token_lists = [list(tokenize(d.text)) for d in docs]  # 2) список списків токенів
    counts: Counter[str] = Counter()
    for tokens in token_lists:  # 3) лише тепер рахуємо
        counts.update(tokens)
    return CorpusStats(documents=len(docs), tokens=counts.total(), counts=counts)


MODES: dict[str, Callable[[Path, int | None], CorpusStats]] = {
    "lazy": run_lazy,
    "eager": run_eager,
}


# --- вимірювання -------------------------------------------------------------


@dataclass
class Measurement:
    stats: CorpusStats
    elapsed_s: float
    peak_bytes: int


def measure(fn: Callable[[], CorpusStats], trace: bool = True) -> Measurement:
    """Запускає fn під perf_counter і (за замовчуванням) tracemalloc.

    tracemalloc перехоплює КОЖНУ алокацію, тому сповільнює код у кілька разів.
    Для чесного часу запускайте з trace=False (peak_bytes тоді = 0).
    """
    if trace:
        tracemalloc.start()
    start = time.perf_counter()
    try:
        stats = fn()
        elapsed = time.perf_counter() - start
        peak = tracemalloc.get_traced_memory()[1] if trace else 0
    finally:
        if trace:
            tracemalloc.stop()
    return Measurement(stats=stats, elapsed_s=elapsed, peak_bytes=peak)


def human_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    raise AssertionError("unreachable")


# --- CLI ---------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m findex.stats",
        description="Статистика корпусу: документи, токени, словник, топ-N термів.",
    )
    p.add_argument("root", type=Path, help="каталог корпусу або файл .jsonl[.gz]")
    p.add_argument("--limit", type=int, default=None, help="обробити лише перші N документів")
    p.add_argument("--top", type=int, default=50, help="скільки найчастіших термів показати")
    p.add_argument("--mode", choices=sorted(MODES), default="lazy", help="lazy або eager")
    p.add_argument("--json", action="store_true", help="вивести результат як JSON")
    p.add_argument(
        "--no-trace", action="store_true", help="без tracemalloc: точніший час, без пам'яті"
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    if args.limit is not None and args.limit < 0:
        print("--limit має бути >= 0", file=sys.stderr)
        return 2

    run = MODES[args.mode]
    m = measure(lambda: run(args.root, args.limit), trace=not args.no_trace)
    s = m.stats

    if args.json:
        json.dump(
            {
                "mode": args.mode,
                "documents": s.documents,
                "tokens": s.tokens,
                "vocabulary": s.vocabulary,
                "elapsed_s": m.elapsed_s,
                "peak_bytes": m.peak_bytes,
                "top": s.top(args.top),
            },
            sys.stdout,
            ensure_ascii=False,
        )
        print()
        return 0

    print(f"Корпус:          {args.root}  (режим: {args.mode})")
    print(f"Документів:      {s.documents:,}")
    print(f"Токенів:         {s.tokens:,}")
    print(f"Словник:         {s.vocabulary:,} унікальних термів")
    print(f"Час:             {m.elapsed_s:.2f} с")
    if args.no_trace:
        print("Пікова пам'ять:  не вимірювалась (--no-trace)")
    else:
        print(f"Пікова пам'ять:  {human_bytes(m.peak_bytes)} (tracemalloc)")
    print(f"\nТоп-{args.top} термів:")
    for rank, (term, count) in enumerate(s.top(args.top), start=1):
        print(f"{rank:>4}. {term:<20} {count:>10,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
