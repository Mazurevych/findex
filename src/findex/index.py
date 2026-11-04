"""Інвертований індекс: терм -> відсортований за doc_id список постінгів.

Лаба 2: побудова ОДНИМ проходом по лінивому конвеєру лаби 1 (`build_index`).
Лаба 3: словники лаби 2 стали класом `Index` — це `collections.abc.Mapping`
«терм -> постінги», тож працюють `len(ix)`, `term in ix`, `ix[term]`, `for t in ix`.

doc_id видаються послідовно (0, 1, 2, ...), тож кожен список постінгів
автоматично відсортований: ми лише дописуємо в кінець більші doc_id.

Три способи зберігати постінги (дослідження пам'яті, лаба 2 M4):
  * "dataclass" — list[PlainPosting]  (звичайний @dataclass, у кожного об'єкта є __dict__)
  * "slots"     — list[Posting]       (@dataclass(frozen=True, slots=True))
  * "array"     — ArrayPostings: два array('I') (doc_ids, tfs), без об'єкта на постінг
"""

from __future__ import annotations

import logging
import re
from array import array
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Literal, Protocol

from findex.corpus import Document
from findex.timing import timed
from findex.tokenize import tokenize

log = logging.getLogger(__name__)

type Layout = Literal["dataclass", "slots", "array"]
LAYOUTS: tuple[Layout, ...] = ("dataclass", "slots", "array")


# --- записи --------------------------------------------------------------------


@dataclass(frozen=True, slots=True, order=True)
class Posting:
    """Один постінг: документ, частота терма в ньому, (опційно) позиції токенів.

    frozen=True  -> незмінний і тому хешований (можна класти в set / робити ключем);
    slots=True   -> немає __dict__ у кожного екземпляра, об'єкт у рази менший;
    order=True   -> сортування за (doc_id, tf, positions).
    """

    doc_id: int
    tf: int
    positions: tuple[int, ...] = ()


@dataclass(frozen=True, order=True)
class PlainPosting:
    """Той самий запис БЕЗ slots — лише для вимірювання пам'яті (рядок 1 таблиці M4)."""

    doc_id: int
    tf: int
    positions: tuple[int, ...] = ()


class PostingLike(Protocol):
    """Будь-що з полями постінга (Posting, PlainPosting) — структурна типізація."""

    @property
    def doc_id(self) -> int: ...
    @property
    def tf(self) -> int: ...
    @property
    def positions(self) -> tuple[int, ...]: ...


class PostingList(Protocol):
    """Список постінгів одного терма: має довжину і віддає постінги по порядку doc_id."""

    def __len__(self) -> int: ...
    def __iter__(self) -> Iterator[PostingLike]: ...


@dataclass(frozen=True, slots=True)
class DocMeta:
    """Метадані для показу результату: шлях (відносно кореня корпусу) і заголовок."""

    path: str
    title: str


class ArrayPostings:
    """Постінги одного терма як «сирі» 4-байтові числа, без Python-об'єктів.

    doc_ids[i] і tfs[i] описують i-й постінг. Якщо увімкнено позиції, вони
    лежать в одному плоскому масиві: спершу tfs[0] позицій першого постінга,
    далі tfs[1] позицій другого і т. д.
    """

    __slots__ = ("doc_ids", "positions", "tfs")

    def __init__(self, with_positions: bool = False) -> None:
        self.doc_ids: array[int] = array("I")
        self.tfs: array[int] = array("I")
        self.positions: array[int] | None = array("I") if with_positions else None

    def append(self, doc_id: int, tf: int, positions: Iterable[int] = ()) -> None:
        self.doc_ids.append(doc_id)
        self.tfs.append(tf)
        if self.positions is not None:
            self.positions.extend(positions)

    def __len__(self) -> int:
        return len(self.doc_ids)

    def __iter__(self) -> Iterator[Posting]:
        """Віддає постінги як об'єкти Posting — ліниво, по одному."""
        offset = 0
        for doc_id, tf in zip(self.doc_ids, self.tfs, strict=True):
            if self.positions is None:
                yield Posting(doc_id, tf)
            else:
                yield Posting(doc_id, tf, tuple(self.positions[offset : offset + tf]))
                offset += tf

    def __repr__(self) -> str:
        return f"ArrayPostings(n={len(self)}, positions={self.positions is not None})"


