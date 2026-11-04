"""Лаба 3 M3: парсер запитів — тести як ДЕРЕВА, а не рядки; обчислення запитів."""

from __future__ import annotations

import pytest
from conftest import doc_by_name, make_index

from findex.index import Index
from findex.query import And, Not, Or, Phrase, QueryError, Term, parse
from findex.search import evaluate

a, b, c, d = Term("a"), Term("b"), Term("c"), Term("d")


@pytest.mark.parametrize(
    ("query", "tree"),
    [
        ("a", a),
        ("a b", And(a, b)),  # неявний AND
        ("a AND b", And(a, b)),
        ("a OR b c", Or(a, And(b, c))),  # AND зв'язує сильніше за OR
        ("a b OR c", Or(And(a, b), c)),
        ("(a OR b) c", And(Or(a, b), c)),  # дужки
        ("a b c", And(And(a, b), c)),  # ліва асоціативність
        ("a OR b OR c", Or(Or(a, b), c)),
        ("NOT a", Not(a)),
        ("a NOT b", And(a, Not(b))),
        ("NOT NOT a", Not(Not(a))),
        ("NOT (a OR b)", Not(Or(a, b))),
        ('"a b" c', And(Phrase(("a", "b")), c)),
        ('"a"', a),  # фраза з одного слова — просто терм
        ("A b", And(a, b)),  # слова нормалізуються як корпус: «A» -> «a»
        ("a or b", And(And(a, Term("or")), b)),  # оператори — лише ВЕЛИКИМИ
        ("e-mail", Phrase(("e", "mail"))),  # дефіс розбиває слово на фразу
        ("a — b", And(a, b)),  # «—» не дає токенів і відкидається
        ("((a))", a),
    ],
)
def test_parse_trees(query: str, tree: object) -> None:
    assert parse(query) == tree


def test_operators_build_the_same_trees() -> None:
    assert a & ~b == parse("a NOT b")
    assert (a | b) & c == parse("(a OR b) c")


def test_str_roundtrip() -> None:
    tree = Or(And(a, Not(Phrase(("b", "c")))), d)
    assert parse(str(tree)) == tree


@pytest.mark.parametrize("query", ["", "   ", "(a OR b", "a)", "a OR", '"unclosed', "NOT", "()"])
def test_syntax_errors(query: str) -> None:
    with pytest.raises(QueryError):
        parse(query)


@pytest.mark.parametrize("engine", ["merge", "set"])
def test_boolean_evaluation(index: Index, engine: str) -> None:
    eng = "merge" if engine == "merge" else "set"
    gen, asy, thr = (
        doc_by_name(index, n) for n in ("a-generators.txt", "b-asyncio.txt", "c-threads.txt")
    )
    ukr = doc_by_name(index, "f-ukr.txt")
    assert evaluate(index, "python asyncio", eng) == [thr]
    assert evaluate(index, "asyncio NOT threading", eng) == [asy]
    assert sorted(evaluate(index, "(asyncio OR generators) NOT threads", eng)) == sorted([gen])
    assert evaluate(index, "python NOT (asyncio OR lazy)", eng) == sorted([ukr])
    assert len(evaluate(index, "NOT python", eng)) == index.num_docs - 3


def test_phrase_needs_positions_and_is_order_sensitive(index: Index, pindex: Index) -> None:
    with pytest.raises(QueryError, match="positions"):
        evaluate(index, '"context manager"')
    ctx = doc_by_name(pindex, "e-context.md")
    assert evaluate(pindex, '"context manager"') == [ctx]
    assert evaluate(pindex, '"calls a context"') == [ctx]
    assert evaluate(pindex, '"context calls"') == []  # порядок має значення
    assert evaluate(pindex, '"generator expression"') == [doc_by_name(pindex, "d-genexpr.txt")]


def test_phrase_requires_adjacency() -> None:
    ix = make_index(["new york city", "new big york"], positions=True)
    assert evaluate(ix, '"new york"') == [0]
    assert evaluate(ix, "new york") == [0, 1]
