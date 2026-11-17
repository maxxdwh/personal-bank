"""Sync/auth helpers. Holds the signing key and the network client. The MCP server must NOT import this."""
import json
import os
import tempfile
import time
from pathlib import Path

import httpx
import jwt  # PyJWT

from .paths import CONFIG_PATH, DB_PATH, HOME, KEY_PATH, SESSIONS_PATH  # noqa: F401 (re-exported)

API = "https://api.enablebanking.com"
MAX_PAGES = 200  # cap on continuation_key paging


def secure_write(path: Path, text: str) -> None:
    """Atomic, owner-only write: temp file (0600) in the same dir, then rename over the target."""
    HOME.mkdir(mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=HOME, prefix=".tmp-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def load_json(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def app_id() -> str:
    return load_json(CONFIG_PATH, {})["app_id"]


def bank() -> dict:
    """The bank to authorize against: config.json -> "bank": {"name": "...", "country": "XX"} (names as listed by `auth banks XX`)."""
    b = load_json(CONFIG_PATH, {}).get("bank") or {}
    if not b.get("name") or not b.get("country"):
        raise SystemExit('Set "bank": {"name": "...", "country": "XX"} in ' + str(CONFIG_PATH))
    return {"name": b["name"], "country": b["country"].upper()}


def consent_days() -> int:
    """How long to ask the bank for access. Banks cap this (see `auth banks XX`); 180 is the usual PSD2 maximum."""
    return int(load_json(CONFIG_PATH, {}).get("consent_days", 180))


def make_jwt() -> str:
    now = int(time.time())
    return jwt.encode(
        {"iss": "enablebanking.com", "aud": "api.enablebanking.com", "iat": now, "exp": now + 3600},
        KEY_PATH.read_bytes(),
        algorithm="RS256",
        headers={"kid": app_id()},
    )


def client() -> httpx.Client:
    # TLS verification on (certifi bundle). trust_env=False: ignore HTTPS_PROXY / SSL_CERT_FILE from the environment.
    # No redirects followed. The token is never logged.
    return httpx.Client(
        base_url=API,
        headers={"Authorization": f"Bearer {make_jwt()}"},
        timeout=30,
        trust_env=False,
        follow_redirects=False,
    )


def api_error(r: httpx.Response) -> str:
    """Status + message only; never echo request headers or full payloads."""
    try:
        msg = str(r.json().get("message", ""))[:200]
    except Exception:
        msg = ""
    return f"HTTP {r.status_code} {msg}".strip()
