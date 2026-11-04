"""Лаба 4 M3: CLI через typer.testing.CliRunner — без запуску окремого процесу."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from findex.cli import app

runner = CliRunner()


@pytest.fixture(scope="module")
def built(corpus_dir: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("cli") / "index.bin"
    result = runner.invoke(app, ["index", str(corpus_dir), "--out", str(out), "--positions"])
    assert result.exit_code == 0, result.output
    return out


def test_index_command(built: Path) -> None:
    assert built.stat().st_size > 0


def test_index_json(corpus_dir: Path, tmp_path: Path) -> None:
    out = tmp_path / "ix.pkl"
    result = runner.invoke(
        app, ["index", str(corpus_dir), "-o", str(out), "--limit", "3", "--json"]
    )
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["documents"] == 3 and data["format"] == "pickle"


def test_search_table(built: Path) -> None:
    result = runner.invoke(app, ["search", str(built), '"context manager"', "-k", "3"])
    assert result.exit_code == 0, result.output
    assert "e-context.md" in result.stdout


def test_search_json_lines(built: Path) -> None:
    result = runner.invoke(
        app, ["search", str(built), "python", "--json", "--scorer", "tfidf", "-k", "2"]
    )
    assert result.exit_code == 0
    lines = [json.loads(line) for line in result.stdout.splitlines()]
    assert [r["rank"] for r in lines] == [1, 2]
    assert all("«python»" in r["snippet"].casefold() for r in lines)


def test_stats_command(built: Path) -> None:
    result = runner.invoke(app, ["stats", str(built), "--json", "--top", "3"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["documents"] == 6 and len(data["top"]) == 3


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["search", "missing.bin", "x"], "немає файлу індексу"),
        (["stats", "missing.bin"], "немає файлу індексу"),
        (["index", "no-such-dir", "--out", "x.bin"], "корпус не знайдено"),
    ],
)
def test_expected_errors_exit_2(args: list[str], message: str) -> None:
    result = runner.invoke(app, args)
    assert result.exit_code == 2
    assert message in result.stderr
    assert len(result.stderr.strip().splitlines()) == 1  # один зрозумілий рядок


def test_bad_query_exit_2(built: Path) -> None:
    result = runner.invoke(app, ["search", str(built), "(python OR"])
    assert result.exit_code == 2 and "запит" in result.stderr


def test_verbose_logs_go_to_stderr(built: Path) -> None:
    result = runner.invoke(app, ["-v", "search", str(built), "lazy", "lazy", "--json"])
    assert result.exit_code == 0
    assert "match cache: hits=1" in result.stderr  # повтор узято з кешу
    for line in result.stdout.splitlines():
        json.loads(line)  # stdout — лише чистий JSON
