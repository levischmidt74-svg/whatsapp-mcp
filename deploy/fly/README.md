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
