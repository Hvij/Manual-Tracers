# Build context is the repo root (see docker-compose.yml), not RCA/ — pyproject.toml and
# uv.lock live at the root, and RCA/app imports as the `app` package relative to RCA/, so
# both have to be reachable in the same COPY tree.
FROM python:3.13-slim
WORKDIR /repo

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY RCA ./RCA

WORKDIR /repo/RCA
EXPOSE 8000
CMD ["/repo/.venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
