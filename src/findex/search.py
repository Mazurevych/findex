"""Boolean-пошук по збереженому індексу.

Синтаксис запиту (оператори — ВЕЛИКИМИ літерами, як у пошукових системах):
    python generator              -> python AND generator (неявний AND)
    asyncio OR threading          -> хоча б одне
    generator NOT async           -> перше без другого
    NOT deprecated                -> усі документи, де немає терма
Обчислення зліва направо, без дужок (справжній парсер — у лабі 3).
Кожне слово запиту проходить той самий tokenize, що й корпус: «Generators» -> «generators»,
а «e-mail» -> «e» AND «mail».

Два рушії (--engine):
    merge — двовказівникове злиття відсортованих списків doc_id, O(len(a) + len(b));
    set   — set(a) & set(b) і т. д. з подальшим сортуванням результату.

    python -m findex.search index.bin "generator expression"
    python -m findex.search index.bin "asyncio NOT threading" --engine set --show 20
"""

from __future__ import annotations

import argparse
import sys
import time
import tracemalloc
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from findex.index import Index, human_bytes
from findex.tokenize import tokenize

DocIds = Sequence[int]

# --- двовказівникове злиття (merge) ---------------------------------------------------


def intersect(a: DocIds, b: DocIds) -> list[int]:
    """AND: обидва списки відсортовані; рухаємо вказівник на меншому елементі."""
    out: list[int] = []
    i = j = 0
    na, nb = len(a), len(b)
    while i < na and j < nb:
        x, y = a[i], b[j]
        if x == y:
            out.append(x)
            i += 1
            j += 1
        elif x < y:
            i += 1
        else:
            j += 1
    return out


def union(a: DocIds, b: DocIds) -> list[int]:
    """OR: те саме злиття, але віддаємо кожен doc_id один раз."""
    out: list[int] = []
    i = j = 0
    na, nb = len(a), len(b)
    while i < na and j < nb:
        x, y = a[i], b[j]
        if x == y:
            out.append(x)
            i += 1
            j += 1
        elif x < y:
            out.append(x)
            i += 1
        else:
            out.append(y)
            j += 1
    out.extend(a[i:])
    out.extend(b[j:])
    return out


def difference(a: DocIds, b: DocIds) -> list[int]:
    """a NOT b: усе з a, чого немає в b."""
    out: list[int] = []
    i = j = 0
    na, nb = len(a), len(b)
    while i < na:
        if j >= nb:
            out.extend(a[i:])
            break
        x, y = a[i], b[j]
        if x == y:
            i += 1
            j += 1
        elif x < y:
            out.append(x)
            i += 1
        else:
            j += 1
    return out


# --- альтернатива на множинах ---------------------------------------------------------


def set_intersect(a: DocIds, b: DocIds) -> list[int]:
    return sorted(set(a) & set(b))


def set_union(a: DocIds, b: DocIds) -> list[int]:
    return sorted(set(a) | set(b))


def set_difference(a: DocIds, b: DocIds) -> list[int]:
    return sorted(set(a) - set(b))


@dataclass(frozen=True)
class Engine:
    and_: Callable[[DocIds, DocIds], list[int]]
    or_: Callable[[DocIds, DocIds], list[int]]
    not_: Callable[[DocIds, DocIds], list[int]]


ENGINES = {
    "merge": Engine(intersect, union, difference),
    "set": Engine(set_intersect, set_union, set_difference),
}


# --- обчислення запиту ----------------------------------------------------------------


def term_doc_ids(index: Index, word: str, engine: Engine) -> DocIds:
    """Одне слово запиту -> doc_id. Слово, що розпалось на кілька токенів, — це AND."""
    terms = list(tokenize(word))
    if not terms:
        return ()
    ids = index.doc_ids(terms[0])
    for term in terms[1:]:
        ids = engine.and_(ids, index.doc_ids(term))
    return ids


def evaluate(index: Index, query: str, engine: str = "merge") -> list[int]:
    """Обчислює запит зліва направо і повертає відсортовані doc_id."""
    eng = ENGINES[engine]
    all_docs = range(index.n_docs)  # doc_id ідуть підряд 0..N-1
    result: DocIds | None = None
    op, negate = "AND", False

    for word in query.split():
        if word in ("AND", "OR"):
            op = word
            continue
        if word == "NOT":
            negate = True
            continue

        ids = term_doc_ids(index, word, eng)
        if negate:
            if result is None:  # «NOT x» на початку — усі документи без x
                result = eng.not_(all_docs, ids)
            elif op == "AND":
                result = eng.not_(result, ids)
            else:  # «... OR NOT x»
                result = eng.or_(result, eng.not_(all_docs, ids))
        elif result is None:
            result = ids
        elif op == "AND":
            result = eng.and_(result, ids)
        else:
            result = eng.or_(result, ids)
        op, negate = "AND", False

    return list(result) if result is not None else []


# --- CLI ---------------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m findex.search",
        description="Boolean-пошук (AND / OR / NOT) по збереженому індексу.",
    )
    p.add_argument("index", type=Path, help="файл індексу (.bin або .pkl)")
    p.add_argument("query", help='запит, напр. "generator NOT async"')
    p.add_argument("--engine", choices=sorted(ENGINES), default="merge")
    p.add_argument("--show", type=int, default=10, help="скільки результатів показати")
    p.add_argument("--mmap", action="store_true", help="бінарний індекс: читати через mmap")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    from findex.store import load

    args = parse_args(argv)
    if not args.index.exists():
        print(f"Немає файлу індексу: {args.index}", file=sys.stderr)
        return 2

    tracemalloc.start()
    t0 = time.perf_counter()
    index = load(args.index, lazy=args.mmap)
    load_s = time.perf_counter() - t0

    t0 = time.perf_counter()
    hits = evaluate(index, args.query, engine=args.engine)
    search_s = time.perf_counter() - t0
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()

    print(f"Запит: {args.query!r}  (рушій: {args.engine})")
    print(f"Знайдено документів: {len(hits):,} з {index.n_docs:,}\n")
    for doc_id in hits[: args.show]:
        meta = index.doc_meta[doc_id]
        print(f"  [{doc_id:>4}] {meta.title}\n         {meta.path}")
    if len(hits) > args.show:
        print(f"  ... і ще {len(hits) - args.show:,}")
    print(f"\nЗавантаження індексу: {load_s * 1000:.1f} мс")
    print(f"Пошук:                {search_s * 1000:.2f} мс")
    print(f"Пікова пам'ять:       {human_bytes(peak)} (tracemalloc)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
