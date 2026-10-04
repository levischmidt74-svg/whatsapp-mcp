# syntax=docker/dockerfile:1.7
#
# Build: from the repo root,
#   flyctl deploy --config deploy/fly/fly.toml --dockerfile deploy/fly/Dockerfile .

# ---- bridge build ----
# Build on Debian (glibc), matches the python:3.12-slim runtime ABI.
# Alpine/musl produced a binary that fails with "cannot execute: required
# file not found" when copied into the slim image.
FROM golang:1.24-bookworm AS bridge-build
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libsqlite3-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY whatsapp-bridge/go.mod whatsapp-bridge/go.sum ./
RUN go mod download
COPY whatsapp-bridge/ ./
RUN CGO_ENABLED=1 go build -trimpath -ldflags="-s -w" -o /out/whatsapp-bridge .

# ---- caddy stage (just to grab the binary) ----
FROM caddy:2-alpine AS caddy-stage

# ---- runtime ----
FROM python:3.12-slim AS runtime
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_PREFERENCE=only-system \
    UV_PYTHON=/usr/local/bin/python3.12

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

COPY --from=caddy-stage /usr/bin/caddy /usr/local/bin/caddy
COPY --from=bridge-build /out/whatsapp-bridge /app/bridge/whatsapp-bridge

WORKDIR /app/mcp-server
COPY whatsapp-mcp-server/pyproject.toml whatsapp-mcp-server/uv.lock ./
RUN uv sync --frozen --no-dev
# python-multipart is needed by Starlette to parse the OAuth consent form
# POST (application/x-www-form-urlencoded actually doesn't strictly require
# it, but multipart fallback paths do — install once to be safe).
RUN uv pip install --no-cache python-multipart
# segno renders the pairing QR for the /qr page (pure python, no PIL).
RUN uv pip install --no-cache segno
COPY whatsapp-mcp-server/ ./

COPY deploy/fly/mcp_http.py     /app/mcp-server/mcp_http.py
COPY deploy/fly/oauth.py        /app/mcp-server/oauth.py
COPY deploy/fly/oauth_routes.py /app/mcp-server/oauth_routes.py
COPY deploy/fly/qr_page.py     /app/mcp-server/qr_page.py
COPY deploy/fly/Caddyfile       /app/Caddyfile
COPY deploy/fly/start.sh        /app/start.sh
RUN chmod +x /app/start.sh

ENV WHATSAPP_BRIDGE_PORT=8080 \
    WHATSAPP_API_URL=http://127.0.0.1:8080/api \
    WHATSAPP_DB_PATH=/data/store/messages.db \
    WHATSMEOW_DB_PATH=/data/store/whatsapp.db \
    OAUTH_DB_PATH=/data/store/oauth.db \
    MCP_PORT=3000 \
    PUBLIC_PORT=8000

EXPOSE 8000
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/app/start.sh"]
