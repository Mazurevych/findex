"""Командний рядок findex (лаба 4): `findex index | search | stats`.

    findex index data/ --out index.bin --positions
    findex search index.bin '"context manager" NOT async' --k 5
    findex search index.bin "asyncio" --json | jq .title
    findex stats index.bin
    findex -v search ...      # INFO у stderr: час (@timed) і кеш запитів
    findex -vv search ...     # DEBUG

Результати йдуть у stdout, усі діагностичні повідомлення — через `logging` у stderr,
тож `--json` можна безпечно передавати в конвеєр. Очікувані помилки (немає файлу,
синтаксис запиту, чужий файл) — один зрозумілий рядок і код виходу 2.
Логування налаштовується ОДИН раз — тут, у callback кореневої команди.
"""

from __future__ import annotations

import json
import logging
import sys
import time
import tracemalloc
from collections import Counter
from enum import StrEnum
from itertools import islice
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table
from rich.text import Text

from findex.corpus import count_documents, iter_documents
from findex.index import Index, build_index, human_bytes
from findex.query import QueryError
from findex.scoring import get_scorer
from findex.search import query_terms, search
from findex.snippets import Snippet, snippet_for
from findex.store import IndexFormatError, open_index, save

log = logging.getLogger("findex")

app = typer.Typer(
    name="findex",
    help="findex — навчальна пошукова система (курс Python, КАІ).",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)
out = Console()  # stdout — результати
err = Console(stderr=True)  # stderr — діагностика, прогрес


class LayoutOpt(StrEnum):
    dataclass = "dataclass"
    slots = "slots"
    array = "array"


class FormatOpt(StrEnum):
    binary = "binary"
    pickle = "pickle"


class ScorerOpt(StrEnum):
    bm25 = "bm25"
    tfidf = "tfidf"


class EngineOpt(StrEnum):
    merge = "merge"
    set = "set"


def fail(message: str) -> typer.Exit:
    """Очікувана помилка: один рядок у stderr і код 2."""
    err.print(f"[bold red]Помилка:[/] {message}", highlight=False, soft_wrap=True)
    return typer.Exit(code=2)


@app.callback()
def main(
    verbose: Annotated[
        int, typer.Option("--verbose", "-v", count=True, help="-v: INFO, -vv: DEBUG (у stderr)")
    ] = 0,
) -> None:
    """findex — побудова індексу і пошук по корпусу."""
    level = logging.WARNING if verbose == 0 else logging.INFO if verbose == 1 else logging.DEBUG
    logging.basicConfig(
        level=level,
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
        force=True,  # повторні виклики (тести) переналаштовують, а не дублюють обробники
    )


# --- findex index ----------------------------------------------------------------


