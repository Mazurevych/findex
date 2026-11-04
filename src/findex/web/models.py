"""Pydantic-моделі на МЕЖІ сервісу: що приходить і що йде назовні як JSON.

Усередині findex — звичайні dataclass з лаб 2–3 (`Posting`, `SearchResult`, `DocMeta`):
вони дешеві, і їх мільйони. Pydantic потрібен там, де дані приходять від чужих людей і
мають бути перевірені, а відповідь — описана в OpenAPI (`/docs`).
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SearchHit(BaseModel):
    doc_id: int = Field(description="ідентифікатор документа в індексі")
    title: str
    path: str = Field(description="шлях у корпусі або URL сторінки")
    score: float = Field(description="BM25 або TF-IDF")
    snippet: str = Field(description="уривок із «підсвіченими» термами")


class SearchResponse(BaseModel):
    query: str
    scorer: str
    total: int = Field(description="скільки документів відповідає запиту")
    page: int
    k: int
    pages: int
    took_ms: float
    results: list[SearchHit]


class DocumentResponse(BaseModel):
    doc_id: int
    title: str
    path: str
    length: int = Field(description="кількість токенів")
    text: str | None = Field(description="текст документа, якщо файл доступний")


class StatsResponse(BaseModel):
    documents: int
    terms: int
    postings: int
    tokens: int
    avg_doc_length: float
    positions: bool
    index_path: str
    index_bytes: int
    uptime_s: float
    version: str
    search_handler: str


class HealthResponse(BaseModel):
    status: str
    documents: int | None = None


class ErrorResponse(BaseModel):
    detail: str
    request_id: str | None = None
