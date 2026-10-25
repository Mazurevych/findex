"""Завантажує корпус у data/: документація Python (CPython/Doc) + PEP.

Потрібен лише git. Запуск із кореня проєкту:

    uv run python scripts/get_corpus.py

Результат:
    data/python-docs/**.rst   ~560 файлів (довідник, туторіал, HOWTO, What's New)
    data/peps/pep-XXXX.rst    ~745 файлів (Python Enhancement Proposals)
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"


def git(*args: str, cwd: Path | None = None) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True)


def main() -> None:
    if (DATA / "python-docs").exists() or (DATA / "peps").exists():
        sys.exit(f"{DATA} вже містить корпус — видаліть його, щоб завантажити заново.")
    DATA.mkdir(exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        # 1) Лише каталог Doc/ з репозиторію CPython (sparse checkout, без історії)
        cpython = tmp_path / "cpython"
        git("clone", "--depth", "1", "--filter=blob:none", "--sparse",
            "https://github.com/python/cpython.git", str(cpython))  # fmt: skip
        git("sparse-checkout", "set", "Doc", cwd=cpython)
        dst = DATA / "python-docs"
        for src in (cpython / "Doc").rglob("*.rst"):
            target = dst / src.relative_to(cpython / "Doc")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, target)

        # 2) Усі PEP
        peps = tmp_path / "peps"
        git("clone", "--depth", "1", "https://github.com/python/peps.git", str(peps))
        (DATA / "peps").mkdir()
        for src in (peps / "peps").glob("pep-*.rst"):
            shutil.copyfile(src, DATA / "peps" / src.name)

    n = sum(1 for _ in DATA.rglob("*.rst"))
    size = sum(p.stat().st_size for p in DATA.rglob("*.rst"))
    print(f"Готово: {n} документів, {size / 1024 / 1024:.1f} MiB у {DATA}")


if __name__ == "__main__":
    main()
