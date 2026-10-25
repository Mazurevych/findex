"""Лінивий потік документів корпусу.

`iter_documents(root)` — генератор: відкриває ОДИН файл, віддає (yield) документ,
переходить до наступного. Увесь корпус ніколи не лежить у пам'яті одночасно.

Підтримувані джерела:
  * каталог (рекурсивно, через Path.rglob) з файлами .txt / .rst / .md,
    у тому числі стиснутими .gz (розпаковуються «на льоту», без розпакування на диск);
  * файл JSON-lines (.jsonl або .jsonl.gz): один рядок = один документ
    з полями "id" (необов'язкове) і "text".
"""

from __future__ import annotations

import gzip
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

log = logging.getLogger(__name__)

TEXT_SUFFIXES = frozenset({".txt", ".rst", ".md"})


class Document(NamedTuple):
    doc_id: str  # стабільний ідентифікатор: відносний шлях (або id з JSONL)
    path: Path  # файл, з якого прочитано документ
    text: str  # повний текст документа


def iter_documents(root: Path | str) -> Iterator[Document]:
    """Віддає документи корпусу по одному.

    Якщо `root` — каталог, обходить його рекурсивно (лінивий `rglob`).
    Якщо `root` — файл .jsonl / .jsonl.gz, кожен рядок стає документом.
    Файли з битим кодуванням не зупиняють обхід: пишемо попередження в лог
    і читаємо з errors="replace" (некоректні байти -> U+FFFD).
    """
    root = Path(root)
    if root.is_file():
        if _is_jsonl(root):
            yield from _iter_jsonl(root)
        elif _is_text(root):
            doc = _read_document(root, doc_id=root.name)
            if doc is not None:
                yield doc
        else:
            raise ValueError(f"Непідтримуваний тип файлу: {root}")
        return

    if not root.is_dir():
        raise FileNotFoundError(f"Корпус не знайдено: {root}")

    # rglob — генератор шляхів; файли відкриваються лише коли до них дійде черга.
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if _is_jsonl(path):
            yield from _iter_jsonl(path)
        elif _is_text(path):
            doc = _read_document(path, doc_id=path.relative_to(root).as_posix())
            if doc is not None:
                yield doc


# --- допоміжні функції -------------------------------------------------------


def _real_suffix(path: Path) -> str:
    """Розширення без .gz: 'a.txt.gz' -> '.txt'."""
    suffixes = path.suffixes
    if suffixes and suffixes[-1] == ".gz":
        suffixes = suffixes[:-1]
    return suffixes[-1].lower() if suffixes else ""


def _is_text(path: Path) -> bool:
    return _real_suffix(path) in TEXT_SUFFIXES


def _is_jsonl(path: Path) -> bool:
    return _real_suffix(path) == ".jsonl"


def _open_text(path: Path, errors: str = "strict"):
    """Відкриває файл як текст UTF-8; .gz розпаковується потоково."""
    if path.suffix == ".gz":
        return gzip.open(path, mode="rt", encoding="utf-8", errors=errors)
    return path.open(encoding="utf-8", errors=errors)


def _read_document(path: Path, doc_id: str) -> Document | None:
    try:
        with _open_text(path) as f:
            text = f.read()
    except UnicodeDecodeError as exc:
        log.warning("%s: не UTF-8 (%s), читаю з errors='replace'", path, exc.reason)
        with _open_text(path, errors="replace") as f:
            text = f.read()
    except OSError as exc:
        log.warning("%s: не вдалося прочитати (%s), пропускаю", path, exc)
        return None
    return Document(doc_id=doc_id, path=path, text=text)


def _iter_jsonl(path: Path) -> Iterator[Document]:
    # Файл — це ітератор рядків: у пам'яті завжди лише поточний рядок.
    with _open_text(path, errors="replace") as f:
        for lineno, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                text = record["text"]
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                log.warning("%s:%d: пропускаю некоректний рядок (%s)", path, lineno, exc)
                continue
            doc_id = str(record.get("id", f"{path.name}:{lineno}"))
            yield Document(doc_id=doc_id, path=path, text=text)
