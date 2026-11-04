"""Лаба 7, M4: навантажувальний тест `oha` — локально (3 конфігурації) і задеплоєний сервіс.

    uv run python scripts/loadtest.py local index.bin              # 1×def, 1×async, 4×def
    uv run python scripts/loadtest.py remote https://….onrender.com  # задеплоєний, concurrency 20

Потрібна програма oha (https://github.com/hatoo/oha): `winget install hatoo.oha`,
`brew install oha` або `cargo install oha`. Результати додаються в docs/lab07/loadtest.json,
а таблиця в README оновлюється між маркерами.
"""

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "docs" / "lab07" / "loadtest.json"
README = ROOT / "README.md"
START, END = "<!-- lab07-load:start -->", "<!-- lab07-load:end -->"
ROUTE = "/search?q=asyncio+OR+generator&k=10"


def oha_bin() -> str:
    found = shutil.which("oha") or os.environ.get("OHA")
    if not found:
        sys.exit("Не знайдено oha. Встановіть: winget install hatoo.oha (або cargo install oha)")
    return found


def oha(url: str, concurrency: int, seconds: int) -> dict[str, Any]:
    out = subprocess.run(
        [oha_bin(), "-z", f"{seconds}s", "-c", str(concurrency), "--no-tui", "--output-format",
         "json", url],
        check=True, capture_output=True, text=True,
    ).stdout  # fmt: skip
    data = json.loads(out)
    pct = data.get("latencyPercentiles", {})
    codes = data.get("statusCodeDistribution", {})
    return {
        "rps": data["summary"]["requestsPerSec"],
        "p50": pct["p50"] * 1000,
        "p95": pct["p95"] * 1000,
        "p99": pct["p99"] * 1000,
        "ok": int(codes.get("200", 0)),
        "total": sum(int(v) for v in codes.values()),
    }


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def serve(index: Path, handler: str, workers: int) -> tuple[subprocess.Popen[bytes], str]:
    port = free_port()
    env = {**os.environ, "INDEX_PATH": str(index), "LOG_LEVEL": "warning"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "findex", "serve", "--port", str(port), "--handler", handler,
         "--workers", str(workers)],
        env=env, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    base = f"http://127.0.0.1:{port}"
    for _ in range(150):
        try:
            if httpx.get(base + "/health", timeout=1).status_code == 200:
                break
        except httpx.TransportError:
            pass
        time.sleep(0.1)
    time.sleep(1.0 if workers > 1 else 0.2)  # дати всім робітникам завантажити індекс
    httpx.get(base + ROUTE, timeout=30)  # прогрів кешу запиту
    return proc, base


def save(rows: list[dict[str, Any]]) -> None:
    data: dict[str, Any] = json.loads(RESULTS.read_text("utf-8")) if RESULTS.exists() else {}
    for row in rows:
        data[row["target"]] = row
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(json.dumps(data, indent=1, ensure_ascii=False), "utf-8")
    render(data)


def render(data: dict[str, Any]) -> None:
    lines = [
        "| Target | Route | Concurrency | RPS | p50 (ms) | p95 | p99 | 200 OK |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    order = [
        "local, 1 worker, `async def`",
        "local, 1 worker, `def`",
        "local, 4 workers",
        "deployed",
    ]
    for key in sorted(data, key=lambda k: order.index(k) if k in order else 99):
        r = data[key]
        lines.append(
            f"| {key} | `/search` | {r['concurrency']} | {r['rps']:.1f} | {r['p50']:.0f} "
            f"| {r['p95']:.0f} | {r['p99']:.0f} | {r['ok']}/{r['total']} |"
        )
    table = "\n".join(lines)
    text = README.read_text("utf-8")
    if START in text:
        before, rest = text.split(START, 1)
        README.write_text(f"{before}{START}\n{table}\n{END}{rest.split(END, 1)[1]}", "utf-8")
        print("README оновлено")
    print(table)


def set_live_url(url: str) -> None:
    """Публічна адреса — на самому початку README (вимога лаби 7)."""
    text = README.read_text("utf-8")
    start, end = "<!-- live-url -->", "<!-- /live-url -->"
    if start in text:
        before, rest = text.split(start, 1)
        README.write_text(f"{before}{start}[{url}]({url}){end}{rest.split(end, 1)[1]}", "utf-8")
        print(f"README: живий сайт -> {url}")


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    loc = sub.add_parser("local")
    loc.add_argument("index", type=Path)
    loc.add_argument("--seconds", type=int, default=15)
    rem = sub.add_parser("remote")
    rem.add_argument("url")
    rem.add_argument("--seconds", type=int, default=30)
    args = p.parse_args()

    rows: list[dict[str, Any]] = []
    if args.cmd == "local":
        for label, handler, workers in (
            ("local, 1 worker, `async def`", "async", 1),
            ("local, 1 worker, `def`", "sync", 1),
            ("local, 4 workers", "sync", 4),
        ):
            proc, base = serve(args.index, handler, workers)
            try:
                row = {"target": label, "concurrency": 50, **oha(base + ROUTE, 50, args.seconds)}
            finally:
                proc.terminate()
                proc.wait()
            print(row, flush=True)
            rows.append(row)
    else:
        base = args.url.rstrip("/")
        httpx.get(base + "/health", timeout=120)  # розбудити (безкоштовний Render засинає)
        rows.append(
            {"target": "deployed", "concurrency": 20, **oha(base + ROUTE, 20, args.seconds)}
        )
        print(rows[-1])
    save(rows)


if __name__ == "__main__":
    main()