# --- індекс ----------------------------------------------------------------------


class Index(Mapping[str, PostingList]):
    """Інвертований індекс як незмінний словник «терм -> постінги».

    Від `Mapping` безкоштовно отримуємо `get`, `keys`, `items`, `values` і `==`;
    самі реалізуємо `__getitem__`, `__len__`, `__iter__`, а `__contains__` —
    швидкий, без декодування постінгів (важливо для mmap-індексу).
    """

    def __init__(
        self,
        postings: Mapping[str, PostingList],
        doc_lengths: Sequence[int],
        doc_meta: Sequence[DocMeta],
        *,
        layout: Layout = "slots",
        with_positions: bool = False,
        corpus_root: str | None = None,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        if len(doc_lengths) != len(doc_meta):
            raise ValueError("doc_lengths і doc_meta мають однакову довжину")
        self._postings = postings
        self._doc_lengths = list(doc_lengths)
        self._doc_meta = list(doc_meta)
        self.layout: Layout = layout
        self.with_positions = with_positions
        self.corpus_root = corpus_root
        self._on_close = on_close
        self.closed = False

    # --- протокол Mapping ---

    def __getitem__(self, term: str) -> PostingList:
        return self._postings[term]  # KeyError, якщо терма немає — як у dict

    def __len__(self) -> int:
        return len(self._postings)  # кількість термів (розмір словника)

    def __iter__(self) -> Iterator[str]:
        return iter(self._postings)

    def __contains__(self, term: object) -> bool:
        return term in self._postings

    def __repr__(self) -> str:
        state = ", closed" if self.closed else ""
        return (
            f"<Index terms={len(self):,} docs={self.num_docs:,} layout={self.layout}"
            f" positions={self.with_positions}{state}>"
        )

    # Mapping визначає __eq__ (порівняння вмісту), а отже __hash__ = None. Індекс —
    # великий змінний ресурс (відкритий mmap), тож рівність і хеш — за ідентичністю:
    # тоді його можна передавати в lru_cache.
    def __eq__(self, other: object) -> bool:
        return self is other

    def __hash__(self) -> int:
        return id(self)

    # --- статистика для ранжування ---

    @property
    def num_docs(self) -> int:
        return len(self._doc_lengths)

    @cached_property
    def avg_doc_length(self) -> float:
        """Середня довжина документа в токенах. Рахується ОДИН раз — BM25 питає щоразу."""
        return sum(self._doc_lengths) / self.num_docs if self.num_docs else 0.0

    @cached_property
    def num_tokens(self) -> int:
        return sum(self._doc_lengths)

    def doc_length(self, doc_id: int) -> int:
        return self._doc_lengths[doc_id]

    def meta(self, doc_id: int) -> DocMeta:
        return self._doc_meta[doc_id]

    def df(self, term: str) -> int:
        """Document frequency — у скількох документах є терм (0, якщо немає)."""
        plist = self._postings.get(term)
        return len(plist) if plist is not None else 0

    def doc_ids(self, term: str) -> Sequence[int]:
        """Відсортовані doc_id документів, що містять терм (порожньо, якщо терма немає)."""
        plist = self._postings.get(term)
        if plist is None:
            return ()
        if isinstance(plist, ArrayPostings):
            return plist.doc_ids
        return [p.doc_id for p in plist]

    def iter_postings(self, term: str) -> Iterator[PostingLike]:
        plist = self._postings.get(term)
        return iter(plist) if plist is not None else iter(())

    def num_postings(self) -> int:
        return sum(len(p) for p in self._postings.values())

    @property
    def doc_lengths(self) -> list[int]:
        return self._doc_lengths

    @property
    def doc_metas(self) -> list[DocMeta]:
        return self._doc_meta

    def document_path(self, doc_id: int) -> Path | None:
        """Абсолютний шлях до файлу документа (для сніпетів), якщо корінь відомий."""
        if self.corpus_root is None:
            return None
        return Path(self.corpus_root) / self._doc_meta[doc_id].path

    # --- ресурси ---

    def close(self) -> None:
        """Звільняє ресурси (mmap). Повторний виклик безпечний."""
        if not self.closed:
            self.closed = True
            if self._on_close is not None:
                self._on_close()


# --- побудова ----------------------------------------------------------------------


@timed
def build_index(
    docs: Iterable[Document],
    *,
    layout: Layout = "slots",
    positions: bool = False,
    corpus_root: Path | str | None = None,
    progress: Callable[[], None] | None = None,
) -> Index:
    """Будує індекс за один прохід по потоку документів.

    `progress` (необов'язково) викликається після кожного документа — для індикатора.
    """
    if layout not in LAYOUTS:
        raise ValueError(f"layout має бути одним з {LAYOUTS}, а не {layout!r}")

    array_postings: defaultdict[str, ArrayPostings] = defaultdict(lambda: ArrayPostings(positions))
    list_postings: defaultdict[str, list[PostingLike]] = defaultdict(list)
    record: type[Posting] | type[PlainPosting] = PlainPosting if layout == "dataclass" else Posting

    doc_lengths: list[int] = []
    doc_meta: list[DocMeta] = []

    for doc_id, doc in enumerate(docs):
        # 1) Статистика ПОТОЧНОГО документа: терм -> позиції (або лише частота)
        where: defaultdict[str, list[int]] = defaultdict(list)
        counts: Counter[str] = Counter()
        n_tokens = 0
        if positions:
            for n_tokens, term in enumerate(tokenize(doc.text), start=1):
                where[term].append(n_tokens - 1)
        else:
            counts.update(tokenize(doc.text))
            n_tokens = counts.total()

        doc_lengths.append(n_tokens)
        doc_meta.append(DocMeta(path=doc.doc_id, title=extract_title(doc.text, doc.doc_id)))

        # 2) Flush: дописуємо постінги цього документа в кінець списків
        items: Iterable[tuple[str, int, Sequence[int]]] = (
            ((t, len(p), p) for t, p in where.items())
            if positions
            else ((t, tf, ()) for t, tf in counts.items())
        )
        for term, tf, pos in items:
            if layout == "array":
                array_postings[term].append(doc_id, tf, pos)
            else:
                list_postings[term].append(record(doc_id, tf, tuple(pos)))
        if progress is not None:
            progress()

    # далі defaultdict не потрібен: пошук не має створювати ключі
    final: dict[str, PostingList] = (
        dict(array_postings) if layout == "array" else dict(list_postings)
    )
    log.debug("indexed %d docs, %d terms", len(doc_lengths), len(final))
    return Index(
        final,
        doc_lengths,
        doc_meta,
        layout=layout,
        with_positions=positions,
        corpus_root=str(Path(corpus_root).resolve()) if corpus_root is not None else None,
    )


# --- заголовки документів ------------------------------------------------------

_UNDERLINE = re.compile(r"^\s*([=\-~^*#+`'\":.])\1{2,}\s*$")
_RST_ROLE = re.compile(r":[\w:+-]+:`!?([^`<]*?)(?:\s*<[^>]*>)?`")


def extract_title(text: str, fallback: str) -> str:
    """Заголовок документа reStructuredText.

    PEP: рядок «Title: ...» у шапці. Документація: перший рядок, під яким іде
    підкреслення з ===, ---, ~~~ тощо. Дивимось лише на початок тексту.
    """
    lines = text[:4000].splitlines()
    for line in lines:
        if line.startswith("Title:"):
            return line[len("Title:") :].strip().replace("``", "").replace("\\ ", "")
    for cur, nxt in zip(lines, lines[1:], strict=False):
        stripped = cur.strip()
        if (
            stripped
            and not stripped.startswith("..")
            and not _UNDERLINE.match(cur)
            and _UNDERLINE.match(nxt)
        ):
            title = _RST_ROLE.sub(r"\1", stripped).replace("``", "").replace("\\ ", "")
            return title or fallback
    return fallback


def human_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    raise AssertionError("unreachable")


if __name__ == "__main__":
    # `python -m findex.index ...` (лаба 2) = `findex index ...` (лаба 4).
    # Імпортуємо cli, а не виконуємо код тут: при `-m` цей файл — модуль __main__,
    # і pickle записав би класи як «__main__.Index», яких інший процес не знайде.
    from findex.cli import run_subcommand

    run_subcommand("index")
