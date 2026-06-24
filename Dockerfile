FROM ghcr.io/astral-sh/uv:latest AS uv_bin

FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    libjpeg-dev \
    zlib1g-dev \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv_bin /uv /uvx /bin/

RUN useradd -m -u 8888 appuser && \
    mkdir -p /app/static /app/media && \
    chown -R appuser:appuser /app /home/appuser

COPY --chown=appuser:appuser pyproject.toml uv.lock ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY --chown=appuser:appuser . .

RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

USER appuser
EXPOSE 8000
