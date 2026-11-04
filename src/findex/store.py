"""Збереження індексу на диск і завантаження назад. Два формати.

1. pickle (файли .pkl)
   Одним викликом серіалізує будь-який граф Python-об'єктів — швидко і просто.

   !!! НЕБЕЗПЕКА: pickle.load на ЧУЖОМУ файлі = виконання довільного коду. !!!
   Формат pickle — це програма для маленької віртуальної машини. Інструкція
   REDUCE викликає будь-яку функцію з будь-якими аргументами, а __reduce__
   дозволяє автору файлу вказати, що саме викликати, наприклад os.system("...").
   Код виконується ПІД ЧАС load, ще до того, як ми побачимо результат.
   Тому pickle — лише для файлів, які ми створили самі (наш індекс),
   і ніколи — для файлів, завантажених з мережі чи отриманих від інших.

2. Бінарний формат (файли .bin) — безпечний: при завантаженні виконується лише
   json.loads і array.frombytes, жодного коду з файлу.

       FINDEX03                   8 байтів — «магічне число» (сигнатура формату)
       <довжина заголовка>        8 байтів, little-endian uint64
       <заголовок JSON, UTF-8>    метадані документів + терм -> [зсув, к-сть, к-сть позицій]
       <тіло>                     для кожного терма поспіль: doc_ids, tfs, [позиції]
                                  — масиви array('I'), 4 байти на число

   Завантаження: eager (усе в пам'ять) або mmap (lazy=True) — тоді постінги
   читаються з файлу лише для тих термів, які реально шукаємо.

`open_index(path)` — контекстний менеджер: гарантує закриття mmap навіть при винятку.
"""

from __future__ import annotations

import json
import mmap
import pickle
import struct
import sys
from array import array
from collections.abc import Generator, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal, cast

from findex.index import ArrayPostings, DocMeta, Index, PostingList
from findex.timing import timed

type Format = Literal["binary", "pickle"]

MAGIC = b"FINDEX03"
_LEN = struct.Struct("<Q")
_ITEMSIZE = array("I").itemsize  # 4 на всіх поширених платформах


class IndexFormatError(ValueError):
    """Файл не є індексом findex або має несумісну версію."""


# --- публічний API ---------------------------------------------------------------


@timed
def save(index: Index, path: Path | str, fmt: Format = "binary") -> None:
    path = Path(path)
    if fmt == "pickle":
        save_pickle(index, path)
    else:
        save_binary(index, path)


@timed
def load(path: Path | str, *, lazy: bool = False) -> Index:
    """Визначає формат за сигнатурою файлу і завантажує індекс."""
    path = Path(path)
    with path.open("rb") as f:
        head = f.read(len(MAGIC))
    if head == MAGIC:
        return load_binary(path, lazy=lazy)
    if head.startswith(b"FINDEX"):
        raise IndexFormatError(f"{path}: стара версія формату, перебудуйте індекс")
    if head[:1] == b"\x80":  # pickle протоколу 2+ починається з байта 0x80
        return load_pickle(path)
    raise IndexFormatError(f"{path}: невідомий формат файлу індексу")


@contextmanager
def open_index(path: Path | str, *, lazy: bool = False) -> Generator[Index]:
    """`with open_index("index.bin") as ix: ...` — закриває індекс за будь-якого виходу.

    До `yield` — завантаження; після — `close()` у `finally`, тож mmap звільняється
    і тоді, коли тіло `with` кинуло виняток (виняток летить далі, ми його не ковтаємо).
    """
    index = load(path, lazy=lazy)
    try:
        yield index
    finally:
        index.close()


# --- pickle ---------------------------------------------------------------------------


def save_pickle(index: Index, path: Path) -> None:
    with path.open("wb") as f:
        pickle.dump(index, f, protocol=pickle.HIGHEST_PROTOCOL)


def load_pickle(path: Path) -> Index:
    # УВАГА: лише для власних файлів! Див. пояснення на початку модуля:
    # pickle.load на файлі від когось іншого може виконати довільний код.
    with path.open("rb") as f:
        index: object = pickle.load(f)  # noqa: S301 — свідомо, файл створено нами
    if not isinstance(index, Index):
        raise IndexFormatError(f"{path}: це не індекс findex")
    return index


# --- бінарний формат ------------------------------------------------------------------


def _as_arrays(plist: PostingList, with_positions: bool) -> ArrayPostings:
    if isinstance(plist, ArrayPostings):
        return plist
    out = ArrayPostings(with_positions)
    for p in plist:  # list[Posting] або list[PlainPosting]
        out.append(p.doc_id, p.tf, p.positions)
    return out


