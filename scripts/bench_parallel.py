"""Лаба 5, M3–M4: бенчмарк паралельної індексації + таблиця і графік у README.

Звичайний Python (з GIL) — serial, threads і processes на 1/2/4/8/всі ядра:
    uv run python scripts/bench_parallel.py run data/

Free-threaded Python 3.13t (без GIL) — ті самі threads, рядки ДОДАЮТЬСЯ до результатів:
    uv python install 3.13t
    uv run --isolated --python 3.13t python scripts/bench_parallel.py run data/ \
        --executors serial threads

Таблиця + графік (docs/lab05/speedup.png) у README між маркерами:
    uv run python scripts/bench_parallel.py render

Кожен прогін — окремий процес (scripts/_bench_once.py), щонайменше --runs разів;
у таблиці — медіана. Сирі дані — docs/lab05/results.json (їх і бере render).
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "docs" / "lab05" / "results.json"
PLOT = ROOT / "docs" / "lab05" / "speedup.png"
README = ROOT / "README.md"
START, END = "<!-- lab05-bench:start -->", "<!-- lab05-bench:end -->"

type Run = dict[str, Any]


def machine() -> dict[str, Any]:
    ram = None
    try:
        if sys.platform == "win32":
            import ctypes

            class MemStatus(ctypes.Structure):
                _fields_ = [  # noqa: RUF012
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = MemStatus()
            status.dwLength = ctypes.sizeof(MemStatus)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))  # type: ignore[attr-defined]
            ram = status.ullTotalPhys
        else:
            ram = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (OSError, ValueError, AttributeError):
        pass
    cpu = platform.processor() or platform.machine()
    if sys.platform.startswith("linux"):
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    return {
        "os": f"{platform.system()} {platform.release()}",
        "cpu": cpu,
        "cores": os.cpu_count(),
        "ram_gib": round(ram / 2**30, 1) if ram else None,
    }


def run(args: argparse.Namespace) -> None:
    cores = os.cpu_count() or 1
    worker_counts = sorted({1, 2, 4, 8, cores})
    configs: list[tuple[str, int]] = []
    for ex in args.executors:
        configs += [("serial", 1)] if ex == "serial" else [(ex, w) for w in worker_counts]

    data: dict[str, Any] = (
        json.loads(RESULTS.read_text("utf-8")) if RESULTS.exists() else {"runs": []}
    )
    data["machine"] = machine()
    for executor, workers in configs:
        for i in range(args.runs):
            out = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "_bench_once.py"),
                    args.root,
                    executor,
                    str(workers),
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            record: Run = json.loads(out.strip().splitlines()[-1])
            data["runs"].append(record)
            print(
                f"{record['python']:>8} {executor:>9} × {workers:<2} run {i + 1}: "
                f"wall {record['wall_s']:.2f} s, merge {record['merge_s']:.2f} s, "
                f"cpu {record['cpu_s']:.2f} s, rss {record['rss_bytes'] / 2**20:.0f} MiB",
                flush=True,
            )
            RESULTS.parent.mkdir(parents=True, exist_ok=True)
            RESULTS.write_text(json.dumps(data, indent=1), "utf-8")


def render(_: argparse.Namespace) -> None:
    data = json.loads(RESULTS.read_text("utf-8"))
    groups: defaultdict[tuple[str, str, int], list[Run]] = defaultdict(list)
    for r in data["runs"]:
        groups[(r["python"], r["executor"], r["workers"])].append(r)

    def med(rs: list[Run], key: str) -> float:
        return statistics.median(r[key] for r in rs)

    serial = {py: med(rs, "wall_s") for (py, ex, _), rs in groups.items() if ex == "serial"}
    order = {"serial": 0, "threads": 1, "processes": 2}
    rows: list[str] = []
    curves: defaultdict[str, list[tuple[int, float]]] = defaultdict(list)
    for (py, ex, w), rs in sorted(
        groups.items(), key=lambda kv: (kv[0][0].endswith("t"), order[kv[0][1]], kv[0][2])
    ):
        wall = med(rs, "wall_s")
        base = serial.get(py)
        speedup = base / wall if base else float("nan")
        label = ex + (" (3.13t, без GIL)" if py.endswith("t") else "")
        rows.append(
            f"| {label} | {w} | {wall:.2f} | {med(rs, 'cpu_s'):.2f} "
            f"| {med(rs, 'rss_bytes') / 2**20:.0f} MiB | {med(rs, 'merge_s'):.2f} "
            f"| {speedup:.2f}× | {len(rs)} |"
        )
        if ex != "serial":
            curves[label].append((w, speedup))

    m = data["machine"]
    pys = sorted({py for py, _, _ in groups})
    table = "\n".join(
        [
            f"Машина: {m['cpu']}, {m['cores']} логічних ядер, {m['ram_gib']} GiB RAM, {m['os']}; "
            f"Python {', '.join(pys)}. Корпус: {data['runs'][0]['docs']:,} документів.",
            "",
            "| Executor | Workers | Wall, с (медіана) | CPU, с | Пік RSS батька | Merge, с "
            "| Speedup | Прогонів |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
            *rows,
            "",
            "Speedup — відносно `serial` того самого інтерпретатора. "
            "CPU = процес-батько + звіти робітників-процесів.",
            "",
            "![Speedup vs workers](docs/lab05/speedup.png)",
        ]
    )
    text = README.read_text("utf-8")
    if START in text:
        before, rest = text.split(START, 1)
        after = rest.split(END, 1)[1]
        README.write_text(f"{before}{START}\n{table}\n{END}{after}", "utf-8")
        print("README оновлено")
    else:
        print(table)

    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib не встановлено — графік пропущено")
        return
    fig, ax = plt.subplots(figsize=(7, 4.5), dpi=120)
    xs = sorted({w for pts in curves.values() for w, _ in pts})
    ax.plot(xs, xs, linestyle="--", color="#999999", label="ідеальне лінійне")
    for label, pts in curves.items():
        pts.sort()
        ax.plot([w for w, _ in pts], [s for _, s in pts], marker="o", label=label)
    ax.axhline(1.0, color="#cccccc", linewidth=0.8)
    ax.axvline(m["cores"], color="#d62728", linestyle=":", linewidth=1,
               label=f"ядер на машині: {m['cores']}")  # fmt: skip
    ax.set_xlabel("Кількість робітників")
    ax.set_ylabel("Прискорення відносно serial")
    ax.set_title(f"findex index: {data['runs'][0]['docs']:,} документів, {m['cores']} ядер")
    ax.set_xticks(xs)
    ax.grid(alpha=0.3)
    ax.legend()
    fig.tight_layout()
    PLOT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(PLOT)
    print(f"графік: {PLOT}")


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("root")
    r.add_argument("--runs", type=int, default=3)
    r.add_argument(
        "--executors",
        nargs="+",
        default=["serial", "threads", "processes"],
        choices=["serial", "threads", "processes"],
    )
    r.add_argument("--fresh", action="store_true", help="почати results.json заново")
    sub.add_parser("render")
    args = p.parse_args()
    if args.cmd == "run":
        if args.fresh and RESULTS.exists():
            RESULTS.unlink()
        run(args)
    else:
        render(args)


if __name__ == "__main__":
    main()
