# Remote deploy on Fly.io

Runs the WhatsApp bridge + MCP HTTP server in a single Fly machine, fronted
by Caddy doing bearer-token auth. The bridge stays bound to `127.0.0.1` so
its unauthenticated REST API is never reachable from the internet.

## Architecture

```
                       Fly edge (TLS)
                             │
                             ▼
              public :8000  ── Caddy ──┐
                                       │  Authorization: Bearer $MCP_BEARER_TOKEN
                                       ▼
                              127.0.0.1:3000  mcp_http.py (FastMCP, streamable-http)
                                       │  http://127.0.0.1:8080/api
                                       ▼
                              127.0.0.1:8080  whatsapp-bridge (whatsmeow)
                                       │
                                       ▼
                              /data/store/{messages.db, whatsapp.db}
                              (Fly volume "whatsapp_data")
```

If the volume is lost, the WhatsApp pairing is lost and you re-pair.
If the machine is stopped (auto-stop or manual), the WhatsApp Web session
disconnects. `auto_stop_machines = "off"` in `fly.toml` keeps it alive.

## First-time deploy

Run from the repo root (`whatsapp-mcp/`):

```bash
# 1. Create app + volume in your preferred region
flyctl apps create lawnsmith-whatsapp
flyctl volumes create whatsapp_data --size 1 --region dfw -a lawnsmith-whatsapp

# 2. Generate and store a long random bearer token (KEEP A COPY for Claude mobile)
TOKEN=$(openssl rand -base64 48 | tr -d '\n')
echo "$TOKEN"
flyctl secrets set MCP_BEARER_TOKEN="$TOKEN" -a lawnsmith-whatsapp

# 3. Deploy
flyctl deploy --config deploy/fly/fly.toml --dockerfile deploy/fly/Dockerfile .

# 4. Watch logs for the QR pairing code
flyctl logs -a lawnsmith-whatsapp
```

The Go bridge prints an ASCII QR to stdout on first boot. Scan it with
WhatsApp on your phone (Settings → Linked Devices → Link a Device). The
session is then persisted to the volume and the QR will not appear again
on subsequent restarts.

If the QR rolls over before you scan it, restart the machine:
`flyctl machine restart -a lawnsmith-whatsapp`.

## Pairing from a browser (no flyctl needed)

Reading the QR out of `flyctl logs` is painful: the bridge draws it with
`qrterminal.GenerateHalfBlock`, and log pipelines mangle half-block glyphs.
So the bridge also writes every code it emits to `/data/store/pair_qr.txt`
(`writePairQR` in `main.go`) and the Python app re-encodes it as an SVG:

    https://lawnsmith-whatsapp.fly.dev/qr?token=$MCP_BEARER_TOKEN

Open that on your laptop, scan with your phone, done. The page polls every
3s, so it picks up each ~20s rollover on its own and switches to "Already
linked" once the handshake lands. Routes (all admin-bearer gated, as a
header or `?token=` so the link is clickable):

| Route | Purpose |
| --- | --- |
| `GET /qr` | the page |
| `GET /qr.svg` | current code as an SVG QR |
| `GET /qr/state` | `{state, code_id, jid, age}` for the poller |
| `POST /qr/relink` | wipe the session and bounce the machine |

A code older than 90s is treated as a leftover from a dead attempt and shown
as "no live code", so a stale file can't hand you an unscannable QR.

`state` is one of:

| state | meaning |
| --- | --- |
| `code` | a live code is on screen, scan it |
| `linked` | paired and connected, nothing to do |
| `logged_out` | a session exists but WhatsApp rejected it - press Re-link |
| `waiting` | no code yet (booting, or between attempts) |

`logged_out` exists because a rejected session is indistinguishable from a
healthy one by the database alone: both leave a row in `whatsmeow_device`,
and the bridge emits no QR for either. So `main.go` drops a
`store/pair_logged_out` marker on the `LoggedOut` event and removes it on
`Connected`. Without it the page would cheerfully report a dead machine as
"Already linked".

### Typical first run after the machine has been logged out

1. `flyctl deploy ...` once, to ship this page (needed only this one time).
2. Open `/qr?token=...` - it will say **Session rejected**.
3. Press **Re-link this device**, confirm.
4. Wait ~30s; the QR appears on its own. Scan it.
5. The page flips to **Already linked**. No flyctl, no SSH, no log scraping.

### Why there's a Re-link button

The bridge only enters the QR flow when `client.Store.ID == nil`. A session
that exists but is logged out takes the "already logged in" branch instead:
it connects, gets a `LoggedOut` event, warns, and loops — **no QR, ever**.
Clearing `whatsapp.db` is what forces a fresh pair.

`POST /qr/relink` does that over HTTP so you don't need `flyctl ssh console`:
it unlinks `whatsapp.db{,-wal,-shm}` and `pair_qr.txt`, then exits the
process. `start.sh`'s `wait -n` sees the dead child, tears down its
siblings, and Fly restarts the machine — which boots with no session, enters
the QR flow, and writes a fresh code for the page to pick up. Takes 20-40s.
`messages.db` is untouched, so stored history survives.

It is destructive and irreversible (you must re-scan), hence the confirm
dialog and the token.

### QR leak risk

Scanning links *this bridge* to *the scanning phone's* account, not the
reverse — a leaked code does not expose your message history. The real risk
is someone linking the bridge to their own account and quietly feeding their
messages into your MCP, which is why the page is token-gated like `/mcp`.

## Connecting Claude

In the Claude mobile (or desktop) app, add a custom remote MCP connector:

- **URL:** `https://lawnsmith-whatsapp.fly.dev/mcp`
- **Authorization header:** `Bearer <token from step 2>`

Then in Claude, allow the read-only tools (`list_messages`, `search_contacts`,
`get_chat`, etc.) without confirmation. **Keep the write tools (`send_message`,
`send_file`, `send_audio_message`) set to require approval per call** — see
the threat model below.

## Threat model & operational notes

- **The bridge has no auth.** The Go bridge's `/api/send` endpoint will send
  any message to any recipient with no credentials. It is bound to
  `127.0.0.1` only and never reverse-proxied. Do not change that.
- **The MCP transport is gated by a single bearer token.** Anyone with that
  token can read every message in your WhatsApp history and send messages
  as you. Treat it like a password. Rotate by `flyctl secrets set
  MCP_BEARER_TOKEN=...`.
- **Prompt injection from incoming messages is a real attack vector.** A
  malicious DM can contain instructions that Claude will read via
  `list_messages` and may try to act on (e.g. `send_message` to attacker).
  Mitigate by leaving destructive tools in "ask before running" mode.
- **The session DB (`/data/store/whatsapp.db`) is sensitive.** Anyone with
  that file can impersonate you on WhatsApp until you unlink the device
  from your phone. Fly volumes are encrypted at rest, so the on-disk file
  is protected against someone walking off with the underlying hardware,
  but anyone with `flyctl` access to your org can `ssh sftp` it off in
  cleartext. Treat your Fly auth token like a password too.
- **First-pair window.** Until the QR is scanned, `whatsapp.db` is empty
  and there's no risk; after pairing, treat the volume as PII storage.
- **Revoke fast.** If you suspect compromise: WhatsApp on phone →
  Linked Devices → log out the Fly device. Then rotate `MCP_BEARER_TOKEN`.

## Updating

```bash
git pull
flyctl deploy --config deploy/fly/fly.toml --dockerfile deploy/fly/Dockerfile .
```

The deploy preserves the volume, so the WhatsApp session and message
history survive across deploys.
