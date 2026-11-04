"""Лаба 3 M4: precision@5 для TF-IDF і BM25 на 10 запитах з ручною розміткою.

    uv run findex index data/ --out index.bin --positions
    uv run python scripts/evaluate.py index.bin

Розмітка — eval/queries.json: для кожного запиту список документів, які «мають бути
в топ-5». Її зроблено ДО запуску ранжування, щоб не підганяти мітки під результат.
precision@5 = (скільки з перших 5 результатів релевантні) / 5.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import TypedDict, cast

from findex.scoring import BM25, Scorer, TfIdf
from findex.search import search
from findex.store import open_index

K = 5


class Judgement(TypedDict):
    query: str
    relevant: list[str]


def precision_at_k(found: list[str], relevant: set[str], k: int = K) -> float:
    return sum(1 for path in found[:k] if path in relevant) / k


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("index")
    p.add_argument("--labels", default=str(Path(__file__).parent.parent / "eval/queries.json"))
    p.add_argument("--details", action="store_true", help="показати топ-5 кожного запиту")
    args = p.parse_args()

    judgements = cast(list[Judgement], json.loads(Path(args.labels).read_text("utf-8")))
    scorers: dict[str, Scorer] = {"TF-IDF": TfIdf(), "BM25": BM25()}
    totals = dict.fromkeys(scorers, 0.0)

    print(f"| # | Запит | Релевантних | P@{K} TF-IDF | P@{K} BM25 |")
    print("|---:|---|---:|---:|---:|")
    with open_index(args.index) as ix:
        for i, j in enumerate(judgements, start=1):
            relevant = set(j["relevant"])
            row: list[str] = []
            for name, scorer in scorers.items():
                found = [r.path for r in search(ix, j["query"], scorer=scorer, k=K)]
                prec = precision_at_k(found, relevant)
                totals[name] += prec
                row.append(f"{prec:.1f}")
                if args.details:
                    marks = ["✓" if f in relevant else "·" for f in found]
                    print(
                        f"    {name:6} {j['query']}: "
                        + ", ".join(f"{m} {f}" for m, f in zip(marks, found, strict=True))
                    )
            query = j["query"].replace("|", "\\|")
            print(f"| {i} | `{query}` | {len(relevant)} | {row[0]} | {row[1]} |")
    n = len(judgements)
    print(
        f"| | **Середнє P@5** | | **{totals['TF-IDF'] / n:.2f}** | **{totals['BM25'] / n:.2f}** |"
    )


if __name__ == "__main__":
    main()