@app.command("index")
def index_cmd(
    corpus: Annotated[Path, typer.Argument(help="каталог корпусу або файл .jsonl[.gz]")],
    output: Annotated[Path, typer.Option("--out", "-o", help="файл індексу (.bin або .pkl)")],
    positions: Annotated[
        bool, typer.Option("--positions", help="зберігати позиції (для фраз)")
    ] = False,
    limit: Annotated[int | None, typer.Option(min=0, help="лише перші N документів")] = None,
    layout: Annotated[LayoutOpt, typer.Option(help="як зберігати постінги")] = LayoutOpt.slots,
    fmt: Annotated[
        FormatOpt | None,
        typer.Option("--format", help="формат файлу; за замовчуванням за розширенням"),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="вимірювання як JSON")] = False,
) -> None:
    """Будує інвертований індекс і зберігає його у файл."""
    if not corpus.exists():
        raise fail(f"корпус не знайдено: {corpus}")
    file_format = fmt.value if fmt else ("pickle" if output.suffix == ".pkl" else "binary")
    show_progress = not as_json and err.is_terminal

    total = count_documents(corpus)
    if limit is not None:
        total = min(total, limit)
    log.info("indexing %d documents from %s", total, corpus)

    tracemalloc.start()
    t0 = time.perf_counter()
    with Progress(
        TextColumn("[bold]Індексація"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=err,
        disable=not show_progress,
        transient=True,
    ) as bar:
        task = bar.add_task("index", total=total)
        index = build_index(
            islice(iter_documents(corpus), limit),
            layout=layout.value,
            positions=positions,
            corpus_root=corpus if corpus.is_dir() else corpus.parent,
            progress=lambda: bar.advance(task),
        )
    build_s = time.perf_counter() - t0
    index_bytes, build_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    output.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    save(index, output, fmt="pickle" if file_format == "pickle" else "binary")
    save_s = time.perf_counter() - t0
    size = output.stat().st_size

    result = {
        "layout": layout.value,
        "positions": positions,
        "format": file_format,
        "documents": index.num_docs,
        "terms": len(index),
        "postings": index.num_postings(),
        "build_s": build_s,
        "build_peak_bytes": build_peak,
        "index_bytes": index_bytes,
        "save_s": save_s,
        "file_bytes": size,
    }
    if as_json:
        typer.echo(json.dumps(result))
        return

    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Документів", f"{index.num_docs:,}")
    table.add_row("Термів", f"{len(index):,}")
    table.add_row("Постінгів", f"{result['postings']:,}")
    table.add_row("Постінги", layout.value + (" + позиції" if positions else ""))
    table.add_row("Побудова", f"{build_s:.2f} с, пік пам'яті {human_bytes(build_peak)}")
    table.add_row("Збереження", f"{save_s:.2f} с → {output} ({human_bytes(size)}, {file_format})")
    out.print(table)


# --- findex search ---------------------------------------------------------------


def _snippet_text(snippet: Snippet) -> Text:
    text = Text("…" if snippet.starts_mid else "", style="dim")
    for chunk, hit in snippet.segments:
        text.append(chunk, style="bold yellow" if hit else "")
    if snippet.ends_mid:
        text.append("…", style="dim")
    return text


@app.command("search")
def search_cmd(
    index_path: Annotated[Path, typer.Argument(metavar="INDEX", help="файл індексу")],
    queries: Annotated[
        list[str], typer.Argument(metavar="QUERY...", help="один або кілька запитів")
    ],
    k: Annotated[int, typer.Option("--k", "-k", min=1, help="скільки результатів")] = 10,
    scorer: Annotated[ScorerOpt, typer.Option(help="функція ранжування")] = ScorerOpt.bm25,
    engine: Annotated[EngineOpt, typer.Option(help="злиття списків")] = EngineOpt.merge,
    as_json: Annotated[bool, typer.Option("--json", help="JSON lines у stdout")] = False,
    use_mmap: Annotated[bool, typer.Option("--mmap", help="читати індекс через mmap")] = False,
    snippets: Annotated[bool, typer.Option(help="показувати сніпети")] = True,
) -> None:
    """Шукає за запитом (AND / OR / NOT, дужки, "фрази") і ранжує результати.

    Кілька запитів в одному виклику виконуються по черзі — повторний запит
    береться з кешу (видно з `findex -v search ...`).
    """
    try:
        with open_index(index_path, lazy=use_mmap) as ix:
            for query in queries:
                _run_query(ix, query, k, scorer, engine, as_json, snippets)
    except FileNotFoundError:
        raise fail(f"немає файлу індексу: {index_path}") from None
    except IndexFormatError as exc:
        raise fail(str(exc)) from None
    except QueryError as exc:
        raise fail(f"запит: {exc}") from None


def _run_query(
    ix: Index,
    query: str,
    k: int,
    scorer: ScorerOpt,
    engine: EngineOpt,
    as_json: bool,
    snippets: bool,
) -> None:
    results = search(ix, query, scorer=get_scorer(scorer.value), k=k, engine=engine.value)
    terms = query_terms(query)
    snips = [snippet_for(ix, r.doc_id, terms) if snippets else None for r in results]

    if as_json:
        for rank, (r, snip) in enumerate(zip(results, snips, strict=True), start=1):
            record = {
                "query": query,
                "rank": rank,
                "doc_id": r.doc_id,
                "score": round(r.score, 4),
                "title": r.title,
                "path": r.path,
                "snippet": snip.render() if snip else None,
            }
            typer.echo(json.dumps(record, ensure_ascii=False))
        return

    table = Table(title=f"«{query}» — {scorer.value}", title_justify="left", expand=True)
    table.add_column("#", justify="right", style="dim", no_wrap=True)
    table.add_column("Оцінка", justify="right", no_wrap=True)
    table.add_column("Документ", ratio=2)
    if snippets:
        table.add_column("Сніпет", ratio=3)
    for rank, (r, snip) in enumerate(zip(results, snips, strict=True), start=1):
        doc = Text(r.title, style="bold")
        doc.append(f"\n{r.path}", style="dim cyan")
        row: list[str | Text] = [str(rank), f"{r.score:.2f}", doc]
        if snip is not None:
            row.append(_snippet_text(snip))
        table.add_row(*row)
    if not results:
        table.caption = "нічого не знайдено"
    out.print(table)


# --- findex stats ----------------------------------------------------------------


@app.command("stats")
def stats_cmd(
    index_path: Annotated[Path, typer.Argument(metavar="INDEX", help="файл індексу")],
    top: Annotated[int, typer.Option(min=0, help="скільки найчастіших термів")] = 50,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Статистика лаби 1 з готового індексу: документи, токени, словник, топ термів."""
    try:
        with open_index(index_path) as ix:
            cf: Counter[str] = Counter()
            for term in ix:
                cf[term] = sum(p.tf for p in ix.iter_postings(term))
            data = {
                "documents": ix.num_docs,
                "tokens": ix.num_tokens,
                "vocabulary": len(ix),
                "postings": ix.num_postings(),
                "avg_doc_length": round(ix.avg_doc_length, 1),
                "positions": ix.with_positions,
                "top": cf.most_common(top),
            }
    except FileNotFoundError:
        raise fail(f"немає файлу індексу: {index_path}") from None
    except IndexFormatError as exc:
        raise fail(str(exc)) from None

    if as_json:
        typer.echo(json.dumps(data, ensure_ascii=False))
        return

    summary = Table(show_header=False, box=None, pad_edge=False)
    summary.add_column(style="bold")
    summary.add_column(justify="right")
    summary.add_row("Документів", f"{ix.num_docs:,}")
    summary.add_row("Токенів", f"{ix.num_tokens:,}")
    summary.add_row("Словник", f"{len(ix):,} термів")
    summary.add_row("Постінгів", f"{data['postings']:,}")
    summary.add_row("Середня довжина", f"{ix.avg_doc_length:,.1f} токенів")
    summary.add_row("Позиції", "так" if ix.with_positions else "ні")
    out.print(summary)
    if top:
        terms = Table(title=f"Топ-{top} термів", title_justify="left")
        terms.add_column("#", justify="right", style="dim")
        terms.add_column("Терм", style="bold")
        terms.add_column("Входжень", justify="right")
        terms.add_column("Документів", justify="right")
        for rank, (term, count) in enumerate(cf.most_common(top), start=1):
            terms.add_row(str(rank), term, f"{count:,}", f"{ix.df(term):,}")
        out.print(terms)


def run_subcommand(name: str) -> None:
    """`python -m findex.index ARGS` == `findex index ARGS` (сумісність з лабою 2)."""
    app(args=[name, *sys.argv[1:]], prog_name=f"python -m findex.{name}")
