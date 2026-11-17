"""Pull accounts/balances/transactions into the local SQLite cache.
The ONLY component that holds the signing key. Run manually or from cron/launchd, at most ~1-2x/day.
Usage: python -m personalbank.sync [--first] [--force]"""
import hashlib
import json
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone

from .common import CONFIG_PATH, DB_PATH, HOME, MAX_PAGES, SESSIONS_PATH, api_error, client, load_json

MIN_INTERVAL = timedelta(hours=4)   # PSD2 unattended access is rate-limited per account
PENDING_WINDOW_DAYS = 14            # refetch trailing window so pending rows get replaced
BACKFILL_TRIES = (730, 365, 180, 90)

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts(
  key TEXT PRIMARY KEY, iban TEXT, name TEXT, currency TEXT, owners TEXT,
  balance REAL, balance_type TEXT, synced_at TEXT, alias TEXT, closed INTEGER DEFAULT 0, earmarks TEXT);
CREATE TABLE IF NOT EXISTS transactions(
  id TEXT PRIMARY KEY, account_key TEXT NOT NULL, status TEXT, booking_date TEXT,
  amount REAL NOT NULL, currency TEXT, counterparty TEXT, description TEXT, mcc TEXT, raw TEXT);
CREATE INDEX IF NOT EXISTS tx_date ON transactions(booking_date);
CREATE INDEX IF NOT EXISTS tx_acct ON transactions(account_key);
CREATE TABLE IF NOT EXISTS sessions_meta(label TEXT PRIMARY KEY, valid_until TEXT);
"""


def account_key(a: dict) -> str:
    aid = a.get("account_id") or {}
    ident = aid.get("iban") or (aid.get("other") or {}).get("identification")
    return ident or f"{a.get('name')}|{a.get('currency')}|{a.get('uid')}"


def tx_id(account: str, t: dict) -> str:
    ref = t.get("entry_reference") or t.get("transaction_id")
    if ref:
        return f"{account}:{ref}"
    amt = t.get("transaction_amount", {})
    basis = "|".join(str(x) for x in (
        account, t.get("booking_date") or t.get("value_date"), amt.get("amount"), amt.get("currency"),
        t.get("credit_debit_indicator"), " ".join(t.get("remittance_information") or [])))
    return "h:" + hashlib.sha256(basis.encode()).hexdigest()[:32]


def signed_amount(t: dict) -> float:
    v = float(t["transaction_amount"]["amount"])
    return -abs(v) if t.get("credit_debit_indicator") == "DBIT" else abs(v)


def fetch_transactions(c, uid: str, date_from: date):
    params = {"date_from": date_from.isoformat(), "date_to": date.today().isoformat()}
    for _ in range(MAX_PAGES):
        r = c.get(f"/accounts/{uid}/transactions", params=params)
        if r.status_code != 200:
            raise RuntimeError(api_error(r))
        body = r.json()
        yield from body.get("transactions", [])
        if not body.get("continuation_key"):
            return
        params["continuation_key"] = body["continuation_key"]
    raise RuntimeError("too many pages")


def main() -> None:
    first, force = "--first" in sys.argv, "--force" in sys.argv
    sessions = load_json(SESSIONS_PATH, {})
    if not sessions:
        sys.exit("No sessions. Run: python -m personalbank.auth start <label>")

    HOME.mkdir(mode=0o700, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    DB_PATH.chmod(0o600)
    db.executescript(SCHEMA)
    cols = [r[1] for r in db.execute("PRAGMA table_info(accounts)")]
    if "alias" not in cols:
        db.execute("ALTER TABLE accounts ADD COLUMN alias TEXT")
    if "earmarks" not in cols:
        db.execute("ALTER TABLE accounts ADD COLUMN earmarks TEXT")
    if "closed" not in cols:
        db.execute("ALTER TABLE accounts ADD COLUMN closed INTEGER DEFAULT 0")
    closed = load_json(CONFIG_PATH, {}).get("closed", [])  # last digits of closed accounts: history kept, never synced
    earmarks = load_json(CONFIG_PATH, {}).get("earmarks", {})  # {"<iban suffix>": {"Goal name": {"amount": N, "as_of": "YYYY-MM-DD"}}}
    def earmarks_for(k):
        e = next((v for suf, v in earmarks.items() if k.endswith(suf)), None)
        return json.dumps(e) if e else None
    def is_closed(k):
        return any(k.endswith(suf) for suf in closed)
    aliases = load_json(CONFIG_PATH, {}).get("aliases", {})  # {"<last digits of IBAN>": "Name"}
    def alias_for(k):
        return next((v for suf, v in aliases.items() if k.endswith(suf)), None)
    now = datetime.now(timezone.utc)

    # Dedupe joint accounts: same IBAN shows up under both sessions with different uids.
    plan: dict[str, dict] = {}
    for label, s in sessions.items():
        db.execute("INSERT OR REPLACE INTO sessions_meta VALUES (?,?)", (label, s["valid_until"]))
        left = (datetime.fromisoformat(s["valid_until"]) - now).days
        if left <= 14:
            msg = f"Bank login for '{label}' " + (f"expires in {left} days" if left >= 0 else "has expired")
            print(f"! {msg} - re-run: python -m personalbank.auth start {label}")
            subprocess.run(["osascript", "-e", "on run argv", "-e", 'display notification (item 1 of argv) with title "personal-bank"',
                            "-e", "end run", f"{msg}. See README.md in the personal-bank folder"], capture_output=True)
        if datetime.fromisoformat(s["valid_until"]) < now:
            print(f"! session '{label}' EXPIRED - re-run: python -m personalbank.auth start {label}")
            continue
        for a in s["accounts"]:
            k = account_key(a)
            p = plan.setdefault(k, {"uid": a["uid"], "acct": a, "owners": [], "label": label})
            p["owners"].append(label)

    for k in [k for k in plan if is_closed(k)]:
        db.execute("UPDATE accounts SET closed=1, balance=NULL WHERE key=?", (k,))
        del plan[k]
    for (k,) in db.execute("SELECT key FROM accounts").fetchall():
        if is_closed(k):
            db.execute("UPDATE accounts SET closed=1, balance=NULL WHERE key=?", (k,))
    with_clients = {}
    for k, p in plan.items():
        row = db.execute("SELECT synced_at FROM accounts WHERE key=?", (k,)).fetchone()
        if row and row[0] and not force and now - datetime.fromisoformat(row[0]) < MIN_INTERVAL:
            print(f"- {k[-4:]}: synced recently, skipping (use --force to override)")
            continue
        # a fresh client per session label so each uses its own session's account uid
        c = with_clients.get(p["label"]) or with_clients.setdefault(p["label"], client())
        a = p["acct"]

        balance, btype = None, None
        r = c.get(f"/accounts/{p['uid']}/balances")
        if r.status_code == 200:
            bals = r.json().get("balances", [])
            pick = next((b for b in bals if b.get("balance_type") in ("CLBD", "ITBD", "XPCD")), bals[0] if bals else None)
            if pick:
                balance, btype = float(pick["balance_amount"]["amount"]), pick.get("balance_type")
        else:
            print(f"! balances {k[-4:]}: {api_error(r)}")

        have = db.execute("SELECT COUNT(*) FROM transactions WHERE account_key=?", (k,)).fetchone()[0]
        n = 0
        if first or have == 0:
            attempts = BACKFILL_TRIES
        else:
            attempts = (PENDING_WINDOW_DAYS,)
        for days in attempts:
            try:
                txs = list(fetch_transactions(c, p["uid"], date.today() - timedelta(days=days)))
                break
            except RuntimeError as e:
                print(f"! transactions {k[-4:]} ({days}d): {e}")
                txs = None
        if txs is None:
            continue
        # replace pending rows in the window (they change id/amount or vanish when booked)
        db.execute("DELETE FROM transactions WHERE account_key=? AND status='PDNG'", (k,))
        for t in txs:
            try:
                amt = signed_amount(t)
            except (KeyError, ValueError):
                continue
            cp = (t.get("creditor") or {}).get("name") if amt < 0 else (t.get("debtor") or {}).get("name")
            db.execute("INSERT OR REPLACE INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?)", (
                tx_id(k, t), k, t.get("status"), t.get("booking_date") or t.get("value_date") or t.get("transaction_date"),
                amt, t["transaction_amount"].get("currency"), cp,
                " ".join(t.get("remittance_information") or []) or t.get("note"),
                t.get("merchant_category_code"), None))  # raw payload intentionally not stored
            n += 1
        db.execute("INSERT OR REPLACE INTO accounts VALUES (?,?,?,?,?,?,?,?,?,0,?)", (
            k, (a.get("account_id") or {}).get("iban"), a.get("name"), a.get("currency"),
            json.dumps(p["owners"]), balance, btype, now.isoformat(), alias_for(k), earmarks_for(k)))
        db.commit()
        print(f"+ {a.get('name') or k[-4:]}: {n} transactions, balance {balance} (owners: {', '.join(p['owners'])})")
    db.commit()
    for c in with_clients.values():
        c.close()


if __name__ == "__main__":
    main()
