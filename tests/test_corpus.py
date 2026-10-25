"""Перевірки потоку документів і статистики (лаба 1)."""

import gzip
import inspect
import json
from pathlib import Path

from findex.corpus import iter_documents
from findex.stats import compute_stats, main, run_eager, run_lazy


def make_corpus(root: Path) -> Path:
    (root / "sub").mkdir(parents=True)
    (root / "a.txt").write_text("Привіт світ", encoding="utf-8")
    (root / "sub" / "b.md").write_text("hello world hello", encoding="utf-8")
    (root / "c.txt").write_bytes(b"bad \xff\xfe bytes")  # не UTF-8
    (root / "skip.bin").write_bytes(b"\x00\x01")  # не текст — ігнорується
    with gzip.open(root / "d.txt.gz", "wt", encoding="utf-8") as f:
        f.write("стиснутий текст")
    return root


def test_iter_documents_is_generator(tmp_path):
    assert inspect.isgeneratorfunction(iter_documents)
    docs = iter_documents(make_corpus(tmp_path))
    first = next(docs)  # повертається одразу, решта ще не прочитана
    assert first.text


def test_iter_documents_reads_all_and_survives_bad_encoding(tmp_path, caplog):
    docs = {d.doc_id: d.text for d in iter_documents(make_corpus(tmp_path))}
    assert set(docs) == {"a.txt", "sub/b.md", "c.txt", "d.txt.gz"}
    assert "�" in docs["c.txt"]
    assert "не UTF-8" in caplog.text


def test_jsonl(tmp_path):
    p = tmp_path / "docs.jsonl"
    lines = [json.dumps({"id": 1, "text": "one"}), "not json", json.dumps({"text": "two"})]
    p.write_text("\n".join(lines), encoding="utf-8")
    assert [d.text for d in iter_documents(p)] == ["one", "two"]


def test_stats_single_pass_and_eager_agree(tmp_path):
    root = make_corpus(tmp_path)
    lazy, eager = run_lazy(root), run_eager(root)
    assert lazy.documents == eager.documents == 4
    assert lazy.counts == eager.counts
    assert lazy.counts["hello"] == 2
    assert lazy.tokens == sum(lazy.counts.values())


def test_limit_uses_islice(tmp_path):
    root = make_corpus(tmp_path)
    assert run_lazy(root, limit=2).documents == 2
    assert run_eager(root, limit=2).documents == 2


def test_compute_stats_accepts_any_iterable():
    assert compute_stats([]).documents == 0


def test_cli_prints_report(tmp_path, capsys):
    assert main([str(make_corpus(tmp_path)), "--top", "3"]) == 0
    out = capsys.readouterr().out
    assert "Документів:" in out and "Пікова пам'ять" in out and "hello" in out
