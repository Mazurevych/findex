"""Лаба 3 M2: три sanity-перевірки ранжування на справжньому корпусі.

    uv run python scripts/sanity_checks.py index.bin

1. Рідкісний терм запиту важить більше за частий (idf).
2. BM25 насичує tf: 20-те входження терма майже нічого не додає.
3. Короткий документ з одним збігом вищий за дуже довгий з одним збігом (b).
"""

from __future__ import annotations

import argparse

from findex.index import Index, Posting
from findex.scoring import BM25, TfIdf
from findex.search import search
from findex.store import open_index


def rare_vs_common(ix: Index) -> None:
    common, rare = "python", "cdecimal"
    print(f"1) Запит `{common} OR {rare}`:", end=" ")
    print(f"df({common}) = {ix.df(common)}, df({rare}) = {ix.df(rare)}")
    for name, scorer in (("TF-IDF", TfIdf()), ("BM25", BM25())):
        top = search(ix, f"{common} OR {rare}", scorer=scorer, k=1)[0]
        has_rare = top.doc_id in set(ix.doc_ids(rare))
        print(f"   {name}: №1 — {top.path} ({top.title}); містить `{rare}`: {has_rare}")
    bm25 = BM25()
    print(f"   idf BM25: {common} = {bm25.idf(common, ix):.3f}, {rare} = {bm25.idf(rare, ix):.3f}")


def saturation(ix: Index) -> None:
    bm25, tfidf = BM25(), TfIdf()
    term = "generator"
    doc = max(ix.iter_postings(term), key=lambda p: p.tf)
    dl = ix.doc_length(doc.doc_id)
    print(f"2) Терм `{term}` у {ix.meta(doc.doc_id).path} (tf = {doc.tf}, довжина {dl}):")
    print("   tf | BM25 score | приріст | TF-IDF score | приріст")
    prev_b = prev_t = 0.0
    for tf in (1, 2, 5, 10, 19, 20):
        p = Posting(doc.doc_id, tf)
        b, t = bm25.score(term, p, ix), tfidf.score(term, p, ix)
        print(f"   {tf:>2} | {b:10.3f} | {b - prev_b:+7.3f} | {t:12.3f} | {t - prev_t:+7.3f}")
        prev_b, prev_t = b, t
    first = bm25.score(term, Posting(doc.doc_id, 1), ix)
    at = lambda tf: bm25.score(term, Posting(doc.doc_id, tf), ix)  # noqa: E731
    gain20 = at(20) - at(19)
    print(f"   20-те входження в BM25 дає {gain20 / first:.1%} від внеску першого")


def short_vs_long(ix: Index) -> None:
    term = "walrus"
    once = [p for p in ix.iter_postings(term) if p.tf == 1]
    short = min(once, key=lambda p: ix.doc_length(p.doc_id))
    long = max(once, key=lambda p: ix.doc_length(p.doc_id))
    print(f"3) Терм `{term}` один раз (tf = 1), avgdl = {ix.avg_doc_length:,.0f}:")
    for label, p in (("короткий", short), ("довгий", long)):
        dl = ix.doc_length(p.doc_id)
        print(
            f"   {label:8} {ix.meta(p.doc_id).path:45} довжина {dl:>7,}: "
            f"BM25 {BM25().score(term, p, ix):.3f}, BM25(b=0) {BM25(b=0).score(term, p, ix):.3f}, "
            f"TF-IDF {TfIdf().score(term, p, ix):.3f}"
        )


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("index")
    args = p.parse_args()
    with open_index(args.index) as ix:
        rare_vs_common(ix)
        print()
        saturation(ix)
        print()
        short_vs_long(ix)


if __name__ == "__main__":
    main()
