"""Спільні фікстури: крихітний корпус на диску і індекси, побудовані з нього.

Індекси — scope="session": будуються ОДИН раз на весь прогін тестів. Тому їх не можна
змінювати в тестах — інакше наступний тест побачив би чужі зміни і результат залежав би
від порядку запуску. `Index` не має публічних методів зміни, а тести, яким потрібен
«свій» індекс, будують його самі (`make_index`).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from findex.corpus import Document, iter_documents
from findex.index import Index, Layout, build_index

TEXTS: dict[str, str] = {
    "a-generators.txt": "Python generators are lazy. Generators yield values one at a time.",
    "b-asyncio.txt": "Asyncio uses coroutines and an event loop, not threads.",
    "c-threads.txt": "Threads and asyncio: Python concurrency with the threading module.",
    "d-genexpr.txt": "A generator expression is lazy too: sum(x for x in range(10)).",
    "e-context.md": "The with statement calls a context manager. Context managers close files.",
    "f-ukr.txt": "Генератори в Python ліниві: п'ять значень по одному.",
}


@pytest.fixture(scope="session")
def corpus_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("corpus")
    for name, text in TEXTS.items():
        (root / name).write_text(text, encoding="utf-8")
    return root


def docs_from(texts: list[str]) -> Iterator[Document]:
    return (Document(f"d{i}.txt", Path(f"d{i}.txt"), t) for i, t in enumerate(texts))


def make_index(texts: list[str], *, positions: bool = False, layout: Layout = "slots") -> Index:
    return build_index(docs_from(texts), positions=positions, layout=layout)


@pytest.fixture(scope="session")
def index(corpus_dir: Path) -> Index:
    """Індекс фікстурного корпусу без позицій."""
    return build_index(iter_documents(corpus_dir), corpus_root=corpus_dir)


@pytest.fixture(scope="session")
def pindex(corpus_dir: Path) -> Index:
    """Той самий корпус з позиціями (для фраз і сніпетів)."""
    return build_index(iter_documents(corpus_dir), positions=True, corpus_root=corpus_dir)


def doc_by_name(ix: Index, name: str) -> int:
    for doc_id in range(ix.num_docs):
        if ix.meta(doc_id).path == name:
            return doc_id
    raise KeyError(name)
