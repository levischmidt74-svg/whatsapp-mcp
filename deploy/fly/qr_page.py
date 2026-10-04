"""Click-a-link WhatsApp pairing page for the remote bridge.

Why a file handoff instead of asking the bridge: main.go only calls
startRESTServer() *after* pairing succeeds, so nothing is listening on the
bridge's port while a QR code is on screen. The bridge therefore writes each
code it emits to <store>/pair_qr.txt (see writePairQR in main.go) and this
module re-encodes it as an SVG QR.

Routes, all gated by the admin bearer, accepted either as an Authorization
header or as ?token=... so the URL is clickable from a phone:

    GET  /qr            self-refreshing HTML page
    GET  /qr.svg        current code as an SVG QR
    GET  /qr/state      {state, code_id, jid, age} for the page's poller
                        state: code | linked | logged_out | waiting
    POST /qr/relink     wipe the session and bounce the machine to re-pair

Leaking a live code does not expose your messages: whoever scans it links
*this bridge* to *their* account, not the other way round. It is still gated,
because that would hijack the bridge and silently feed someone else's
messages into the MCP.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path

import segno
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

WHATSMEOW_DB_PATH = Path(os.getenv("WHATSMEOW_DB_PATH", "/data/store/whatsapp.db"))
STORE_DIR = WHATSMEOW_DB_PATH.parent
QR_FILE = STORE_DIR / "pair_qr.txt"
LOGGED_OUT_FILE = STORE_DIR / "pair_logged_out"
STATIC_TOKEN = os.getenv("MCP_BEARER_TOKEN", "")

# whatsmeow rolls the code about every 20s; the bridge rewrites the file each
# time. Anything older than this is a leftover from a dead pairing attempt.
FRESH_SECONDS = 90


def _authorized(request) -> bool:
    if not STATIC_TOKEN:
        return False
    header = request.headers.get("authorization", "")
    presented = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not presented:
        presented = request.query_params.get("token", "")
    if not presented:
        return False
    return secrets.compare_digest(presented, STATIC_TOKEN)


def _deny() -> Response:
    # Deliberately no WWW-Authenticate: a Basic challenge would make the
    # browser pop a username/password box, which is not what we want.
    return HTMLResponse(
        "<h1>401</h1><p>Append <code>?token=YOUR_MCP_BEARER_TOKEN</code> to this URL.</p>",
        status_code=401,
    )


def _read_code() -> tuple[str | None, int]:
    """Return (code, age_seconds). code is None if missing or stale."""
    try:
        raw = QR_FILE.read_text(encoding="utf-8").strip().split("\n")
    except (OSError, ValueError):
        return None, -1
    if len(raw) < 2:
        return None, -1
    try:
        age = int(time.time()) - int(raw[0].strip())
    except ValueError:
        return None, -1
    code = raw[1].strip()
    if not code or age > FRESH_SECONDS:
        return None, age
    return code, age


def _linked_jid() -> str | None:
    """The JID of the paired device, or None if the session store is empty."""
    if not WHATSMEOW_DB_PATH.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{WHATSMEOW_DB_PATH}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT jid FROM whatsmeow_device LIMIT 1").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return row[0] if row and row[0] else None


def _state() -> dict:
    code, age = _read_code()
    if code:
        return {
            "state": "code",
            "code_id": hashlib.sha256(code.encode()).hexdigest()[:16],
            "jid": None,
            "age": age,
        }
    jid = _linked_jid()
    if LOGGED_OUT_FILE.exists():
        # A rejected session still leaves a whatsmeow_device row, and the
        # bridge won't emit a QR while that row exists -- so without this
        # marker the page would report a dead machine as linked.
        return {"state": "logged_out", "code_id": None, "jid": jid, "age": age}
    return {
        "state": "linked" if jid else "waiting",
        "code_id": None,
        "jid": jid,
        "age": age,
    }


def _responsive(svg: str) -> str:
    """Give segno's SVG a viewBox so CSS can scale it.

    svg_inline() emits fixed width/height and draws through a scale()
    transform, with no viewBox. Stretching that with width:100% resizes the
    viewport but not the drawing, which crops the QR on a narrow phone --
    exactly where this page gets used. Swap the fixed size for a viewBox.
    """
    match = re.match(r'<svg width="(\d+)" height="(\d+)"', svg)
    if not match:
        return svg
    w, h = match.group(1), match.group(2)
    return svg.replace(match.group(0), f'<svg viewBox="0 0 {w} {h}"', 1)


async def qr_svg(request):
    if not _authorized(request):
        return _deny()
    code, _ = _read_code()
    if not code:
        return PlainTextResponse("no live pairing code", status_code=404)
    svg = segno.make(code, error="l").svg_inline(scale=7, border=2, dark="#111", light="#fff")
    return Response(
        _responsive(svg), media_type="image/svg+xml", headers={"Cache-Control": "no-store"}
    )


async def qr_state(request):
    if not _authorized(request):
        return _deny()
    return JSONResponse(_state(), headers={"Cache-Control": "no-store"})


async def qr_relink(request):
    if not _authorized(request):
        return _deny()

    removed = []
    for name in (
        "whatsapp.db",
        "whatsapp.db-wal",
        "whatsapp.db-shm",
        "pair_qr.txt",
        "pair_logged_out",
    ):
        target = STORE_DIR / name
        try:
            target.unlink()
            removed.append(name)
        except FileNotFoundError:
            pass
        except OSError as exc:
            return HTMLResponse(
                f"<h1>Could not unlink {html.escape(name)}</h1><pre>{html.escape(str(exc))}</pre>",
                status_code=500,
            )

    # Exiting takes the whole machine down with us: start.sh's `wait -n` sees a
    # dead child, kills the rest, and Fly restarts the machine. The bridge then
    # boots with no session, enters the QR flow, and writes a fresh code.
    def _bounce():
        time.sleep(1.5)
        os._exit(1)

    threading.Thread(target=_bounce, daemon=True).start()

    token = request.query_params.get("token", "")
    back = f"/qr?token={html.escape(token, quote=True)}" if token else "/qr"
    return HTMLResponse(
        _shell(
            f"""
            <h1>Session wiped</h1>
            <p class="muted">Removed: {html.escape(", ".join(removed) or "nothing")}</p>
            <p>The machine is restarting. A fresh QR code should appear in 20-40 seconds.</p>
            <p><a class="btn" href="{back}">Back to the pairing page</a></p>
            """
        )
    )


def _shell(body: str, head: str = "") -> str:
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WhatsApp pairing</title>
{head}
<style>
  :root {{ color-scheme: light dark; }}
  body {{ margin: 0; min-height: 100vh; display: grid; place-items: center;
         font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
         background: #f5f5f4; color: #18181b; }}
  main {{ width: min(92vw, 420px); padding: 28px; text-align: center;
          background: #fff; border-radius: 16px; box-shadow: 0 1px 3px rgba(0,0,0,.12); }}
  h1 {{ font-size: 1.25rem; margin: 0 0 .5rem; }}
  .muted {{ color: #71717a; font-size: .875rem; }}
  #qr svg {{ width: 100%; height: auto; display: block; border-radius: 8px; }}
  .slot {{ min-height: 260px; display: grid; place-items: center; }}
  .btn {{ display: inline-block; margin-top: 1rem; padding: .55rem 1rem; border: 0;
          border-radius: 8px; background: #18181b; color: #fff; font: inherit;
          text-decoration: none; cursor: pointer; }}
  .btn.danger {{ background: #fff; color: #b91c1c; border: 1px solid #fca5a5; }}
  .ok {{ color: #15803d; font-weight: 600; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #09090b; color: #fafafa; }}
    main {{ background: #18181b; box-shadow: none; }}
    .btn {{ background: #fafafa; color: #18181b; }}
    .btn.danger {{ background: transparent; color: #fca5a5; border-color: #7f1d1d; }}
    .muted {{ color: #a1a1aa; }}
  }}
</style>
</head><body><main>{body}</main></body></html>"""


