"""Лаба 2 + лаба 3 M1: записи, побудова індексу, клас Index, збереження."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from conftest import docs_from, make_index

from findex.index import (
    LAYOUTS,
    ArrayPostings,
    DocMeta,
    Index,
    Layout,
    PlainPosting,
    Posting,
    build_index,
    extract_title,
)
from findex.search import evaluate
from findex.store import IndexFormatError, load, open_index, save

TEXTS = [
    "Python generators are lazy. Generators yield values.",  # 0
    "Asyncio uses coroutines, not threads.",  # 1
    "Threads and asyncio: python concurrency.",  # 2
    "A generator expression is lazy too.",  # 3
]


# --- записи (лаба 2) --------------------------------------------------------------


def test_records_are_frozen_slotted_and_hashable() -> None:
    for record in (Posting(1, 2), DocMeta("p", "t")):
        # службовий атрибут dataclass, якого немає в стабах типів:
        assert type(record).__dataclass_params__.frozen  # type: ignore[attr-defined]
        assert not hasattr(record, "__dict__")  # slots=True
    assert len({Posting(1, 2), Posting(1, 2), Posting(2, 1)}) == 2
    assert len({DocMeta("a", "A"), DocMeta("a", "A")}) == 1
    with pytest.raises(dataclasses.FrozenInstanceError):
        Posting(1, 2).tf = 3  # type: ignore[misc]  # навмисно: перевіряємо, що frozen забороняє


def test_plain_posting_has_dict_but_slotted_does_not() -> None:
    assert hasattr(PlainPosting(1, 1), "__dict__")
    assert not hasattr(Posting(1, 1), "__dict__")


def test_array_postings_iterates_as_postings() -> None:
    ap = ArrayPostings(with_positions=True)
    ap.append(3, 2, [0, 5])
    ap.append(7, 1, [2])
    assert list(ap) == [Posting(3, 2, (0, 5)), Posting(7, 1, (2,))]


# --- побудова --------------------------------------------------------------------


@pytest.mark.parametrize("layout", LAYOUTS)
def test_build_index_tf_df_sorted(layout: Layout) -> None:
    ix = make_index(TEXTS, layout=layout)
    assert ix.num_docs == 4
    assert list(ix.doc_ids("python")) == [0, 2]
    assert ix.df("lazy") == 2
    assert list(ix.doc_ids("nonexistent")) == []
    assert {p.doc_id: p.tf for p in ix.iter_postings("generators")} == {0: 2}
    assert ix.doc_length(0) == 7
    for term in ix:  # кожен список постінгів відсортований за doc_id
        ids = list(ix.doc_ids(term))
        assert ids == sorted(ids)


def test_build_is_single_pass_over_a_generator() -> None:
    assert build_index(docs_from(TEXTS)).num_docs == 4  # генератор: пройти можна лише раз


@pytest.mark.parametrize("layout", LAYOUTS)
def test_positions(layout: Layout) -> None:
    ix = make_index(TEXTS, layout=layout, positions=True)
    (p,) = list(ix.iter_postings("generators"))
    assert p.tf == 2 and p.positions == (1, 4)


def test_extract_title() -> None:
    assert extract_title("PEP: 8\nTitle: Style Guide\nAuthor: x", "f") == "Style Guide"
    rst = ".. highlight:: python\n\n:mod:`!os` --- OS stuff\n=====================\n\nBody"
    assert extract_title(rst, "f") == "os --- OS stuff"
    assert extract_title("no title here", "fallback") == "fallback"
    escaped = "Title: Handling ``ExceptionGroup``\\ s"
    assert extract_title(escaped, "f") == "Handling ExceptionGroups"


# --- клас Index (лаба 3 M1) ----------------------------------------------------------


def test_index_mapping_protocol(index: Index) -> None:
    assert len(index) == len(list(index))  # __len__ = кількість термів, __iter__ — терми
    assert "asyncio" in index and "nonexistent" not in index  # __contains__
    assert [p.doc_id for p in index["asyncio"]] == list(index.doc_ids("asyncio"))
    with pytest.raises(KeyError):
        index["nonexistent"]  # __getitem__ кидає KeyError, як dict
    assert index.get("nonexistent") is None  # get() безкоштовно від Mapping
    assert repr(index).startswith("<Index terms=") and "docs=6" in repr(index)


def test_index_statistics(index: Index) -> None:
    assert index.num_docs == 6
    assert index.avg_doc_length == pytest.approx(sum(index.doc_lengths) / 6)
    assert "avg_doc_length" in vars(index)  # cached_property: обчислено і збережено
    assert index.df("python") == 3


def test_index_is_hashable_by_identity(index: Index, pindex: Index) -> None:
    assert index == index and index != pindex
    assert len({index, index, pindex}) == 2


# --- збереження ---------------------------------------------------------------------


@pytest.mark.parametrize("layout", LAYOUTS)
@pytest.mark.parametrize("fmt", ["pickle", "binary"])
@pytest.mark.parametrize("positions", [False, True])
def test_save_load_roundtrip(tmp_path: Path, layout: Layout, fmt: str, positions: bool) -> None:
    ix = make_index(TEXTS, layout=layout, positions=positions)
    path = tmp_path / ("ix.pkl" if fmt == "pickle" else "ix.bin")
    save(ix, path, fmt="pickle" if fmt == "pickle" else "binary")
    for lazy in (False, True):
        with open_index(path, lazy=lazy) as back:
            assert back.num_docs == ix.num_docs and len(back) == len(ix)
            assert back.doc_metas == ix.doc_metas and back.doc_lengths == ix.doc_lengths
            for term in ix:
                got = [(p.doc_id, p.tf, p.positions) for p in back.iter_postings(term)]
                assert got == [(p.doc_id, p.tf, p.positions) for p in ix.iter_postings(term)]
            assert evaluate(back, "python NOT lazy") == [2]


def test_open_index_closes_mmap_even_on_exception(tmp_path: Path) -> None:
    path = tmp_path / "ix.bin"
    save(make_index(TEXTS), path)
    with pytest.raises(RuntimeError), open_index(path, lazy=True) as ix:
        assert not ix.closed
        raise RuntimeError("boom")
    assert ix.closed  # finally в open_index відпрацював


def test_load_rejects_foreign_file(tmp_path: Path) -> None:
    bad = tmp_path / "x.bin"
    bad.write_bytes(b"not an index at all")
    with pytest.raises(IndexFormatError):
        load(bad)  # не pickle і не FINDEX03 -> відмова, а не спроба виконати pickle
