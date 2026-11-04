"""Вебсервіс findex (лаба 7): JSON API + HTML-сторінки поверх того самого `search()`.

    uvicorn findex.web.app:create_app --factory          # або: findex serve

Шлях запиту: uvicorn (сокет, HTTP) -> ASGI -> Starlette (маршрутизація, middleware) ->
FastAPI (розбирає й перевіряє параметри за анотаціями через Pydantic, викликає залежності
`Depends`) -> наша функція -> Pydantic-модель -> JSON.

* Індекс завантажується ОДИН раз у `lifespan` і живе в `app.state`; маршрути отримують
  його через `Depends(get_index)` — у тестах його підміняють `dependency_overrides`.
* Без `from __future__ import annotations`: FastAPI читає анотації параметрів під час
  виконання, а рядкові анотації не бачать локальних змінних `create_app` (напр. max_k).
* `/search` — `def` за замовчуванням: пошук — це CPU-робота (≈ мілісекунди), і в `def`
  FastAPI запускає її в пулі потоків, тож цикл подій лишається вільним для інших запитів.
  `SEARCH_HANDLER=async` вмикає `async def`-варіант для порівняння (scripts/bench_web.py).
"""

import contextvars
import logging
import math
import time
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from findex.index import Index
from findex.query import QueryError
from findex.scoring import get_scorer
from findex.search import SearchResult, matching_doc_ids, query_terms, search
from findex.snippets import Snippet, highlight_segments, read_document, snippet_for
from findex.store import IndexFormatError, load
from findex.web.models import (
    DocumentResponse,
    ErrorResponse,
    HealthResponse,
    SearchHit,
    SearchResponse,
    StatsResponse,
)
from findex.web.settings import Settings, get_settings

log = logging.getLogger("findex.web")
HERE = Path(__file__).parent
request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


def _version() -> str:
    try:
        return version("findex")
    except PackageNotFoundError:  # запуск із вихідного коду без встановлення
        return "dev"


VERSION = _version()

type ScorerParam = Literal["bm25", "tfidf"]


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id.get()
        return True


# --- залежності --------------------------------------------------------------------


def settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def maybe_index(request: Request) -> Index | None:
    """Індекс, завантажений у lifespan (None, якщо не вдалося). Тести підміняють саме це."""
    index: Index | None = getattr(request.app.state, "index", None)
    return index


def get_index(index: Annotated[Index | None, Depends(maybe_index)]) -> Index:
    if index is None:
        raise HTTPException(status_code=503, detail="індекс не завантажено")
    return index


IndexDep = Annotated[Index, Depends(get_index)]
SettingsDep = Annotated[Settings, Depends(settings_dep)]


# --- спільна логіка пошуку ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PageOfResults:
    hits: list[tuple[SearchResult, Snippet]]
    total: int
    pages: int
    took_ms: float


def search_page(index: Index, q: str, k: int, scorer: ScorerParam, page: int) -> PageOfResults:
    """Пошук із пагінацією: беремо top-(k·page) і відрізаємо потрібну сторінку.

    Кидає QueryError на синтаксичну помилку — маршрут перетворює її на 400.
    """
    started = time.perf_counter()
    ranked = search(index, q, scorer=get_scorer(scorer), k=k * page)
    total = len(matching_doc_ids(index, q))  # з кешу: search щойно це обчислив
    terms = query_terms(q)
    hits = [(r, snippet_for(index, r.doc_id, terms)) for r in ranked[(page - 1) * k : page * k]]
    took_ms = (time.perf_counter() - started) * 1000
    return PageOfResults(hits, total, max(1, math.ceil(total / k)), took_ms)


def run_search(index: Index, q: str, k: int, scorer: ScorerParam, page: int) -> SearchResponse:
    try:
        result = search_page(index, q, k, scorer, page)
    except QueryError as exc:
        raise HTTPException(status_code=400, detail=f"помилка в запиті: {exc}") from None
    return SearchResponse(
        query=q,
        scorer=scorer,
        total=result.total,
        page=page,
        k=k,
        pages=result.pages,
        took_ms=round(result.took_ms, 2),
        results=[
            SearchHit(
                doc_id=r.doc_id,
                title=r.title,
                path=r.path,
                score=round(r.score, 4),
                snippet=snip.render(),
            )  # fmt: skip
            for r, snip in result.hits
        ],
    )


