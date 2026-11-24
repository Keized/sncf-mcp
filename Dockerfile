FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS dev
RUN mkdir -m 777 /cache
ENV UV_CACHE_DIR=/cache/uv UV_PROJECT_ENVIRONMENT=/cache/venv HOME=/cache
WORKDIR /app

FROM dev AS build
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN UV_PROJECT_ENVIRONMENT=/app/.venv uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim-bookworm AS runtime
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH HOST=0.0.0.0 PORT=8830
USER nobody
EXPOSE 8830
CMD ["sncf-mcp"]