async def qr_page(request):
    if not _authorized(request):
        return _deny()

    token = request.query_params.get("token", "")
    qs = f"?token={html.escape(token, quote=True)}" if token else ""
    state = _state()

    if state["state"] == "linked":
        intro = f'<h1>Already linked</h1><p class="ok">{html.escape(state["jid"] or "")}</p>'
        slot = '<div class="slot muted"><p>No pairing needed.</p></div>'
    elif state["state"] == "logged_out":
        intro = (
            '<h1>Session rejected</h1>'
            '<p class="muted">WhatsApp logged this device out'
            f'{" (" + html.escape(state["jid"] or "") + ")" if state["jid"] else ""}. '
            'The bridge cannot show a QR until the dead session is cleared.</p>'
        )
        slot = (
            '<div class="slot muted" id="qr"><p>Press <b>Re-link this device</b> below.<br>'
            "The machine restarts and a code appears here in 20-40s.</p></div>"
        )
    elif state["state"] == "code":
        intro = "<h1>Scan to link</h1><p class=\"muted\">WhatsApp &rarr; Settings &rarr; Linked Devices &rarr; Link a Device</p>"
        slot = '<div class="slot" id="qr"><p>loading&hellip;</p></div>'
    else:
        intro = "<h1>Waiting for a code</h1>"
        slot = (
            '<div class="slot muted" id="qr"><p>The bridge is not in pairing mode.<br>'
            "Use Re-link below to wipe the session and restart it.</p></div>"
        )

    body = f"""
    {intro}
    {slot}
    <p class="muted" id="status"></p>
    <form method="post" action="/qr/relink{qs}"
          onsubmit="return confirm('Wipe the stored WhatsApp session and restart the machine? You will have to scan a new QR code.')">
      <button class="btn danger" type="submit">Re-link this device</button>
    </form>
    <script>
      const qs = {json.dumps(qs)};
      let shown = null;
      async function tick() {{
        try {{
          const s = await (await fetch('/qr/state' + qs, {{cache: 'no-store'}})).json();
          const status = document.getElementById('status');
          if (s.state === 'linked') {{
            status.textContent = 'Linked as ' + s.jid + ' - reload to confirm.';
            return;
          }}
          if (s.state === 'logged_out') {{
            status.textContent = 'Dead session - use Re-link below.';
            return;
          }}
          if (s.state === 'code' && s.code_id !== shown) {{
            const svg = await (await fetch('/qr.svg' + qs, {{cache: 'no-store'}})).text();
            const slot = document.getElementById('qr');
            if (slot && svg.indexOf('<svg') === 0) {{ slot.innerHTML = svg; shown = s.code_id; }}
          }}
          status.textContent = s.state === 'code'
            ? 'Code refreshes automatically (' + s.age + 's old).'
            : 'No live code yet.';
        }} catch (e) {{ /* transient during the restart; next tick retries */ }}
      }}
      tick();
      setInterval(tick, 3000);
    </script>
    """
    return HTMLResponse(_shell(body), headers={"Cache-Control": "no-store"})


routes = [
    Route("/qr", qr_page, methods=["GET"]),
    Route("/qr.svg", qr_svg, methods=["GET"]),
    Route("/qr/state", qr_state, methods=["GET"]),
    Route("/qr/relink", qr_relink, methods=["POST"]),
]
