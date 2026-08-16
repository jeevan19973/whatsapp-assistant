# Python 3.14 (the Huckleberry client requires it) on a slim base, with uv for installs.
FROM python:3.14-slim

# uv binary from the official image — no pip bootstrap needed.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Install dependencies first (cached layer). --no-install-project: this is an app, not a
# packaged library, so we install its deps into .venv and run it via PYTHONPATH instead.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app \
    DB_PATH=/data/journal.db

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
