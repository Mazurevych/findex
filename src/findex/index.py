"""Інвертований індекс: терм -> відсортований за doc_id список постінгів.

Будується ОДНИМ проходом по лінивому конвеєру з лаби 1: документи надходять по
одному, для поточного документа рахуємо Counter (або позиції), після документа
«скидаємо» (flush) його в postings. Корпус у пам'яті не тримаємо — росте лише індекс.

doc_id видаються послідовно (0, 1, 2, ...), тож кожен список постінгів
автоматично відсортований: ми лише дописуємо в кінець більші doc_id.

Три способи зберігати постінги (для порівняння пам'яті, M4):
  * "dataclass" — list[PlainPosting]  (звичайний @dataclass, у кожного об'єкта є __dict__)
  * "slots"     — list[Posting]       (@dataclass(frozen=True, slots=True))
  * "array"     — ArrayPostings: два array('I') (doc_ids, tfs), без об'єкта на постінг

Запуск:
    python -m findex.index data/ --out index.bin              # будує і зберігає
    python -m findex.index data/ --out index.pkl --layout dataclass
    python -m findex.index data/ --out index.bin --positions  # з позиціями токенів
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import tracemalloc
from array import array
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path

from findex.corpus import Document, iter_documents
from findex.tokenize import tokenize

LAYOUTS = ("dataclass", "slots", "array")


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


@dataclass(frozen=True, slots=True)
class DocMeta:
    """Метадані для показу результату: шлях (doc_id з лаби 1) і заголовок."""

    path: str
    title: str


class ArrayPostings:
    """Постінги одного терма як «сирі» 4-байтові числа, без Python-об'єктів.

    doc_ids[i] і tfs[i] описують i-й постінг. Якщо увімкнено позиції, вони
    лежать в одному плоскому масиві: спершу tfs[0] позицій першого постінга,
    далі tfs[1] позицій другого і т. д.
    """

    __slots__ = ("doc_ids", "tfs", "positions")

    def __init__(self, with_positions: bool = False) -> None:
        self.doc_ids = array("I")
        self.tfs = array("I")
        self.positions: array | None = array("I") if with_positions else None

    def append(self, doc_id: int, tf: int, positions: Sequence[int] = ()) -> None:
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


# --- індекс ----------------------------------------------------------------------


@dataclass
class Index:
    postings: Mapping[str, object]  # терм -> list[Posting] | list[PlainPosting] | ArrayPostings
    doc_lengths: dict[int, int]  # doc_id -> кількість токенів (знадобиться для BM25 у лабі 3)
    doc_meta: dict[int, DocMeta]  # doc_id -> шлях і заголовок
    layout: str = "slots"
    with_positions: bool = False
    extra: dict = field(default_factory=dict)  # місце для службових даних (напр. відкритий mmap)

    @property
    def n_docs(self) -> int:
        return len(self.doc_lengths)

    @property
    def n_terms(self) -> int:
        return len(self.postings)

    def n_postings(self) -> int:
        return sum(len(p) for p in self.postings.values())

    def doc_ids(self, term: str) -> Sequence[int]:
        """Відсортовані doc_id документів, що містять терм (порожньо, якщо терма немає)."""
        plist = self.postings.get(term)
        if plist is None:
            return ()
        if isinstance(plist, ArrayPostings):
            return plist.doc_ids
        return [p.doc_id for p in plist]

    def iter_postings(self, term: str) -> Iterator[Posting | PlainPosting]:
        plist = self.postings.get(term)
        return iter(plist) if plist is not None else iter(())

    def df(self, term: str) -> int:
        """Document frequency — у скількох документах є терм."""
        plist = self.postings.get(term)
        return len(plist) if plist is not None else 0


def build_index(
    docs: Iterable[Document], *, layout: str = "slots", positions: bool = False
) -> Index:
    """Будує індекс за один прохід по потоку документів."""
    if layout not in LAYOUTS:
        raise ValueError(f"layout має бути одним з {LAYOUTS}, а не {layout!r}")

    if layout == "array":
        postings: defaultdict[str, object] = defaultdict(lambda: ArrayPostings(positions))
    else:
        postings = defaultdict(list)  # терм -> [постінги]; без «if term not in ...»
    record = PlainPosting if layout == "dataclass" else Posting

    doc_lengths: dict[int, int] = {}
    doc_meta: dict[int, DocMeta] = {}

    for doc_id, doc in enumerate(docs):
        # 1) Статистика ПОТОЧНОГО документа
        if positions:
            where: defaultdict[str, list[int]] = defaultdict(list)
            n_tokens = 0
            for n_tokens, term in enumerate(tokenize(doc.text), start=1):
                where[term].append(n_tokens - 1)
            per_doc = where
        else:
            counts = Counter(tokenize(doc.text))
            n_tokens = counts.total()
            per_doc = counts

        doc_lengths[doc_id] = n_tokens
        doc_meta[doc_id] = DocMeta(path=doc.doc_id, title=extract_title(doc.text, doc.doc_id))

        # 2) Flush: дописуємо постінги цього документа в кінець списків
        for term, value in per_doc.items():
            if positions:
                tf, pos = len(value), value
            else:
                tf, pos = value, ()
            if layout == "array":
                postings[term].append(doc_id, tf, pos)
            else:
                postings[term].append(record(doc_id, tf, tuple(pos)))

    return Index(
        postings=dict(postings),  # далі defaultdict не потрібен: пошук не має створювати ключі
        doc_lengths=doc_lengths,
        doc_meta=doc_meta,
        layout=layout,
        with_positions=positions,
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
            return line[len("Title:") :].strip()
    for cur, nxt in zip(lines, lines[1:], strict=False):
        stripped = cur.strip()
        if (
            stripped
            and not stripped.startswith("..")
            and not _UNDERLINE.match(cur)
            and _UNDERLINE.match(nxt)
        ):
            title = _RST_ROLE.sub(r"\1", stripped).replace("``", "")
            return title or fallback
    return fallback


# --- CLI ---------------------------------------------------------------------------


def human_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    raise AssertionError("unreachable")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m findex.index",
        description="Будує інвертований індекс корпусу і зберігає його у файл.",
    )
    p.add_argument("root", type=Path, help="каталог корпусу або файл .jsonl[.gz]")
    p.add_argument("--out", type=Path, required=True, help="файл індексу (.bin або .pkl)")
    p.add_argument(
        "--format",
        choices=("binary", "pickle"),
        default=None,
        help="формат файлу; за замовчуванням за розширенням (.pkl -> pickle, інше -> binary)",
    )
    p.add_argument("--layout", choices=LAYOUTS, default="slots", help="як зберігати постінги")
    p.add_argument("--positions", action="store_true", help="зберігати позиції токенів")
    p.add_argument("--limit", type=int, default=None, help="лише перші N документів")
    p.add_argument("--json", action="store_true", help="вивести вимірювання як JSON")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    from findex.store import save  # тут, щоб не було циклічного імпорту store <-> index

    args = parse_args(argv)
    fmt = args.format or ("pickle" if args.out.suffix == ".pkl" else "binary")

    tracemalloc.start()
    t0 = time.perf_counter()
    index = build_index(
        islice(iter_documents(args.root), args.limit),
        layout=args.layout,
        positions=args.positions,
    )
    build_s = time.perf_counter() - t0
    index_bytes, build_peak = tracemalloc.get_traced_memory()  # current ≈ сам індекс
    tracemalloc.stop()

    t0 = time.perf_counter()
    save(index, args.out, fmt=fmt)
    save_s = time.perf_counter() - t0
    size = args.out.stat().st_size

    result = {
        "layout": args.layout,
        "positions": args.positions,
        "format": fmt,
        "documents": index.n_docs,
        "terms": index.n_terms,
        "postings": index.n_postings(),
        "build_s": build_s,
        "build_peak_bytes": build_peak,
        "index_bytes": index_bytes,
        "save_s": save_s,
        "file_bytes": size,
    }
    if args.json:
        json.dump(result, sys.stdout)
        print()
        return 0

    print(f"Документів:        {index.n_docs:,}")
    print(f"Термів:            {index.n_terms:,}")
    print(f"Постінгів:         {result['postings']:,}")
    print(f"Постінги:          {args.layout}{' + позиції' if args.positions else ''}")
    print(f"Побудова:          {build_s:.2f} с, пік пам'яті {human_bytes(build_peak)}")
    print(f"Індекс у пам'яті:  {human_bytes(index_bytes)}")
    print(f"Збереження ({fmt}): {save_s:.2f} с -> {args.out} ({human_bytes(size)})")
    return 0


if __name__ == "__main__":
    # Пастка pickle: при `python -m findex.index` цей файл виконується як модуль
    # __main__, і класи Index/Posting стали б «__main__.Index». pickle записав би
    # у файл саме таку назву, а інший процес (findex.search) її не знайде.
    # Тому викликаємо main() зі справжнього модуля findex.index.
    from findex.index import main as _main

    raise SystemExit(_main())
