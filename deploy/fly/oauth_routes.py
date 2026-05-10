"""Starlette routes implementing OAuth 2.1 + PKCE discovery and endpoints.

Endpoints (mirrors lawnsmith-hq):
    GET  /.well-known/oauth-authorization-server   RFC 8414 metadata
    GET  /.well-known/oauth-protected-resource     RFC 9728 metadata
    POST /oauth/register                           RFC 7591 dynamic client reg
    GET  /oauth/authorize                          consent screen (HTML form)
    POST /oauth/authorize/consent                  form submission, mints code
    POST /oauth/token                              code -> access_token
"""

from __future__ import annotations

import os
import time
from html import escape
from urllib.parse import urlencode

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from oauth import (
    create_authorization_code,
    exchange_code,
    get_client,
    register_client,
    verify_shared_secret,
)

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")


def _base() -> str:
    if not PUBLIC_BASE_URL:
        raise RuntimeError("PUBLIC_BASE_URL not set; cannot build OAuth URLs")
    return PUBLIC_BASE_URL


async def discovery(_: Request) -> JSONResponse:
    base = _base()
    return JSONResponse(
        {
            "issuer": base,
            "authorization_endpoint": f"{base}/oauth/authorize",
            "token_endpoint": f"{base}/oauth/token",
            "registration_endpoint": f"{base}/oauth/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"],
            "token_endpoint_auth_methods_supported": ["none"],
            "code_challenge_methods_supported": ["S256"],
            "scopes_supported": [],
        }
    )


async def protected_resource(_: Request) -> JSONResponse:
    base = _base()
    return JSONResponse(
        {
            "resource": f"{base}/mcp",
            "authorization_servers": [base],
            "bearer_methods_supported": ["header"],
        }
    )


