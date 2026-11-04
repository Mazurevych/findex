"""Лаба 7: API і сторінки через TestClient; справжній файл індексу НЕ потрібен —
`dependency_overrides` підставляє крихітний індекс із фікстурного корпусу."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from findex.index import Index
from findex.web.app import create_app, maybe_index
from findex.web.settings import Settings


@pytest.fixture
def client(pindex: Index, tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(Settings(index_path=tmp_path / "nope.bin", max_k=20))
    app.dependency_overrides[maybe_index] = lambda: pindex
    with TestClient(app) as c:  # with -> виконується lifespan
        yield c


@pytest.fixture
def unloaded(tmp_path: Path) -> Iterator[TestClient]:
    """Сервіс, у якого індекс не завантажився (файлу немає) — без підміни."""
    with TestClient(create_app(Settings(index_path=tmp_path / "missing.bin"))) as c:
        yield c


def test_search_ok(client: TestClient) -> None:
    r = client.get("/search", params={"q": "python", "k": 2})
    assert r.status_code == 200
    body = r.json()
    assert body["query"] == "python" and body["total"] == 3 and body["pages"] == 2
    assert len(body["results"]) == 2 and body["results"][0]["score"] > 0
    assert "«python»" in body["results"][0]["snippet"].casefold()
    assert r.headers["x-request-id"]


def test_search_pagination(client: TestClient) -> None:
    page1 = client.get("/search", params={"q": "python", "k": 2, "page": 1}).json()
    page2 = client.get("/search", params={"q": "python", "k": 2, "page": 2}).json()
    ids1 = [h["doc_id"] for h in page1["results"]]
    ids2 = [h["doc_id"] for h in page2["results"]]
    assert len(ids2) == 1 and not set(ids1) & set(ids2)  # 3 результати: 2 + 1, без повторів


def test_search_phrase_and_scorer(client: TestClient) -> None:
    r = client.get("/search", params={"q": '"context manager"', "scorer": "tfidf"})
    assert r.status_code == 200 and r.json()["results"][0]["path"] == "e-context.md"


@pytest.mark.parametrize(
    "params",
    [
        {},  # q обов'язковий
        {"q": ""},
        {"q": "x", "k": 0},
        {"q": "x", "k": 21},  # більше за max_k
        {"q": "x", "page": 0},
        {"q": "x", "scorer": "pagerank"},
        {"q": "x", "k": "ten"},
    ],
)
def test_search_validation_422(client: TestClient, params: dict[str, object]) -> None:
    r = client.get("/search", params=params)
    assert r.status_code == 422 and "detail" in r.json()


def test_search_bad_query_400(client: TestClient) -> None:
    r = client.get("/search", params={"q": "(python OR"})
    assert r.status_code == 400 and "запит" in r.json()["detail"]


def test_document_and_404(client: TestClient) -> None:
    r = client.get("/docs/0")
    assert r.status_code == 200 and r.json()["text"] and r.json()["length"] > 0
    missing = client.get("/docs/999")
    assert missing.status_code == 404 and "999" in missing.json()["detail"]
    assert client.get("/docs/-1").status_code == 422


def test_stats(client: TestClient) -> None:
    body = client.get("/stats").json()
    assert body["documents"] == 6 and body["terms"] > 10 and body["uptime_s"] >= 0


def test_health_ok_and_503(client: TestClient, unloaded: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok", "documents": 6}
    r = unloaded.get("/health")
    assert r.status_code == 503 and r.json()["status"] == "unavailable"
    assert unloaded.get("/search", params={"q": "x"}).status_code == 503


def test_openapi_docs(client: TestClient) -> None:
    assert client.get("/docs").status_code == 200  # Swagger UI
    paths = client.get("/openapi.json").json()["paths"]
    assert {"/search", "/docs/{doc_id}", "/stats", "/health"} <= set(paths)


def test_unexpected_error_is_clean_500_with_request_id(
    pindex: Index, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    app = create_app(Settings(index_path=tmp_path / "nope.bin"))

    def broken() -> Index:
        raise RuntimeError("boom")

    app.dependency_overrides[maybe_index] = broken
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/stats", headers={"X-Request-ID": "abc123"})
    assert r.status_code == 500
    assert r.json() == {"detail": "внутрішня помилка сервера", "request_id": "abc123"}
    assert "boom" not in r.text  # деталі — у журнал, не користувачу
    assert "abc123" in caplog.text


def test_html_pages(client: TestClient) -> None:
    home = client.get("/")
    assert home.status_code == 200 and "Пошук у документації" in home.text
    results = client.get("/", params={"q": "lazy generators"})
    assert "<mark>" in results.text and "Знайдено" in results.text
    assert "Помилка в запиті" in client.get("/", params={"q": '"unclosed'}).text
    doc = client.get("/doc/0", params={"q": "python"})
    assert doc.status_code == 200 and "<pre" in doc.text
    assert client.get("/static/style.css").status_code == 200


def test_async_handler_variant(pindex: Index, tmp_path: Path) -> None:
    app = create_app(Settings(index_path=tmp_path / "x.bin", search_handler="async"))
    app.dependency_overrides[maybe_index] = lambda: pindex
    with TestClient(app) as c:
        assert c.get("/search", params={"q": "python"}).json()["total"] == 3
        assert c.get("/stats").json()["search_handler"] == "async"
