"""Один прогін паралельної індексації в ОКРЕМОМУ процесі (для scripts/bench_parallel.py).

    python scripts/_bench_once.py data/ processes 4   ->  один рядок JSON

Окремий процес — щоб пік RSS і кеші одного прогону не впливали на інший.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from findex.parallel import ExecutorName, build_parallel
from findex.sysinfo import peak_rss_bytes


def main() -> None:
    root, executor, workers = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
    if executor not in ("serial", "threads", "processes"):
        raise SystemExit(f"невідомий executor: {executor}")
    ex: ExecutorName = executor  # type: ignore[assignment]  # перевірено рядком вище
    index, stats = build_parallel(root, executor=ex, workers=workers, layout="array")
    gil = getattr(sys, "_is_gil_enabled", lambda: True)()
    print(
        json.dumps(
            {
                "executor": executor,
                "workers": stats.workers,
                "chunks": stats.chunks,
                "wall_s": stats.wall_s,
                "plan_s": stats.plan_s,
                "map_s": stats.map_s,
                "merge_s": stats.merge_s,
                "cpu_s": stats.cpu_s,
                "rss_bytes": peak_rss_bytes(),
                "docs": index.num_docs,
                "terms": len(index),
                "gil": gil,
                "python": sys.version.split()[0] + ("t" if not gil else ""),
            }
        )
    )


if __name__ == "__main__":  # обов'язково: spawn імпортує цей файл у кожному робітнику
    main()
