"""Ранжування: TF-IDF і BM25 за спільним протоколом `Scorer`.

`Scorer` — це `typing.Protocol`: будь-який об'єкт із методом
`score(term, posting, index) -> float` підходить, БЕЗ наслідування.
pyright перевіряє відповідність структурно — за сигнатурою методу.

    N   — кількість документів у корпусі
    df  — у скількох документах є терм
    tf  — скільки разів терм трапився в цьому документі
    dl  — довжина документа (токенів), avgdl — середня довжина

TF-IDF:  tf · log(N / df)
BM25:    idf · tf·(k1 + 1) / (tf + k1·(1 − b + b·dl/avgdl)),
         idf = log(1 + (N − df + 0.5) / (df + 0.5))

Дві відмінності BM25 від TF-IDF:
  * насичення tf: k1 обмежує внесок частоти — 20-те входження майже нічого не додає
    (при tf → ∞ дріб прямує до k1 + 1);
  * нормалізація довжини: b вирішує, наскільки довгий документ «штрафується».
    b = 0 — довжина ігнорується; b = 1 — повна нормалізація на avgdl.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, Protocol

from findex.index import Index, PostingLike

type ScorerName = Literal["bm25", "tfidf"]


class Scorer(Protocol):
    """Внесок одного терма запиту в оцінку одного документа."""

    def score(self, term: str, posting: PostingLike, index: Index) -> float: ...


@dataclass(frozen=True, slots=True)
class TfIdf:
    """Класичний TF-IDF: частота в документі × рідкісність по корпусу."""

    def score(self, term: str, posting: PostingLike, index: Index) -> float:
        df = index.df(term)
        if df == 0:
            return 0.0
        return posting.tf * math.log(index.num_docs / df)

    def __call__(self, term: str, posting: PostingLike, index: Index) -> float:
        return self.score(term, posting, index)


@dataclass(frozen=True, slots=True)
class BM25:
    """Okapi BM25 (як у Lucene/Elasticsearch). k1 — насичення tf, b — нормалізація довжини."""

    k1: float = 1.5
    b: float = 0.75

    def idf(self, term: str, index: Index) -> float:
        df = index.df(term)
        n = index.num_docs
        return math.log(1 + (n - df + 0.5) / (df + 0.5))

    def tf_part(self, tf: int, doc_len: int, avgdl: float) -> float:
        norm = 1 - self.b + self.b * (doc_len / avgdl if avgdl else 0.0)
        return tf * (self.k1 + 1) / (tf + self.k1 * norm)

    def score(self, term: str, posting: PostingLike, index: Index) -> float:
        dl = index.doc_length(posting.doc_id)
        return self.idf(term, index) * self.tf_part(posting.tf, dl, index.avg_doc_length)

    def __call__(self, term: str, posting: PostingLike, index: Index) -> float:
        return self.score(term, posting, index)


def get_scorer(name: ScorerName) -> Scorer:
    return BM25() if name == "bm25" else TfIdf()
