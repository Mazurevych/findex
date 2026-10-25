"""Лаба 2: інвертований індекс, пошук, збереження."""

import dataclasses
import random

import pytest

from findex.corpus import Document
from findex.index import (
    LAYOUTS,
    ArrayPostings,
    DocMeta,
    PlainPosting,
    Posting,
    build_index,
    extract_title,
)
from findex.search import ENGINES, difference, evaluate, intersect, union
from findex.store import load, save

TEXTS = [
    "Python generators are lazy. Generators yield values.",  # 0
    "Asyncio uses coroutines, not threads.",  # 1
    "Threads and asyncio: python concurrency.",  # 2
    "A generator expression is lazy too.",  # 3
]


def docs():
    return (Document(f"d{i}.txt", None, t) for i, t in enumerate(TEXTS))


# --- записи -----------------------------------------------------------------------


def test_records_are_frozen_slotted_and_hashable():
    for cls in (Posting, DocMeta):
        params = cls.__dataclass_params__
        assert params.frozen
        assert "__slots__" in cls.__dict__ and not hasattr(cls(*_args(cls)), "__dict__")
    # хешовані -> можна класти в set, однакові значення = один елемент
    assert len({Posting(1, 2), Posting(1, 2), Posting(2, 1)}) == 2
    assert len({DocMeta("a", "A"), DocMeta("a", "A")}) == 1
    with pytest.raises(dataclasses.FrozenInstanceError):
        Posting(1, 2).tf = 3


def _args(cls):
    return (1, 2) if cls is Posting else ("p", "t")


def test_plain_posting_has_dict_but_slotted_does_not():
    assert hasattr(PlainPosting(1, 1), "__dict__")
    assert not hasattr(Posting(1, 1), "__dict__")


# --- побудова ---------------------------------------------------------------------


@pytest.mark.parametrize("layout", LAYOUTS)
def test_build_index_tf_df_sorted(layout):
    ix = build_index(docs(), layout=layout)
    assert ix.n_docs == 4
    assert list(ix.doc_ids("python")) == [0, 2]
    assert list(ix.doc_ids("asyncio")) == [1, 2]
    assert ix.df("lazy") == 2
    assert list(ix.doc_ids("nonexistent")) == []
    tf = {p.doc_id: p.tf for p in ix.iter_postings("generators")}
    assert tf == {0: 2}
    assert ix.doc_lengths[0] == 7
    for term in ix.postings:  # кожен список постінгів відсортований за doc_id
        ids = list(ix.doc_ids(term))
        assert ids == sorted(ids)


def test_build_is_single_pass_over_a_generator():
    ix = build_index(docs())  # генератор: пройти можна лише раз — і цього досить
    assert ix.n_docs == 4


@pytest.mark.parametrize("layout", LAYOUTS)
def test_positions(layout):
    ix = build_index(docs(), layout=layout, positions=True)
    (p,) = list(ix.iter_postings("generators"))
    assert p.tf == 2 and p.positions == (1, 4)


def test_array_postings_iterates_as_postings():
    ap = ArrayPostings(with_positions=True)
    ap.append(3, 2, [0, 5])
    ap.append(7, 1, [2])
    assert list(ap) == [Posting(3, 2, (0, 5)), Posting(7, 1, (2,))]


def test_extract_title():
    assert extract_title("PEP: 8\nTitle: Style Guide\nAuthor: x", "f") == "Style Guide"
    rst = ".. highlight:: python\n\n:mod:`!os` --- OS stuff\n=====================\n\nBody"
    assert extract_title(rst, "f") == "os --- OS stuff"
    assert extract_title("no title here", "fallback") == "fallback"


# --- пошук ------------------------------------------------------------------------


def test_merge_matches_sets_on_random_lists():
    rng = random.Random(0)
    for _ in range(200):
        a = sorted(rng.sample(range(100), rng.randint(0, 40)))
        b = sorted(rng.sample(range(100), rng.randint(0, 40)))
        assert intersect(a, b) == sorted(set(a) & set(b))
        assert union(a, b) == sorted(set(a) | set(b))
        assert difference(a, b) == sorted(set(a) - set(b))


@pytest.mark.parametrize("engine", sorted(ENGINES))
def test_boolean_queries(engine):
    ix = build_index(docs())
    assert evaluate(ix, "python asyncio", engine) == [2]  # неявний AND
    assert evaluate(ix, "python OR asyncio", engine) == [0, 1, 2]
    assert evaluate(ix, "asyncio NOT concurrency", engine) == [1]
    assert evaluate(ix, "NOT lazy", engine) == [1, 2]
    assert evaluate(ix, "lazy OR NOT python", engine) == [0, 1, 3]
    assert evaluate(ix, "PYTHON", engine) == [0, 2]  # запит нормалізується як корпус
    assert evaluate(ix, "missingword", engine) == []
    assert evaluate(ix, "", engine) == []


# --- збереження -------------------------------------------------------------------


@pytest.mark.parametrize("layout", LAYOUTS)
@pytest.mark.parametrize("fmt", ["pickle", "binary"])
@pytest.mark.parametrize("positions", [False, True])
def test_save_load_roundtrip(tmp_path, layout, fmt, positions):
    ix = build_index(docs(), layout=layout, positions=positions)
    path = tmp_path / ("ix.pkl" if fmt == "pickle" else "ix.bin")
    save(ix, path, fmt=fmt)
    for lazy in (False, True):
        back = load(path, lazy=lazy)
        assert back.n_docs == ix.n_docs and back.n_terms == ix.n_terms
        assert back.doc_meta == ix.doc_meta and back.doc_lengths == ix.doc_lengths
        for term in ix.postings:
            assert list(back.doc_ids(term)) == list(ix.doc_ids(term))
            assert [(p.doc_id, p.tf, p.positions) for p in back.iter_postings(term)] == [
                (p.doc_id, p.tf, p.positions) for p in ix.iter_postings(term)
            ]
        assert evaluate(back, "python NOT lazy") == [2]


def test_binary_rejects_foreign_file(tmp_path):
    bad = tmp_path / "x.bin"
    bad.write_bytes(b"not an index at all")
    with pytest.raises(ValueError):
        load(bad)  # не pickle і не FINDEX02 -> відмова, а не спроба виконати pickle