async def register(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_request"}, status_code=400)
    redirect_uris = body.get("redirect_uris")
    if not isinstance(redirect_uris, list) or not redirect_uris:
        return JSONResponse({"error": "invalid_redirect_uri"}, status_code=400)
    info = register_client(redirect_uris, body.get("client_name"))
    return JSONResponse(
        {
            "client_id": info["client_id"],
            "client_secret": info["client_secret"],
            "client_id_issued_at": int(time.time()),
            "redirect_uris": info["redirect_uris"],
            "client_name": info["client_name"],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
        },
        status_code=201,
    )


def _consent_html(
    client_name: str,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    code_challenge_method: str,
    state: str,
    error: str | None = None,
) -> str:
    err_html = f'<div class="error">{escape(error)}</div>' if error else ""
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Authorize {escape(client_name)}</title>
<style>
body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 440px;
       margin: 80px auto; padding: 24px; background: #fafafa; color: #111; }}
h1 {{ font-size: 18px; margin: 0 0 8px; font-weight: 600; }}
.sub {{ color: #555; font-size: 14px; margin-bottom: 20px; }}
label {{ display: block; font-size: 13px; margin-bottom: 6px; color: #333; }}
input[type=password] {{ width: 100%; padding: 10px; border: 1px solid #ccc;
       border-radius: 6px; font-size: 14px; box-sizing: border-box; }}
button {{ background: #111; color: white; border: 0; padding: 10px 20px;
       border-radius: 6px; cursor: pointer; margin-top: 12px; font-size: 14px; }}
button:hover {{ background: #333; }}
.error {{ color: #b00; margin-top: 12px; font-size: 13px;
       padding: 8px 12px; background: #fee; border-radius: 4px; }}
.note {{ color: #666; font-size: 12px; margin-top: 20px;
       padding-top: 16px; border-top: 1px solid #eee; }}
</style></head>
<body>
<h1>Authorize {escape(client_name)}</h1>
<div class="sub">This will let it read and send your WhatsApp messages.</div>
<form method="post" action="/oauth/authorize/consent">
  <label for="s">MCP shared secret</label>
  <input id="s" type="password" name="shared_secret" autocomplete="off" autofocus required />
  <input type="hidden" name="client_id" value="{escape(client_id)}" />
  <input type="hidden" name="redirect_uri" value="{escape(redirect_uri)}" />
  <input type="hidden" name="code_challenge" value="{escape(code_challenge)}" />
  <input type="hidden" name="code_challenge_method" value="{escape(code_challenge_method)}" />
  <input type="hidden" name="state" value="{escape(state)}" />
  <button type="submit">Authorize</button>
  {err_html}
</form>
<div class="note">If you didn't initiate this from your Claude app, close this window.</div>
</body></html>"""


async def authorize_get(request: Request) -> Response:
    q = request.query_params
    response_type = q.get("response_type", "code")
    client_id = q.get("client_id", "")
    redirect_uri = q.get("redirect_uri", "")
    code_challenge = q.get("code_challenge", "")
    code_challenge_method = q.get("code_challenge_method", "S256")
    state = q.get("state", "")

    if response_type != "code":
        return HTMLResponse("response_type must be 'code'", status_code=400)
    if not (client_id and redirect_uri and code_challenge):
        return HTMLResponse("missing required parameters", status_code=400)
    if code_challenge_method != "S256":
        return HTMLResponse("only S256 PKCE supported", status_code=400)

    client = get_client(client_id)
    if not client:
        return HTMLResponse("unknown client_id", status_code=400)
    if redirect_uri not in client["redirect_uris"]:
        return HTMLResponse("redirect_uri not registered for this client", status_code=400)

    name = client["client_name"] or client_id
    return HTMLResponse(
        _consent_html(name, client_id, redirect_uri, code_challenge, code_challenge_method, state)
    )


async def consent(request: Request) -> Response:
    form = await request.form()
    client_id = form.get("client_id", "")
    redirect_uri = form.get("redirect_uri", "")
    code_challenge = form.get("code_challenge", "")
    code_challenge_method = form.get("code_challenge_method", "S256")
    state = form.get("state", "")

    client = get_client(client_id)
    if not client or redirect_uri not in client["redirect_uris"]:
        return HTMLResponse("invalid client/redirect_uri", status_code=400)

    if not verify_shared_secret(form.get("shared_secret", "")):
        # Re-render the consent page with an error
        return HTMLResponse(
            _consent_html(
                client["client_name"] or client_id,
                client_id,
                redirect_uri,
                code_challenge,
                code_challenge_method,
                state,
                error="Invalid shared secret. Try again.",
            ),
            status_code=403,
        )

    code = create_authorization_code(
        client_id=client_id,
        redirect_uri=redirect_uri,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
    )
    sep = "&" if "?" in redirect_uri else "?"
    location = f"{redirect_uri}{sep}{urlencode({'code': code, 'state': state})}"
    return RedirectResponse(location, status_code=303)


async def token(request: Request) -> JSONResponse:
    form = await request.form()
    if form.get("grant_type") != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
    code = form.get("code", "")
    code_verifier = form.get("code_verifier", "")
    redirect_uri = form.get("redirect_uri", "")
    client_id = form.get("client_id", "")
    if not (code and code_verifier and redirect_uri and client_id):
        return JSONResponse({"error": "invalid_request"}, status_code=400)
    result = exchange_code(
        code=code,
        code_verifier=code_verifier,
        redirect_uri=redirect_uri,
        client_id=client_id,
    )
    if not result:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    return JSONResponse(
        result,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


routes = [
    Route("/.well-known/oauth-authorization-server", discovery, methods=["GET"]),
    Route("/.well-known/oauth-protected-resource", protected_resource, methods=["GET"]),
    Route("/oauth/register", register, methods=["POST"]),
    Route("/oauth/authorize", authorize_get, methods=["GET"]),
    Route("/oauth/authorize/consent", consent, methods=["POST"]),
    Route("/oauth/token", token, methods=["POST"]),
]
