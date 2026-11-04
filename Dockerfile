# findex — вебпошук (лаба 7). Двоетапна збірка:
#   build:   uv ставить залежності з uv.lock, качає корпус і будує індекс;
#   runtime: лише Python, .venv, корпус і index.bin — без uv, git і компіляторів.
#
#   docker build -t findex .
#   docker run --rm -p 8000:8000 findex               # http://localhost:8000
#   docker run --rm -p 8000:8000 -e SEARCH_HANDLER=async -e LOG_LEVEL=debug findex

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS build
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
WORKDIR /app

# 1) спершу лише залежності — цей шар кешується, поки не змінився uv.lock
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# 2) сам пакет (не editable — у runtime не буде вихідного коду)
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

# 3) корпус (документація Python + PEP з GitHub) і індекс з позиціями
COPY scripts/get_corpus.py ./scripts/get_corpus.py
RUN .venv/bin/python scripts/get_corpus.py \
 && .venv/bin/findex index data/ --out /app/index.bin --positions --executor processes


FROM python:3.13-slim-bookworm AS runtime
RUN useradd --create-home --uid 10001 findex
WORKDIR /app
COPY --from=build --chown=findex:findex /app/.venv /app/.venv
COPY --from=build --chown=findex:findex /app/data /app/data
COPY --from=build --chown=findex:findex /app/index.bin /app/index.bin
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    INDEX_PATH=/app/index.bin \
    INDEX_MMAP=true \
    LOG_LEVEL=info \
    PORT=8000
USER findex
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import os, urllib.request as u; u.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/health', timeout=4)"
# PORT задає платформа (Render — 10000); локально — 8000
CMD ["sh", "-c", "exec findex serve --host 0.0.0.0 --port ${PORT}"]
