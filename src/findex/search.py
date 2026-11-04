"""Пошук: розбір запиту -> множина документів -> ранжування -> top-k.

    with open_index("index.bin") as ix:
        for r in search(ix, '"generator expression" NOT async', scorer=BM25(), k=10):
            print(r.score, r.title)

1. `parse` перетворює рядок на дерево (findex.query), дерево обчислює відсортовані doc_id
   злиттям (лаба 2). Цей шлях «запит -> doc_id» кешується `functools.lru_cache`:
   повторний запит не розбирається і не зливає списки вдруге.
2. Для кожного терма запиту (крім тих, що під NOT) проходимо його постінги й додаємо
   `scorer.score(...)` документам із знайденої множини.
3. `heapq.nlargest(k, ...)` — O(n log k) замість O(n log n) у `sorted(...)[:k]`.

Злиття (intersect / union / difference / ENGINES) живуть у findex.merge і
реекспортуються тут для зворотної сумісності з лабою 2.
"""

from __future__ import annotations

import heapq
import logging
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache

from findex.index import Index
from findex.merge import (
    ENGINES,
    Engine,
    EngineName,
    difference,
    intersect,
    set_difference,
    set_intersect,
    set_union,
    union,
)
from findex.query import Node, parse
from findex.scoring import BM25, Scorer
from findex.timing import timed

__all__ = [
    "ENGINES",
    "Engine",
    "SearchResult",
    "difference",
    "evaluate",
    "intersect",
    "matching_doc_ids",
    "query_terms",
    "search",
    "set_difference",
    "set_intersect",
    "set_union",
    "union",
]

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, order=True)
class SearchResult:
    """Один результат. order=True: `sorted(results)` сортує за (doc_id, score, title)."""

    doc_id: int
    score: float
    title: str
    path: str


@lru_cache(maxsize=1024)
def compile_query(query: str) -> Node:
    """Рядок -> дерево (кешовано: дерева незмінні, тож їх безпечно ділити)."""
    return parse(query)


@lru_cache(maxsize=256)
def matching_doc_ids(index: Index, query: str, engine: EngineName = "merge") -> tuple[int, ...]:
    """Запит -> відсортовані doc_id. Кеш за (індекс, запит, рушій).

    Індекс хешується за ідентичністю (див. Index.__hash__). Кортеж, а не список: результат
    кешу ділять усі виклики, тож він має бути незмінним.
    """
    return tuple(compile_query(query).evaluate(index, engine))


def query_terms(query: str) -> frozenset[str]:
    """Терми запиту, що підвищують оцінку (усе, крім частин під NOT) — для сніпетів."""
    return frozenset(compile_query(query).positive_terms())


def evaluate(index: Index, query: str, engine: EngineName = "merge") -> list[int]:
    """Лаба 2: лише Boolean-збіг, без ранжування."""
    return list(matching_doc_ids(index, query, engine))


@timed
def search(
    index: Index,
    query: str,
    scorer: Scorer | None = None,
    k: int = 10,
    engine: EngineName = "merge",
) -> list[SearchResult]:
    """Top-k документів за запитом, найкращі першими."""
    scorer = scorer if scorer is not None else BM25()
    matched = matching_doc_ids(index, query, engine)
    info = matching_doc_ids.cache_info()
    log.info("match cache: hits=%d misses=%d size=%d", info.hits, info.misses, info.currsize)
    if not matched or k <= 0:
        return []

    wanted = set(matched)
    scores: defaultdict[int, float] = defaultdict(float)
    for term in dict.fromkeys(compile_query(query).positive_terms()):  # без повторів, у порядку
        for posting in index.iter_postings(term):
            if posting.doc_id in wanted:
                scores[posting.doc_id] += scorer.score(term, posting, index)

    # за рівної оцінки — менший doc_id першим (детермінований порядок)
    top = heapq.nlargest(k, matched, key=lambda d: (scores.get(d, 0.0), -d))
    return [
        SearchResult(d, scores.get(d, 0.0), index.meta(d).title, index.meta(d).path) for d in top
    ]


if __name__ == "__main__":
    # `python -m findex.search ...` (лаба 2) = `findex search ...` (лаба 4)
    from findex.cli import run_subcommand

    run_subcommand("search")
