"""Налаштування вебсервісу — лише зі змінних середовища (або файлу .env), не з коду.

Той самий Docker-образ працює і локально, і на Render: відрізняються лише змінні.

    INDEX_PATH=/app/index.bin       файл індексу (findex index ... --out)
    CORPUS_ROOT=/app/data           де лежать тексти для сніпетів (якщо не там, де будували)
    INDEX_MMAP=true                 читати постінги з диска на вимогу (менше RAM)
    SEARCH_HANDLER=sync             sync = `def` (пул потоків), async = `async def` (цикл подій)
    MAX_K=50                        скільки результатів на сторінку максимум
    LOG_LEVEL=info
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    index_path: Path = Path("index.bin")
    corpus_root: Path | None = None
    index_mmap: bool = True
    search_handler: Literal["sync", "async"] = "sync"
    max_k: int = Field(default=50, ge=1, le=500)
    log_level: Literal["debug", "info", "warning", "error"] = "info"
    site_title: str = "findex"


@lru_cache
def get_settings() -> Settings:
    return Settings()
