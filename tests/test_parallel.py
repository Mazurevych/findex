"""Лаба 5: паралельна побудова дає БАЙТ-У-БАЙТ той самий індекс, що й послідовна."""

from __future__ import annotations

import hashlib
import pickle
from pathlib import Path

import pytest

from findex.corpus import corpus_paths, iter_documents
from findex.index import build_index
from findex.parallel import (
    EXECUTORS,
    Chunk,
    CorpusChangedError,
    ExecutorName,
    build_parallel,
    build_partial,
    merge,
    plan_chunks,
)
from findex.store import save


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def big_corpus(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """40 файлів із перетином словників, підкаталог і JSONL — щоб шматків було багато."""
    root = tmp_path_factory.mktemp("big")
    (root / "sub").mkdir()
    for i in range(40):
        words = " ".join(f"w{(i * 7 + j) % 53}" for j in range(30 + i))
        target = root / ("sub" if i % 3 else ".") / f"doc{i:02d}.txt"
        target.write_text(f"Документ {i}. {words} спільне слово", encoding="utf-8")
    lines = [
        f'{{"id": "j{i}", "title": "J {i}", "text": "jsonl текст {i} w{i}"}}' for i in range(5)
    ]
    (root / "extra.jsonl").write_text("\n".join(lines), encoding="utf-8")
    return root


@pytest.mark.parametrize("positions", [False, True])
def test_serial_merge_of_one_partial_equals_build_index(big_corpus: Path, positions: bool) -> None:
    ref = build_index(iter_documents(big_corpus), layout="array", positions=positions)
    one = merge([build_partial(list(corpus_paths(big_corpus)), big_corpus, 0, positions)])
    assert list(one) == list(ref)  # той самий порядок термів
    for term in ref:
        assert list(one.iter_postings(term)) == list(ref.iter_postings(term))
    assert one.doc_lengths == ref.doc_lengths and one.doc_metas == ref.doc_metas


@pytest.mark.slow
@pytest.mark.parametrize("executor", EXECUTORS)
def test_all_executors_produce_byte_identical_index(
    big_corpus: Path, tmp_path: Path, executor: ExecutorName
) -> None:
    ref = build_index(
        iter_documents(big_corpus), layout="array", positions=True, corpus_root=big_corpus
    )
    save(ref, tmp_path / "ref.bin")
    index, stats = build_parallel(big_corpus, executor=executor, workers=3, positions=True)
    save(index, tmp_path / "par.bin")
    assert digest(tmp_path / "par.bin") == digest(tmp_path / "ref.bin")
    if executor != "serial":
        assert stats.chunks > 1  # справді поділено на шматки


def test_threads_identical_quick(big_corpus: Path, tmp_path: Path) -> None:
    """Швидка версія для звичайного прогону (без процесів)."""
    ref = build_index(iter_documents(big_corpus), layout="array", corpus_root=big_corpus)
    save(ref, tmp_path / "ref.bin")
    index, _ = build_parallel(big_corpus, executor="threads", workers=4)
    save(index, tmp_path / "par.bin")
    assert digest(tmp_path / "par.bin") == digest(tmp_path / "ref.bin")


def test_plan_chunks_assigns_contiguous_ranges(big_corpus: Path) -> None:
    chunks = plan_chunks(big_corpus, 6)
    assert 1 < len(chunks) <= 6
    first = 0
    for chunk in chunks:
        assert chunk.first_doc_id == first
        first += chunk.n_docs
    assert first == 45  # 40 файлів + 5 рядків JSONL


def test_partial_index_is_picklable(big_corpus: Path) -> None:
    part = build_partial(list(corpus_paths(big_corpus))[:3], big_corpus, 10)
    back = pickle.loads(pickle.dumps(part))
    assert back.first_doc_id == 10 and back.doc_lengths == part.doc_lengths


def test_merge_rejects_gaps(big_corpus: Path) -> None:
    paths = list(corpus_paths(big_corpus))
    a = build_partial(paths[:2], big_corpus, 0)
    b = build_partial(paths[2:4], big_corpus, 5)  # має бути 2
    with pytest.raises(ValueError, match="doc_id"):
        merge([a, b])


@pytest.mark.parametrize("executor", ["threads", "processes"])
def test_worker_error_fails_loudly(
    tmp_path: Path, executor: ExecutorName, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Збій у робітнику -> виняток із причиною в батьківському процесі, а не зависання."""
    root = tmp_path / "c"
    root.mkdir()
    for i in range(6):
        (root / f"{i}.txt").write_text(f"text {i}", encoding="utf-8")
    real_plan = plan_chunks

    def plan_with_missing_file(r: Path | str, n: int) -> list[Chunk]:
        chunks = real_plan(r, n)
        (root / "3.txt").unlink()  # файл зник між плануванням і індексацією
        return chunks

    monkeypatch.setattr("findex.parallel.plan_chunks", plan_with_missing_file)
    with pytest.raises(CorpusChangedError, match="очікувалось 1"):
        build_parallel(root, executor=executor, workers=2, chunks_per_worker=3)
