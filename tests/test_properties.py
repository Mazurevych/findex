"""Hypothesis: властивості, що мають виконуватись для БУДЬ-ЯКИХ вхідних даних.

Замість прикладів формулюємо закон і даємо Hypothesis згенерувати сотні входів. Якщо
закон порушено, Hypothesis «стискає» (shrinking) контрприклад до найменшого — замість
списку з 40 чисел отримуємо, скажімо, `a=[0], b=[0, 1]`.
"""

from __future__ import annotations

import pytest
from conftest import make_index
from hypothesis import given, settings
from hypothesis import strategies as st

from findex.merge import difference, intersect, union
from findex.query import And, Node, Not, Or, Phrase, Term, parse
from findex.search import evaluate
from findex.tokenize import tokenize

sorted_ids = st.lists(st.integers(min_value=0, max_value=500), unique=True).map(sorted)


@given(sorted_ids, sorted_ids)
def test_merge_equals_set_algebra(a: list[int], b: list[int]) -> None:
    """Злиття = операції над множинами, а результат відсортований і без повторів."""
    assert intersect(a, b) == sorted(set(a) & set(b))
    assert union(a, b) == sorted(set(a) | set(b))
    assert difference(a, b) == sorted(set(a) - set(b))


@given(st.text())
def test_tokenize_is_idempotent(text: str) -> None:
    """Токени — нерухома точка: токенізація вже токенізованого тексту нічого не змінює."""
    tokens = list(tokenize(text))
    assert list(tokenize(" ".join(tokens))) == tokens


words = st.from_regex(r"[a-z]{1,6}", fullmatch=True)


def trees() -> st.SearchStrategy[Node]:
    leaves: st.SearchStrategy[Node] = st.one_of(
        words.map(Term),
        st.lists(words, min_size=2, max_size=3).map(lambda ws: Phrase(tuple(ws))),
    )
    return st.recursive(
        leaves,
        lambda kids: st.one_of(
            st.builds(And, kids, kids), st.builds(Or, kids, kids), kids.map(Not)
        ),
        max_leaves=8,
    )


@given(trees())
def test_parse_inverts_str(tree: Node) -> None:
    """Друк дерева і повторний розбір дають те саме дерево."""
    assert parse(str(tree)) == tree


TEXTS = ["a b c", "b c d", "c d e", "a e", "b", "a b c d e"]
INDEX = make_index(TEXTS, positions=True)
letters = st.sampled_from("abcdef").map(Term)


@given(letters, letters)
def test_boolean_laws_on_index(x: Term, y: Term) -> None:
    """De Morgan і доповнення: NOT(x OR y) == NOT x AND NOT y; x OR NOT x == усі документи."""
    assert evaluate(INDEX, str(Not(Or(x, y)))) == evaluate(INDEX, str(And(Not(x), Not(y))))
    assert evaluate(INDEX, f"{x} OR NOT {x}") == list(range(len(TEXTS)))
    assert evaluate(INDEX, f"{x} {y}") == evaluate(INDEX, f"{y} {x}")  # AND комутативний


@pytest.mark.slow
@settings(max_examples=1500, deadline=None)
@given(trees())
def test_parse_inverts_str_thoroughly(tree: Node) -> None:
    """Те саме, що вище, але 1500 прикладів (> 1 с) — позначено slow."""
    assert parse(str(tree)) == tree
