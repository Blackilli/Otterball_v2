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
    gosu \
    curl \
    procps \
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

# `uv run` re-syncs the project against pyproject.toml before running, and its
# default group set includes dev - so every container start re-downloaded
# basedpyright, pyrefly, black and a node toolchain into the image's --no-dev
# venv, four containers at once, into the same /app/.venv. The image is already
# synced at build time; nothing at runtime should be resolving dependencies.
ENV UV_NO_SYNC=1

# The container starts as root only so docker-entrypoint.sh can remap appuser to
# PUID/PGID; it drops to that user via gosu before running anything.
ENV PUID=8888 \
    PGID=8888 \
    WEB_PORT=8000

COPY --chmod=755 docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]

EXPOSE ${WEB_PORT}

# Default command: the web service. The other three services (bot, worker, beat)
# override it with their own; see compose.yml.
CMD ["sh", "-c", "uv run python manage.py migrate && uv run python manage.py ensure_schedule && uv run python manage.py collectstatic --noinput && uv run gunicorn otterball_v2.wsgi:application --bind 0.0.0.0:${WEB_PORT:-8000}"]
