"""Лінивий потік документів корпусу.

`iter_documents(root)` — генератор: відкриває ОДИН файл, віддає (yield) документ,
переходить до наступного. Увесь корпус ніколи не лежить у пам'яті одночасно.

Підтримувані джерела:
  * каталог (рекурсивно, через Path.rglob) з файлами .txt / .rst / .md,
    у тому числі стиснутими .gz (розпаковуються «на льоту», без розпакування на диск);
  * файл JSON-lines (.jsonl або .jsonl.gz): один рядок = один документ
    з полями "id" (необов'язкове), "text" і "title" (необов'язкове) — так пише `findex crawl`.

Лаба 5: `corpus_paths(root)` дає шляхи в тому самому порядку, що й `iter_documents`, а
`documents_in(path, root)` — документи одного файлу. Так паралельна індексація ділить
роботу за ШЛЯХАМИ (дешево передати в інший процес), а не за прочитаними текстами.
"""

from __future__ import annotations

import gzip
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple, TextIO, cast

log = logging.getLogger(__name__)

TEXT_SUFFIXES = frozenset({".txt", ".rst", ".md"})


class Document(NamedTuple):
    doc_id: str  # стабільний ідентифікатор: відносний шлях (або id/URL з JSONL)
    path: Path  # файл, з якого прочитано документ
    text: str  # повний текст документа
    title: str | None = None  # готовий заголовок (JSONL з краулера); інакше — з тексту


def iter_documents(root: Path | str) -> Iterator[Document]:
    """Віддає документи корпусу по одному.

    Якщо `root` — каталог, обходить його рекурсивно (лінивий `rglob`).
    Якщо `root` — файл .jsonl / .jsonl.gz, кожен рядок стає документом.
    Файли з битим кодуванням не зупиняють обхід: пишемо попередження в лог
    і читаємо з errors="replace" (некоректні байти -> U+FFFD).
    """
    root = Path(root)
    if root.is_file() and not (_is_jsonl(root) or _is_text(root)):
        raise ValueError(f"Непідтримуваний тип файлу: {root}")
    for path in corpus_paths(root):
        yield from documents_in(path, root)


def corpus_paths(root: Path | str) -> Iterator[Path]:
    """Файли корпусу в порядку обходу (лінивий `rglob`). Файл-корпус — це він сам."""
    root = Path(root)
    if root.is_file():
        yield root
        return
    if not root.is_dir():
        raise FileNotFoundError(f"Корпус не знайдено: {root}")
    # rglob — генератор шляхів; файли відкриваються лише коли до них дійде черга.
    for path in root.rglob("*"):
        if path.is_file() and (_is_jsonl(path) or _is_text(path)):
            yield path


def documents_in(path: Path, root: Path | str) -> Iterator[Document]:
    """Документи одного файлу: текстовий файл — один документ, JSONL — по рядку."""
    root = Path(root)
    if _is_jsonl(path):
        yield from _iter_jsonl(path)
        return
    doc_id = path.name if root.is_file() else path.relative_to(root).as_posix()
    doc = _read_document(path, doc_id=doc_id)
    if doc is not None:
        yield doc


def count_in(path: Path) -> int:
    """Скільки документів дасть `documents_in(path)` — для текстового файлу 1, без читання."""
    if _is_jsonl(path):
        return sum(1 for _ in _iter_jsonl(path, quiet=True))
    return 1


def count_documents(root: Path | str) -> int:
    """Скільки документів віддасть iter_documents — без читання текстів (для прогрес-бару).

    Для каталогу рахує лише шляхи; JSON-lines — коректні рядки (потокове читання).
    """
    return sum(count_in(path) for path in corpus_paths(root))


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


def _open_text(path: Path, errors: str = "strict") -> TextIO:
    """Відкриває файл як текст UTF-8; .gz розпаковується потоково."""
    if path.suffix == ".gz":
        return cast(TextIO, gzip.open(path, mode="rt", encoding="utf-8", errors=errors))
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


def _iter_jsonl(path: Path, *, quiet: bool = False) -> Iterator[Document]:
    # Файл — це ітератор рядків: у пам'яті завжди лише поточний рядок.
    with _open_text(path, errors="replace") as f:
        for lineno, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                record = cast(dict[str, object], json.loads(line))
                text = record["text"]
                if not isinstance(text, str):
                    raise TypeError("поле text має бути рядком")
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                if not quiet:
                    log.warning("%s:%d: пропускаю некоректний рядок (%s)", path, lineno, exc)
                continue
            doc_id = str(record.get("id", f"{path.name}:{lineno}"))
            title = record.get("title")
            yield Document(
                doc_id=doc_id, path=path, text=text, title=title if isinstance(title, str) else None
            )
