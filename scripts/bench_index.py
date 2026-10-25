"""Лаба 2, M3 + M4: пам'ять побудови, розмір файлу, час save/load.

Кожна побудова і кожне завантаження — окремий процес (чисті вимірювання).
Час завантаження — медіана з --repeat запусків без tracemalloc.

    uv run python scripts/bench_index.py data/

Друкує дві Markdown-таблиці для README.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

# (підпис, layout, позиції, формат, mmap при завантаженні)
CONFIGS = [
    ("`list[Posting]`, звичайний `@dataclass`", "dataclass", False, "pickle", False),
    ("`list[Posting]`, `slots=True`", "slots", False, "pickle", False),
    ("`array('I')` пари (doc_ids, tfs)", "array", False, "pickle", False),
    ("`array('I')` пари, бінарний формат", "array", False, "binary", False),
    ("`array('I')` пари, бінарний формат + mmap", "array", False, "binary", True),
    ("`slots=True` + позиції", "slots", True, "pickle", False),
    ("`array('I')` + позиції, бінарний формат", "array", True, "binary", False),
]

LOAD_SNIPPET = """
import sys, time, json
from findex.store import load
lazy = sys.argv[2] == "1"
t0 = time.perf_counter(); ix = load(sys.argv[1], lazy=lazy); t = time.perf_counter() - t0
print(json.dumps({"load_s": t}))
"""


def mib(n: float) -> str:
    return f"{n / 1024 / 1024:.1f} MiB"


def ms(s: float) -> str:
    return f"{s * 1000:.0f} мс"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("root")
    p.add_argument("--repeat", type=int, default=3)
    args = p.parse_args()

    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        built: dict[tuple, dict] = {}
        for label, layout, positions, fmt, lazy in CONFIGS:
            key = (layout, positions, fmt)
            out = Path(tmp) / f"{layout}-{int(positions)}.{'pkl' if fmt == 'pickle' else 'bin'}"
            if key not in built:
                cmd = [sys.executable, "-m", "findex.index", args.root, "--out", str(out)]
                cmd += ["--layout", layout, "--format", fmt, "--json"]
                if positions:
                    cmd.append("--positions")
                res = subprocess.run(cmd, check=True, capture_output=True, text=True)
                built[key] = json.loads(res.stdout)
                print(f"  built {label}", file=sys.stderr, flush=True)
            b = built[key]
            loads = [
                json.loads(
                    subprocess.run(
                        [sys.executable, "-c", LOAD_SNIPPET, str(out), "1" if lazy else "0"],
                        check=True,
                        capture_output=True,
                        text=True,
                    ).stdout
                )
                for _ in range(args.repeat)
            ]
            rows.append(
                (
                    label,
                    fmt + (" (mmap)" if lazy else ""),
                    b,
                    statistics.median(x["load_s"] for x in loads),
                )
            )

    print("\n### Пам'ять і формати\n")
    print("| Постінги | Формат | Пік пам'яті (побудова) | Індекс у пам'яті | Файл | Save | Load |")
    print("|---|---|---:|---:|---:|---:|---:|")
    for label, fmt, b, load_s in rows:
        print(
            f"| {label} | {fmt} | {mib(b['build_peak_bytes'])} | {mib(b['index_bytes'])} "
            f"| {mib(b['file_bytes'])} | {ms(b['save_s'])} | {ms(load_s)} |"
        )
    b = rows[0][2]
    print(
        f"\nКорпус: {b['documents']:,} документів, {b['terms']:,} термів, "
        f"{b['postings']:,} постінгів."
    )


if __name__ == "__main__":
    main()
