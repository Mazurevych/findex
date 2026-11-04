"""Лаба 5, M4: гонка даних, яку ховає GIL і показує free-threaded Python.

    uv run python scripts/race_demo.py                              # з GIL
    uv run --isolated --python 3.13t python scripts/race_demo.py    # без GIL

1. `counter += 1` у кількох потоках — це НЕ одна операція (див. байт-код нижче):
   прочитати, додати, записати. Якщо інший потік встиг між читанням і записом,
   одне оновлення губиться.
2. Та сама помилка в «наївному» паралельному індексаторі: спільний словник частот,
   `counts[t] = counts.get(t, 0) + 1` з кількох потоків.
3. Виправлення: або `threading.Lock` навколо читання-зміни-запису, або (як у findex)
   прибрати спільний стан — кожен потік рахує у СВІЙ словник, а злиття робить один потік.
"""

from __future__ import annotations

import dis
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path

THREADS = 4
N = 200_000

counter = 0
lock = threading.Lock()


def unsafe() -> None:
    global counter
    for _ in range(N):
        counter += 1


def safe() -> None:
    global counter
    for _ in range(N):
        with lock:
            counter += 1


def run(target: Callable[[], None]) -> tuple[int, float]:
    global counter
    counter = 0
    threads = [threading.Thread(target=target) for _ in range(THREADS)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return counter, time.perf_counter() - t0


def tokens_of_corpus(limit: int = 200) -> list[list[str]]:
    from findex.corpus import iter_documents
    from findex.tokenize import tokenize

    root = Path(__file__).resolve().parent.parent / "data"
    docs: list[list[str]] = []
    for doc in iter_documents(root):
        docs.append(list(tokenize(doc.text)))
        if len(docs) == limit:
            break
    return docs


def shared_dict(docs: list[list[str]]) -> int:
    counts: dict[str, int] = {}

    def work(part: list[list[str]]) -> None:
        for tokens in part:
            for t in tokens:
                counts[t] = counts.get(t, 0) + 1  # читання-зміна-запис: гонка

    parts = [docs[i::THREADS] for i in range(THREADS)]
    threads = [threading.Thread(target=work, args=(p,)) for p in parts]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return sum(counts.values())


def private_counters(docs: list[list[str]]) -> int:
    results: list[Counter[str]] = [Counter() for _ in range(THREADS)]

    def work(i: int, part: list[list[str]]) -> None:
        for tokens in part:
            results[i].update(tokens)  # кожен потік — у СВІЙ Counter

    parts = [docs[i::THREADS] for i in range(THREADS)]
    threads = [threading.Thread(target=work, args=(i, p)) for i, p in enumerate(parts)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return sum(sum(c.values()) for c in results)  # злиття — в одному потоці


def main() -> None:
    gil = getattr(sys, "_is_gil_enabled", lambda: True)()
    print(
        f"Python {sys.version.split()[0]}, GIL {'увімкнено' if gil else 'ВИМКНЕНО'}, "
        f"{THREADS} потоки × {N:,} інкрементів\n"
    )
    print("Байт-код `counter += 1`:")
    dis.dis(compile("counter += 1", "<demo>", "exec"))

    expected = THREADS * N
    for name, fn in (("без замка", unsafe), ("з Lock", safe)):
        got, elapsed = run(fn)
        lost = expected - got
        print(
            f"{name:10}: {got:>9,} з {expected:,}  втрачено {lost:>8,} "
            f"({lost / expected:6.2%}), {elapsed:.2f} с"
        )

    docs = tokens_of_corpus()
    true_total = sum(len(d) for d in docs)
    print(f"\nПідрахунок токенів 200 документів корпусу ({true_total:,} токенів):")
    for name, fn in (("спільний dict", shared_dict), ("свій Counter", private_counters)):
        got = fn(docs)
        print(f"{name:14}: {got:>9,}  втрачено {true_total - got:,}")


if __name__ == "__main__":
    main()
