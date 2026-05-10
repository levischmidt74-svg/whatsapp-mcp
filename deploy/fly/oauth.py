"""SQLite-backed OAuth 2.1 + PKCE storage for the WhatsApp MCP.

Single-user model: there are no real user accounts. Authorization on the
consent screen is gated by knowing the shared secret (MCP_BEARER_TOKEN).
After consent, an access token is minted and used as Bearer for /mcp.

Tables (mirrors the lawnsmith-hq schema):
    oauth_clients              — DCR-registered clients
    oauth_authorization_codes  — short-lived (60s), single-use, PKCE-bound
    oauth_access_tokens        — long-lived (30d), opaque random tokens
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from typing import Any

DB_PATH = os.getenv("OAUTH_DB_PATH", "/data/store/oauth.db")
SHARED_SECRET = os.getenv("MCP_BEARER_TOKEN", "")
CODE_TTL_SECONDS = 60
TOKEN_TTL_SECONDS = 30 * 24 * 3600


@contextmanager
def _db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, isolation_level="DEFERRED")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with _db() as c:
        c.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS oauth_clients (
                client_id TEXT PRIMARY KEY,
                client_secret_hash TEXT,
                client_name TEXT,
                redirect_uris TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS oauth_authorization_codes (
                code TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                redirect_uri TEXT NOT NULL,
                code_challenge TEXT NOT NULL,
                code_challenge_method TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                used INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS oauth_access_tokens (
                token TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_tokens_expires ON oauth_access_tokens(expires_at);
            """
        )


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def verify_shared_secret(provided: str) -> bool:
    if not SHARED_SECRET or not provided:
        return False
    return secrets.compare_digest(provided, SHARED_SECRET)


def register_client(redirect_uris: list[str], client_name: str | None) -> dict[str, Any]:
    if not redirect_uris:
        raise ValueError("redirect_uris required")
    client_id = "mcp-" + secrets.token_hex(8)
    client_secret = secrets.token_urlsafe(32)
    client_secret_hash = _b64u(hashlib.sha256(client_secret.encode()).digest())
    with _db() as c:
        c.execute(
            "INSERT INTO oauth_clients (client_id, client_secret_hash, client_name, "
            "redirect_uris, created_at) VALUES (?, ?, ?, ?, ?)",
            (client_id, client_secret_hash, client_name, json.dumps(redirect_uris), int(time.time())),
        )
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uris": redirect_uris,
        "client_name": client_name,
    }


def get_client(client_id: str) -> dict[str, Any] | None:
    if not client_id:
        return None
    with _db() as c:
        row = c.execute(
            "SELECT * FROM oauth_clients WHERE client_id=?", (client_id,)
        ).fetchone()
    if not row:
        return None
    return {
        "client_id": row["client_id"],
        "client_secret_hash": row["client_secret_hash"],
        "client_name": row["client_name"],
        "redirect_uris": json.loads(row["redirect_uris"]),
    }


def create_authorization_code(
    *,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    code_challenge_method: str,
) -> str:
    if code_challenge_method != "S256":
        raise ValueError("only S256 PKCE supported")
    code = secrets.token_hex(24)
    now = int(time.time())
    with _db() as c:
        c.execute(
            "INSERT INTO oauth_authorization_codes "
            "(code, client_id, redirect_uri, code_challenge, code_challenge_method, "
            "expires_at, used, created_at) VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
            (code, client_id, redirect_uri, code_challenge, code_challenge_method,
             now + CODE_TTL_SECONDS, now),
        )
    return code


def exchange_code(
    *,
    code: str,
    code_verifier: str,
    redirect_uri: str,
    client_id: str,
) -> dict[str, Any] | None:
    """Exchange an authorization code for an access token. Atomic mark-used + mint."""
    now = int(time.time())
    with _db() as c:
        row = c.execute(
            "SELECT * FROM oauth_authorization_codes WHERE code=? AND used=0", (code,),
        ).fetchone()
        if not row or row["expires_at"] < now:
            return None
        if row["client_id"] != client_id or row["redirect_uri"] != redirect_uri:
            return None
        challenge = _b64u(hashlib.sha256(code_verifier.encode()).digest())
        if not secrets.compare_digest(challenge, row["code_challenge"]):
            return None
        affected = c.execute(
            "UPDATE oauth_authorization_codes SET used=1 WHERE code=? AND used=0", (code,)
        ).rowcount
        if affected != 1:
            return None  # someone else won the race
        token = secrets.token_hex(32)
        c.execute(
            "INSERT INTO oauth_access_tokens (token, client_id, expires_at, created_at) "
            "VALUES (?, ?, ?, ?)",
            (token, client_id, now + TOKEN_TTL_SECONDS, now),
        )
    return {"access_token": token, "token_type": "Bearer", "expires_in": TOKEN_TTL_SECONDS}


def validate_token(token: str) -> bool:
    if not token:
        return False
    now = int(time.time())
    with _db() as c:
        row = c.execute(
            "SELECT 1 FROM oauth_access_tokens WHERE token=? AND expires_at > ?",
            (token, now),
        ).fetchone()
    return row is not None
