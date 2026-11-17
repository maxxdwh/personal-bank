"""Authorize one person's bank accounts in two non-interactive steps.
  python -m personalbank.auth banks <COUNTRY>          -> lists the bank names Enable Banking knows for a country (e.g. DK)
  python -m personalbank.auth start <label>            -> prints a login URL
  pbpaste | python -m personalbank.auth finish <label> -   -> URL read from stdin (copy it first): keeps it out of shell history and `ps`
  python -m personalbank.auth finish <label> '<url>'       -> same, URL as a quoted argument (visible in history and `ps`)"""
import json
import secrets
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from .common import HOME, SESSIONS_PATH, api_error, bank, client, consent_days, load_json, secure_write

REDIRECT = "https://example.com/callback"


def pending_path(label):
    return HOME / f"pending_{label}.json"


def banks(country: str) -> None:
    with client() as c:
        r = c.get("/aspsps", params={"country": country.upper(), "psu_type": "personal"})
    if r.status_code != 200:
        sys.exit(f"listing banks failed: {api_error(r)}")
    for a in r.json().get("aspsps", []):
        days = int(a.get("maximum_consent_validity") or 0) // 86400
        print(f"{a.get('name')}  (max consent: {days or '?'} days{', beta' if a.get('beta') else ''})")


def start(label: str) -> None:
    b = bank()
    state = secrets.token_urlsafe(24)
    valid_until = (datetime.now(timezone.utc) + timedelta(days=consent_days() - 1)).isoformat()
    with client() as c:
        r = c.post("/auth", json={
            "access": {"valid_until": valid_until},
            "aspsp": b,
            "state": state, "redirect_url": REDIRECT, "psu_type": "personal"})
    if r.status_code != 200:
        sys.exit(f"auth failed: {api_error(r)}")
    secure_write(pending_path(label), json.dumps({"state": state, "valid_until": valid_until}))
    print(f"\nLog in to {b['name']} as '{label}' (with their own bank login):\n\n{r.json()['url']}\n")
    print("You'll land on example.com. Copy the FULL address-bar URL, then run:")
    print(f"  pbpaste | .venv-sync/bin/python -m personalbank.auth finish {label} -")


def finish(label: str, pasted: str) -> None:
    pp = pending_path(label)
    if not pp.exists():
        sys.exit(f"no pending login for '{label}'. Run start first.")
    pend = json.loads(pp.read_text())
    if time.time() - pp.stat().st_mtime > 3600:
        pp.unlink()
        sys.exit("pending login is older than 1 hour. Run start again.")
    if pasted == "-":
        pasted = sys.stdin.read()
    q = parse_qs(urlparse(pasted.strip()).query)
    if q.get("state", [""])[0] != pend["state"]:
        sys.exit("state mismatch: callback is not from this login attempt. Nothing saved.")
    if "code" not in q:
        sys.exit(f"no code in URL (error={q.get('error', ['?'])[0]}). Nothing saved.")
    with client() as c:
        r = c.post("/sessions", json={"code": q["code"][0]})
    if r.status_code != 200:
        sys.exit(f"session exchange failed: {api_error(r)}")
    s = r.json()
    sessions = load_json(SESSIONS_PATH, {})
    sessions[label] = {
        "session_id": s["session_id"],
        "valid_until": s.get("access", {}).get("valid_until", pend["valid_until"]),
        "accounts": s.get("accounts", []),
    }
    secure_write(SESSIONS_PATH, json.dumps(sessions, indent=1))
    pp.unlink()
    print(f"Saved '{label}': {len(sessions[label]['accounts'])} account(s), valid until {sessions[label]['valid_until']}")
    print("Next: .venv-sync/bin/python -m personalbank.sync --first")


def main() -> None:
    a = sys.argv[1:]
    if len(a) == 2 and a[0] == "banks" and a[1].isalpha() and len(a[1]) == 2:
        banks(a[1])
    elif len(a) == 2 and a[0] == "start" and a[1].isidentifier():
        start(a[1])
    elif len(a) == 3 and a[0] == "finish" and a[1].isidentifier():
        finish(a[1], a[2])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
