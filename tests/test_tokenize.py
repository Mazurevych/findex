"""Перевірки токенізатора (лаба 1). Запуск: uv run pytest"""

import inspect

from findex.tokenize import tokenize


def toks(text: str) -> list[str]:
    return list(tokenize(text))


def test_is_generator():
    assert inspect.isgeneratorfunction(tokenize)
    assert next(tokenize("Hello world")) == "hello"


def test_mixed_case_and_casefold():
    assert toks("Hello WORLD hElLo") == ["hello", "world", "hello"]
    # casefold, а не lower: ß -> ss
    assert toks("Straße") == toks("STRASSE") == ["strasse"]


def test_cyrillic():
    assert toks("Київ — столиця України!") == ["київ", "столиця", "україни"]
    assert toks("ЇЖАК і Ґанок") == ["їжак", "і", "ґанок"]


def test_combining_mark_nfc():
    # «é» одним кодом vs «e» + U+0301 — однаковий токен
    assert toks("café") == toks("café") == ["café"]


def test_orphan_combining_mark_removed():
    # наголос над кириличною «а» не має готового символу -> знак видаляється,
    # слово не розпадається на частини
    assert toks("за́мок") == ["замок"]
    # але «й» (и + бреве) після NFC — ціла літера і не псується
    assert toks("йти") == ["йти"]


def test_apostrophes():
    assert toks("don't") == ["don't"]
    # український апостроф ʼ і типографський ’ зводяться до '
    assert toks("пʼять") == toks("п’ять") == toks("п'ять") == ["п'ять"]
    # апострофи-лапки на краях відкидаються
    assert toks("'hello'") == ["hello"]


def test_hyphens_split():
    assert toks("e-mail state-of-the-art") == ["e", "mail", "state", "of", "the", "art"]


def test_digits_and_underscore():
    assert toks("Python 3.13 вийшов у 2024") == ["python", "3", "13", "вийшов", "у", "2024"]
    assert toks("call __init__ or sys_path") == ["call", "__init__", "or", "sys_path"]


def test_punctuation_only_and_empty():
    assert toks("") == []
    assert toks("  ...!?, — ") == []
