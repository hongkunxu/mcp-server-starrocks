# syntax=docker/dockerfile:1.7

FROM python:3.11-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

RUN pip install --no-cache-dir uv

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN uv build --wheel --out-dir /dist


FROM python:3.11-slim AS runtime

ARG APP_VERSION=0.0.2

LABEL org.opencontainers.image.title="mcp-server-starrocks" \
      org.opencontainers.image.description="MCP server for one or more StarRocks clusters" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.licenses="Apache-2.0"

ENV HOME=/home/mcp \
    MCP_TRANSPORT_MODE=streamable-http \
    PATH="/usr/local/bin:${PATH}" \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install --no-install-recommends --yes ca-certificates \
    && groupadd --gid 65532 mcp \
    && useradd --uid 65532 --gid 65532 --create-home --shell /usr/sbin/nologin mcp \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /dist/*.whl /tmp/

RUN pip install --no-cache-dir /tmp/*.whl \
    && rm -f /tmp/*.whl

USER 65532:65532

EXPOSE 8000
STOPSIGNAL SIGTERM

ENTRYPOINT ["mcp-server-starrocks"]
CMD ["--mode", "streamable-http", "--host", "0.0.0.0", "--port", "8000"]