# --- застосунок ----------------------------------------------------------------------


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        app.state.started = time.monotonic()
        app.state.index = None
        try:
            index = load(settings.index_path, lazy=settings.index_mmap)
            if settings.corpus_root is not None:
                index.corpus_root = str(settings.corpus_root)
            app.state.index = index
            log.info("index loaded: %r from %s", index, settings.index_path)
        except (OSError, IndexFormatError) as exc:
            # сервіс стартує, але /health віддає 503 — платформа не пустить на нього трафік
            log.error("index NOT loaded from %s: %s", settings.index_path, exc)
        try:
            yield
        finally:
            if app.state.index is not None:
                app.state.index.close()

    app = FastAPI(
        title="findex",
        version=VERSION,
        description="Навчальна пошукова система: BM25, Boolean-запити, фрази, сніпети.",
        lifespan=lifespan,
        responses={500: {"model": ErrorResponse}},
    )
    app.state.settings = settings
    app.state.index = None
    app.state.started = time.monotonic()
    templates = Jinja2Templates(directory=HERE / "templates")
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    # --- middleware: request id + журнал кожного запиту + чисті 500 ---

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        token = request_id.set(rid)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            log.exception(
                "unhandled error on %s %s [rid=%s]", request.method, request.url.path, rid
            )
            response = JSONResponse(
                status_code=500,
                content={"detail": "внутрішня помилка сервера", "request_id": rid},
            )
        finally:
            request_id.reset(token)
        response.headers["X-Request-ID"] = rid
        log.info(
            "%s %s -> %d %.1fms [rid=%s]",
            request.method,
            request.url.path,
            response.status_code,
            (time.perf_counter() - started) * 1000,
            rid,
        )
        return response

    # --- JSON API ---

    search_params = {
        "q": Query(min_length=1, max_length=500, description='запит: AND/OR/NOT, (), "фраза"'),
        "k": Query(ge=1, le=settings.max_k, description="результатів на сторінку"),
        "page": Query(ge=1, le=1000, description="номер сторінки"),
    }

    if settings.search_handler == "sync":

        @app.get("/search", response_model=SearchResponse, tags=["api"])
        def search_sync(  # pyright: ignore[reportUnusedFunction]  # реєструє декоратор
            index: IndexDep,
            q: Annotated[str, search_params["q"]],
            k: Annotated[int, search_params["k"]] = 10,
            scorer: ScorerParam = "bm25",
            page: Annotated[int, search_params["page"]] = 1,
        ) -> SearchResponse:
            """Пошук (`def`: виконується в пулі потоків, цикл подій вільний)."""
            return run_search(index, q, k, scorer, page)

    else:

        @app.get("/search", response_model=SearchResponse, tags=["api"])
        async def search_async(  # pyright: ignore[reportUnusedFunction]
            index: IndexDep,
            q: Annotated[str, search_params["q"]],
            k: Annotated[int, search_params["k"]] = 10,
            scorer: ScorerParam = "bm25",
            page: Annotated[int, search_params["page"]] = 1,
        ) -> SearchResponse:
            """Пошук (`async def`: CPU-робота прямо в циклі подій — для порівняння)."""
            return run_search(index, q, k, scorer, page)

    @app.get(
        "/docs/{doc_id}",
        response_model=DocumentResponse,
        responses={404: {"model": ErrorResponse}},
        tags=["api"],
    )
    def get_document(index: IndexDep, doc_id: Annotated[int, PathParam(ge=0)]) -> DocumentResponse:
        """Документ за doc_id (404, якщо такого немає)."""
        if doc_id >= index.num_docs:
            raise HTTPException(status_code=404, detail=f"документа {doc_id} немає")
        meta = index.meta(doc_id)
        return DocumentResponse(
            doc_id=doc_id,
            title=meta.title,
            path=meta.path,
            length=index.doc_length(doc_id),
            text=read_document(index, doc_id),
        )

    @app.get("/stats", response_model=StatsResponse, tags=["api"])
    def stats(request: Request, index: IndexDep, cfg: SettingsDep) -> StatsResponse:
        """Розмір індексу, кількість документів, словник, час роботи."""
        path = cfg.index_path
        return StatsResponse(
            documents=index.num_docs,
            terms=len(index),
            postings=index.num_postings(),
            tokens=index.num_tokens,
            avg_doc_length=round(index.avg_doc_length, 1),
            positions=index.with_positions,
            index_path=str(path),
            index_bytes=path.stat().st_size if path.exists() else 0,
            uptime_s=round(time.monotonic() - request.app.state.started, 1),
            version=VERSION,
            search_handler=cfg.search_handler,
        )

    @app.get(
        "/health",
        response_model=HealthResponse,
        responses={503: {"model": HealthResponse}},
        tags=["ops"],
    )
    def health(index: Annotated[Index | None, Depends(maybe_index)]) -> Response:
        """200, якщо індекс завантажено і відповідає; 503 — якщо ні (платформа не шле трафік)."""
        if index is None or index.closed:
            return JSONResponse(status_code=503, content={"status": "unavailable"})
        return JSONResponse({"status": "ok", "documents": index.num_docs})

    # --- HTML ---

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def home(
        request: Request,
        cfg: SettingsDep,
        index: Annotated[Index | None, Depends(maybe_index)],
        q: Annotated[str, Query(max_length=500)] = "",
        k: Annotated[int, Query(ge=1, le=settings.max_k)] = 10,
        scorer: ScorerParam = "bm25",
        page: Annotated[int, Query(ge=1, le=1000)] = 1,
    ) -> HTMLResponse:
        context: dict[str, object] = {
            "q": q, "k": k, "scorer": scorer, "page": page, "title": cfg.site_title,
            "result": None, "error": None, "index": index,
        }  # fmt: skip
        if q.strip() and index is not None:
            try:
                context["result"] = search_page(index, q, k, scorer, page)
            except QueryError as exc:
                context["error"] = str(exc)
        return templates.TemplateResponse(request, "index.html", context)

    @app.get("/doc/{doc_id}", response_class=HTMLResponse, include_in_schema=False)
    def doc_page(
        request: Request,
        index: IndexDep,
        cfg: SettingsDep,
        doc_id: Annotated[int, PathParam(ge=0)],
        q: Annotated[str, Query(max_length=500)] = "",
    ) -> HTMLResponse:
        if doc_id >= index.num_docs:
            raise HTTPException(status_code=404, detail=f"документа {doc_id} немає")
        text = read_document(index, doc_id)
        try:
            terms = query_terms(q) if q.strip() else frozenset[str]()
        except QueryError:
            terms = frozenset[str]()
        context = {
            "title": cfg.site_title, "q": q, "meta": index.meta(doc_id), "doc_id": doc_id,
            "length": index.doc_length(doc_id),
            "segments": highlight_segments(text, terms) if text is not None else None,
        }  # fmt: skip
        return templates.TemplateResponse(request, "doc.html", context)

    return app


def configure_logging(level: str = "info") -> None:
    """Логи сервісу в stderr із request id (видно в журналі платформи)."""
    handler = logging.StreamHandler()
    handler.addFilter(_RequestIdFilter())
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger("findex")
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    root.propagate = False
