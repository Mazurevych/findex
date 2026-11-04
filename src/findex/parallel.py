"""Паралельна індексація (лаба 5): ділимо шляхи на шматки -> часткові індекси -> злиття.

    paths ──chunk──> [шматок 0 | шматок 1 | ...]        doc_id-діапазони призначено НАПЕРЕД
                         │          │
                   build_partial  build_partial          serial / threads / processes
                         │          │
                         └──merge───┘ -> Index           однопотоково (закон Амдала!)

* `build_partial` — функція рівня МОДУЛЯ: `ProcessPoolExecutor` передає в інший процес
  не саму функцію, а її ім'я (`findex.parallel.build_partial`), тож лямбда чи вкладена
  функція там не спрацюють (pickle не знайде їх за іменем).
* Шматки ділимо за ШЛЯХАМИ: передати в інший процес список `Path` — копійки, а от
  прочитані тексти довелося б серіалізувати (pickle) і копіювати.
* doc_id кожного шматка — суцільний діапазон `[first_doc_id, first_doc_id + n)`, і діапазони
  йдуть по черзі. Тому злиття постінгів одного терма — просто конкатенація в порядку
  шматків: результат уже відсортований за doc_id, і індекс БАЙТ-У-БАЙТ такий самий, як у
  послідовної `build_index` (див. tests/test_parallel.py).
* Спільного змінного стану між потоками НЕМАЄ: кожен шматок має свої словники, а злиття
  робить один потік. Тому в коді індексації немає гонок навіть без GIL (Python 3.13t).
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import time
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import (
    Executor,
    Future,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    as_completed,
)
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from findex.corpus import corpus_paths, count_in, documents_in
from findex.index import (
    ArrayPostings,
    DocMeta,
    Index,
    Layout,
    PlainPosting,
    Posting,
    PostingList,
    analyze_document,
    document_meta,
)
from findex.timing import timed

log = logging.getLogger(__name__)

type ExecutorName = Literal["serial", "threads", "processes"]
EXECUTORS: tuple[ExecutorName, ...] = ("serial", "threads", "processes")


class CorpusChangedError(RuntimeError):
    """Файл зник або змінився між підрахунком документів і індексацією."""


@dataclass(frozen=True, slots=True)
class Chunk:
    """Шматок роботи: шляхи і перший doc_id (діапазон призначено наперед)."""

    paths: tuple[Path, ...]
    first_doc_id: int
    n_docs: int


@dataclass(slots=True)
class PartialIndex:
    """Компактний результат одного шматка: лише масиви, без об'єкта на постінг —
    щоб повернення з процесу (pickle) було дешевим."""

    first_doc_id: int
    postings: dict[str, ArrayPostings]
    doc_lengths: list[int]
    doc_meta: list[DocMeta]
    cpu_seconds: float = 0.0  # CPU-час цього шматка (time.thread_time у робітнику)


@dataclass(slots=True)
class BuildStats:
    executor: ExecutorName
    workers: int
    chunks: int
    wall_s: float = 0.0
    plan_s: float = 0.0  # пошук файлів і підрахунок документів (однопотоково)
    map_s: float = 0.0  # паралельна частина
    merge_s: float = 0.0  # злиття (однопотоково)
    cpu_s: float = 0.0  # батьківський процес + робітники-процеси
    worker_cpu: list[float] = field(default_factory=list[float])


# --- план ------------------------------------------------------------------------


def plan_chunks(root: Path | str, n_chunks: int) -> list[Chunk]:
    """Ділить файли корпусу на ~n_chunks шматків, приблизно рівних за БАЙТАМИ.

    Порядок файлів зберігається (той самий, що в iter_documents), тож doc_id збігаються
    з послідовною індексацією. Рівні за розміром шматки — щоб один «важкий» шматок не
    змушував решту робітників простоювати наприкінці.
    """
    paths = list(corpus_paths(root))
    if not paths:
        return []
    sizes = [p.stat().st_size for p in paths]
    target = max(1, sum(sizes) // max(1, n_chunks))
    chunks: list[Chunk] = []
    current: list[Path] = []
    current_size = 0
    first = 0
    count = 0
    for path, size in zip(paths, sizes, strict=True):
        current.append(path)
        current_size += size
        count += count_in(path)
        if current_size >= target and len(chunks) < n_chunks - 1:
            chunks.append(Chunk(tuple(current), first, count))
            first += count
            current, current_size, count = [], 0, 0
    if current:
        chunks.append(Chunk(tuple(current), first, count))
    return chunks


# --- робітник ----------------------------------------------------------------------


def build_partial(
    doc_paths: Sequence[Path],
    root: Path | str,
    first_doc_id: int = 0,
    positions: bool = False,
    expected_docs: int | None = None,
) -> PartialIndex:
    """Частковий індекс для списку шляхів. Рівень модуля -> pickle-сумісна для процесів."""
    started = time.thread_time()  # CPU саме цього потоку (process_time рахував би всі потоки)
    postings: dict[str, ArrayPostings] = {}
    doc_lengths: list[int] = []
    doc_meta: list[DocMeta] = []
    doc_id = first_doc_id
    for path in doc_paths:
        for doc in documents_in(path, root):
            n_tokens, items = analyze_document(doc.text, positions)
            doc_lengths.append(n_tokens)
            doc_meta.append(document_meta(doc))
            for term, tf, pos in items:
                plist = postings.get(term)
                if plist is None:
                    plist = postings[term] = ArrayPostings(positions)
                plist.append(doc_id, tf, pos)
            doc_id += 1
    if expected_docs is not None and len(doc_lengths) != expected_docs:
        raise CorpusChangedError(
            f"очікувалось {expected_docs} документів у шматку з doc_id {first_doc_id}, "
            f"прочитано {len(doc_lengths)} (файл зник або змінився під час індексації)"
        )
    return PartialIndex(first_doc_id, postings, doc_lengths, doc_meta, time.thread_time() - started)


def _build_chunk(chunk: Chunk, root: Path, positions: bool) -> PartialIndex:
    return build_partial(chunk.paths, root, chunk.first_doc_id, positions, chunk.n_docs)


# --- злиття -----------------------------------------------------------------------


@timed
def merge(
    partials: Iterable[PartialIndex],
    *,
    layout: Layout = "array",
    corpus_root: Path | str | None = None,
) -> Index:
    """Зливає часткові індекси. doc_id-діапазони не перетинаються і йдуть по черзі, тож
    постінги терма — це конкатенація в порядку first_doc_id (вже відсортована).

    Порядок термів у результаті = порядок першої появи по всьому корпусу — як у build_index.
    """
    ordered = sorted(partials, key=lambda p: p.first_doc_id)
    expected = 0
    for part in ordered:
        if part.first_doc_id != expected:
            raise ValueError(f"діапазони doc_id не суцільні: {expected} != {part.first_doc_id}")
        expected += len(part.doc_lengths)

    positions = any(pl.positions is not None for part in ordered for pl in part.postings.values())
    merged: dict[str, ArrayPostings] = {}
    for part in ordered:
        for term, plist in part.postings.items():
            target = merged.get(term)
            if target is None:
                merged[term] = target = ArrayPostings(positions)
            target.doc_ids.extend(plist.doc_ids)
            target.tfs.extend(plist.tfs)
            if target.positions is not None and plist.positions is not None:
                target.positions.extend(plist.positions)

    doc_lengths = [n for part in ordered for n in part.doc_lengths]
    doc_meta = [m for part in ordered for m in part.doc_meta]
    final: dict[str, PostingList]
    if layout == "array":
        final = dict(merged)
    else:
        record: type[Posting] | type[PlainPosting] = (
            PlainPosting if layout == "dataclass" else Posting
        )
        final = {
            term: [record(p.doc_id, p.tf, p.positions) for p in plist]
            for term, plist in merged.items()
        }
    return Index(
        final,
        doc_lengths,
        doc_meta,
        layout=layout,
        with_positions=positions,
        corpus_root=str(Path(corpus_root).resolve()) if corpus_root is not None else None,
    )


# --- запуск -------------------------------------------------------------------------


def _make_executor(executor: ExecutorName, workers: int) -> Executor | None:
    if executor == "serial":
        return None
    if executor == "threads":
        return ThreadPoolExecutor(max_workers=workers, thread_name_prefix="findex-index")
    # spawn — однаково на Linux, macOS і Windows: кожен робітник — чистий інтерпретатор,
    # що імпортує findex заново (без успадкованих через fork замків, потоків і пам'яті).
    return ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn"))


def default_workers() -> int:
    return os.cpu_count() or 1


@timed
def build_parallel(
    root: Path | str,
    *,
    executor: ExecutorName = "processes",
    workers: int | None = None,
    positions: bool = False,
    layout: Layout = "array",
    chunks_per_worker: int = 4,
    on_chunk_done: Callable[[int], None] | None = None,
) -> tuple[Index, BuildStats]:
    """Будує індекс корпусу паралельно. Помилка в будь-якому робітнику зупиняє збірку
    і піднімається тут з оригінальним traceback (для процесів — у `__cause__`)."""
    root = Path(root)
    workers = max(1, workers or default_workers())
    t_start = time.perf_counter()
    cpu_start = time.process_time()

    n_chunks = 1 if executor == "serial" else workers * chunks_per_worker
    chunks = plan_chunks(root, n_chunks)
    stats = BuildStats(executor, workers if executor != "serial" else 1, len(chunks))
    stats.plan_s = time.perf_counter() - t_start
    log.info("%s: %d chunks, %d workers", executor, len(chunks), stats.workers)

    t_map = time.perf_counter()
    partials: list[PartialIndex] = []
    pool = _make_executor(executor, workers)
    if pool is None:
        for chunk in chunks:
            partials.append(_build_chunk(chunk, root, positions))
            if on_chunk_done is not None:
                on_chunk_done(chunk.n_docs)
    else:
        with pool:
            futures: dict[Future[PartialIndex], Chunk] = {
                pool.submit(_build_chunk, chunk, root, positions): chunk for chunk in chunks
            }
            try:
                for future in as_completed(futures):
                    partials.append(future.result())  # виняток робітника -> піднімається тут
                    if on_chunk_done is not None:
                        on_chunk_done(futures[future].n_docs)
            except BaseException:
                # не чекаємо решту шматків: скасовуємо ті, що ще в черзі, і падаємо одразу
                pool.shutdown(wait=True, cancel_futures=True)
                raise
    stats.map_s = time.perf_counter() - t_map

    t_merge = time.perf_counter()
    index = merge(partials, layout=layout, corpus_root=root if root.is_dir() else root.parent)
    stats.merge_s = time.perf_counter() - t_merge

    stats.worker_cpu = [p.cpu_seconds for p in partials]
    stats.cpu_s = time.process_time() - cpu_start
    if executor == "processes":
        # process_time батька не бачить CPU дочірніх процесів — додаємо їхні звіти
        stats.cpu_s += sum(stats.worker_cpu)
    stats.wall_s = time.perf_counter() - t_start
    return index, stats
