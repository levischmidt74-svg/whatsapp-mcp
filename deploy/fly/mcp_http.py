"""Run the FastMCP server over streamable HTTP with OAuth 2.1 + PKCE in front.

Layout of the Starlette app served on 127.0.0.1:$MCP_PORT (Caddy fronts it):
    /.well-known/oauth-*        OAuth discovery (RFC 8414, 9728)
    /oauth/register             dynamic client registration
    /oauth/authorize            consent screen
    /oauth/authorize/consent    consent form POST
    /oauth/token                code -> access_token
    /mcp                        FastMCP streamable-http (auth-gated)

The bearer middleware accepts two kinds of tokens for /mcp:
    1. OAuth-issued access tokens stored in oauth_access_tokens
    2. The static MCP_BEARER_TOKEN (env), kept as an admin/curl backdoor

The MCP_BEARER_TOKEN doubles as the consent-screen shared secret. Anyone who
knows it can mint OAuth tokens, so treat it like a password. start.sh refuses
to launch without it.
"""

from __future__ import annotations

import os
import secrets

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from starlette.routing import Mount

from main import mcp
from oauth import init_db, validate_token
from oauth_routes import routes as oauth_routes

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
STATIC_TOKEN = os.getenv("MCP_BEARER_TOKEN", "")
PROTECTED_PREFIX = "/mcp"


def _www_authenticate() -> str:
    return (
        'Bearer realm="whatsapp-mcp", '
        f'resource_metadata="{PUBLIC_BASE_URL}/.well-known/oauth-protected-resource"'
    )


class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path
        if not (path == PROTECTED_PREFIX or path.startswith(PROTECTED_PREFIX + "/")):
            return await call_next(request)

        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            return Response(
                "Unauthorized",
                status_code=401,
                headers={"WWW-Authenticate": _www_authenticate()},
            )
        token = header[7:].strip()
        if STATIC_TOKEN and secrets.compare_digest(token, STATIC_TOKEN):
            return await call_next(request)
        if validate_token(token):
            return await call_next(request)
        return Response(
            "Unauthorized",
            status_code=401,
            headers={"WWW-Authenticate": _www_authenticate()},
        )


def build_app() -> Starlette:
    if not PUBLIC_BASE_URL:
        raise RuntimeError("PUBLIC_BASE_URL must be set")
    init_db()
    # FastMCP serves at /mcp by default; mount its Starlette app at root so
    # /mcp passes through unchanged. The inner app's lifespan starts the
    # StreamableHTTPSessionManager's task group — without propagating it,
    # /mcp requests crash with "Task group is not initialized".
    mcp_asgi = mcp.streamable_http_app()
    app = Starlette(
        routes=[
            *oauth_routes,
            Mount("/", app=mcp_asgi),
        ],
        middleware=[Middleware(BearerAuthMiddleware)],
        lifespan=mcp_asgi.router.lifespan_context,
    )
    return app


if __name__ == "__main__":
    port = int(os.getenv("MCP_PORT", "3000"))
    uvicorn.run(build_app(), host="127.0.0.1", port=port, log_level="info")
