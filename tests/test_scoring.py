"""Лаба 3 M2/M4: TF-IDF і BM25, три sanity-перевірки, top-k, кеш, @timed, сніпети."""

from __future__ import annotations

import logging

import pytest
from conftest import doc_by_name, make_index

from findex.index import Index, Posting
from findex.scoring import BM25, Scorer, TfIdf, get_scorer
from findex.search import SearchResult, matching_doc_ids, search
from findex.snippets import make_snippet, snippet_for
from findex.timing import timed

FILLER = "filler " * 30


@pytest.mark.parametrize("scorer", [TfIdf(), BM25()])
def test_rare_term_outranks_common(scorer: Scorer) -> None:
    """Sanity 1: документ із рідкісним термом запиту вищий за документ лише з частим."""
    texts = [f"rare {FILLER}", f"common {FILLER}"] + [f"common {FILLER}"] * 8
    ix = make_index(texts)
    results = search(ix, "common OR rare", scorer=scorer, k=10)
    assert results[0].doc_id == 0


def test_bm25_saturates_term_frequency() -> None:
    """Sanity 2: 20-те повторення терма в BM25 майже нічого не додає, у TF-IDF — стільки ж."""
    bm25 = BM25()
    first = bm25.tf_part(1, 100, 100.0)
    twentieth = bm25.tf_part(20, 100, 100.0) - bm25.tf_part(19, 100, 100.0)
    assert twentieth < 0.01 * first
    assert bm25.tf_part(10**6, 100, 100.0) < bm25.k1 + 1  # стеля k1 + 1

    ix = make_index(["x " + FILLER, "x " * 20 + FILLER, "y"])
    tfidf = TfIdf()
    one, many = Posting(0, 1), Posting(1, 20)
    assert tfidf.score("x", many, ix) == pytest.approx(20 * tfidf.score("x", one, ix))


def test_short_document_outranks_long_one() -> None:
    """Sanity 3: один збіг у короткому документі важить більше, ніж в дуже довгому."""
    ix = make_index(["match short doc", "match " + "padding " * 500, "other"])
    results = search(ix, "match", scorer=BM25(), k=2)
    assert [r.doc_id for r in results] == [0, 1]
    assert results[0].score > results[1].score
    flat = BM25(b=0.0)  # b = 0: довжина документа ігнорується
    assert flat.score("match", Posting(0, 1), ix) == pytest.approx(
        flat.score("match", Posting(1, 1), ix)
    )


def test_scorers_satisfy_protocol_and_are_callable(index: Index) -> None:
    posting = next(iter(index["python"]))
    for name in ("bm25", "tfidf"):
        scorer: Scorer = get_scorer(name)  # структурно — без наслідування
        assert scorer.score("python", posting, index) >= 0
    assert BM25()("python", posting, index) == BM25().score("python", posting, index)


def test_search_top_k_and_result_ordering(index: Index) -> None:
    results = search(index, "python OR asyncio", k=2)
    assert len(results) == 2
    assert results[0].score >= results[1].score
    assert all(isinstance(r, SearchResult) for r in results)
    assert sorted(results) == sorted(results, key=lambda r: (r.doc_id, r.score))  # order=True
    assert search(index, "nonexistentword") == []


def test_repeated_query_hits_cache(index: Index, caplog: pytest.LogCaptureFixture) -> None:
    query = "lazy generators"
    search(index, query)
    before = matching_doc_ids.cache_info().hits
    with caplog.at_level(logging.INFO):
        search(index, query)
    assert matching_doc_ids.cache_info().hits == before + 1
    assert "match cache: hits=" in caplog.text
    assert any("search:" in r.message and "ms" in r.message for r in caplog.records)  # @timed


def test_timed_keeps_metadata(caplog: pytest.LogCaptureFixture) -> None:
    @timed
    def add(x: int, y: int) -> int:
        """Docstring."""
        return x + y

    with caplog.at_level(logging.INFO, logger="findex.timing"):
        assert add(2, 3) == 5
    assert add.__name__ == "add" and add.__doc__ == "Docstring."  # functools.wraps
    assert "add" in caplog.text and "ms" in caplog.text


def test_snippet_highlights_terms() -> None:
    text = ("intro " * 40) + "A Context Manager is used by the with statement. " + ("tail " * 40)
    snip = make_snippet(text, {"context", "manager"}, radius=30)
    rendered = snip.render("[", "]")
    assert "[Context] [Manager]" in rendered  # оригінальний регістр збережено
    assert snip.starts_mid and snip.ends_mid and rendered.startswith("…")
    assert len(rendered) < 120


def test_snippet_for_reads_document_file(pindex: Index) -> None:
    doc = doc_by_name(pindex, "e-context.md")
    assert "«context» «manager»" in snippet_for(pindex, doc, {"context", "manager"}).render()
