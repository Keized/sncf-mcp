FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS dev
RUN mkdir -m 777 /cache
ENV UV_CACHE_DIR=/cache/uv UV_PROJECT_ENVIRONMENT=/cache/venv HOME=/cache
WORKDIR /app

FROM dev AS build
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN UV_PROJECT_ENVIRONMENT=/app/.venv uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim-bookworm AS runtime
LABEL io.modelcontextprotocol.server.name="io.github.Keized/sncf-mcp" \
      org.opencontainers.image.source="https://github.com/Keized/sncf-mcp" \
      org.opencontainers.image.licenses="Apache-2.0"
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH HOST=0.0.0.0 PORT=8830
USER nobody
EXPOSE 8830
CMD ["sncf-mcp"]
