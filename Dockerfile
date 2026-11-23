# syntax=docker/dockerfile:1.7

# --- build: resolve and install the dependencies into a virtualenv ----------------------------------------------
# Every dependency ships a prebuilt wheel for linux/x86_64 (rosu-pp-py included), so no compiler or Rust toolchain
# is needed. Only the lock/pyproject files are copied here, so this layer is rebuilt only when dependencies change.
FROM python:3.12-slim AS build
RUN pip install --no-cache-dir uv
WORKDIR /app
COPY pyproject.toml uv.lock* ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev --no-install-project --no-editable

# --- runtime: the virtualenv and the source, nothing else -------------------------------------------------------
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY alembic.ini init_db.py script_start.sh ./
COPY alembic ./alembic
COPY app ./app

EXPOSE 8000
ENTRYPOINT ["bash", "script_start.sh"]
