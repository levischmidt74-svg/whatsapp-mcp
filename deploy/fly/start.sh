#!/bin/bash
# Tiny supervisor: bridge + MCP HTTP + Caddy. If any one dies, kill the
# rest and let Fly restart the machine.
set -euo pipefail

if [[ -z "${MCP_BEARER_TOKEN:-}" ]]; then
	echo "FATAL: MCP_BEARER_TOKEN is unset; refusing to start an unauthenticated public MCP." >&2
	exit 1
fi

mkdir -p /data/store

echo "[start.sh] launching whatsapp-bridge on 127.0.0.1:${WHATSAPP_BRIDGE_PORT}"
( cd /data && exec /app/bridge/whatsapp-bridge ) &
BRIDGE_PID=$!

echo "[start.sh] launching mcp http server on 127.0.0.1:${MCP_PORT}"
( cd /app/mcp-server && exec uv run --frozen python mcp_http.py ) &
MCP_PID=$!

echo "[start.sh] launching caddy on :${PUBLIC_PORT}"
exec_caddy() {
	exec caddy run --config /app/Caddyfile --adapter caddyfile
}
exec_caddy &
CADDY_PID=$!

cleanup() {
	echo "[start.sh] cleanup, stopping children"
	kill "$BRIDGE_PID" "$MCP_PID" "$CADDY_PID" 2>/dev/null || true
	wait 2>/dev/null || true
}
trap cleanup SIGTERM SIGINT

wait -n "$BRIDGE_PID" "$MCP_PID" "$CADDY_PID"
EXIT=$?
echo "[start.sh] a child exited with code $EXIT, tearing down"
cleanup
exit "$EXIT"