def save_binary(index: Index, path: Path) -> None:
    if _ITEMSIZE != 4:
        raise RuntimeError("array('I') на цій платформі не 4-байтовий")

    # Прохід 1: рахуємо зсуви, щоб записати заголовок ДО тіла.
    terms: dict[str, list[int]] = {}
    offset = 0
    for term, plist in index.items():
        arr = _as_arrays(plist, index.with_positions)
        n_pos = len(arr.positions) if arr.positions is not None else 0
        terms[term] = [offset, len(arr), n_pos]
        offset += (2 * len(arr) + n_pos) * _ITEMSIZE

    header = {
        "version": 3,
        "byteorder": sys.byteorder,
        "positions": index.with_positions,
        "layout": index.layout,
        "corpus_root": index.corpus_root,
        "doc_lengths": index.doc_lengths,
        "doc_meta": [[m.path, m.title] for m in index.doc_metas],
        "terms": terms,
    }
    header_bytes = json.dumps(header, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    # Прохід 2: пишемо файл
    with path.open("wb") as f:
        f.write(MAGIC)
        f.write(_LEN.pack(len(header_bytes)))
        f.write(header_bytes)
        for plist in index.values():
            arr = _as_arrays(plist, index.with_positions)
            arr.doc_ids.tofile(f)
            arr.tfs.tofile(f)
            if arr.positions is not None:
                arr.positions.tofile(f)


type Buffer = bytes | mmap.mmap | memoryview


def _read_header(buf: Buffer) -> tuple[dict[str, Any], int]:
    if bytes(buf[: len(MAGIC)]) != MAGIC:
        raise IndexFormatError("не файл findex (немає сигнатури FINDEX03)")
    (header_len,) = cast(tuple[int], _LEN.unpack_from(buf, len(MAGIC)))
    start = len(MAGIC) + _LEN.size
    header = cast(dict[str, Any], json.loads(bytes(buf[start : start + header_len])))
    return header, start + header_len


def _decode(
    buf: Buffer, body: int, entry: list[int], with_positions: bool, swap: bool
) -> ArrayPostings:
    offset, count, n_pos = entry
    pos = body + offset
    out = ArrayPostings(with_positions)
    step = count * _ITEMSIZE
    out.doc_ids.frombytes(buf[pos : pos + step])
    out.tfs.frombytes(buf[pos + step : pos + 2 * step])
    if out.positions is not None:
        start = pos + 2 * step
        out.positions.frombytes(buf[start : start + n_pos * _ITEMSIZE])
    if swap:  # файл записано на машині з іншим порядком байтів
        out.doc_ids.byteswap()
        out.tfs.byteswap()
        if out.positions is not None:
            out.positions.byteswap()
    return out


class LazyPostings(Mapping[str, PostingList]):
    """Словник «терм -> ArrayPostings», що читає постінги з mmap лише на вимогу."""

    def __init__(
        self,
        buf: mmap.mmap,
        body: int,
        terms: dict[str, list[int]],
        with_positions: bool,
        swap: bool,
    ) -> None:
        self._buf, self._body, self._terms = buf, body, terms
        self._with_positions, self._swap = with_positions, swap

    def __getitem__(self, term: str) -> ArrayPostings:
        entry = self._terms[term]  # KeyError, якщо терма немає — як у dict
        return _decode(self._buf, self._body, entry, self._with_positions, self._swap)

    def __contains__(self, term: object) -> bool:
        return term in self._terms

    def __iter__(self) -> Iterator[str]:
        return iter(self._terms)

    def __len__(self) -> int:
        return len(self._terms)


def load_binary(path: Path, *, lazy: bool = False) -> Index:
    with path.open("rb") as f:
        buf: bytes | mmap.mmap = (
            mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) if lazy else f.read()
        )

    header, body = _read_header(buf)
    with_positions = bool(header["positions"])
    swap = header["byteorder"] != sys.byteorder
    terms = cast(dict[str, list[int]], header["terms"])

    postings: Mapping[str, PostingList]
    if isinstance(buf, mmap.mmap):
        postings = LazyPostings(buf, body, terms, with_positions, swap)
        on_close = buf.close
    else:
        view = memoryview(buf)  # зрізи memoryview не копіюють байти
        postings = {t: _decode(view, body, e, with_positions, swap) for t, e in terms.items()}
        on_close = None

    metas = cast(list[list[str]], header["doc_meta"])
    return Index(
        postings,
        cast(list[int], header["doc_lengths"]),
        [DocMeta(path=p, title=t) for p, t in metas],
        layout="array",
        with_positions=with_positions,
        corpus_root=cast(str | None, header.get("corpus_root")),
        on_close=on_close,
    )
