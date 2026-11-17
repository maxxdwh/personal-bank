"""Offline test for personalbank.budget / personalbank.budget_ui against a synthetic DB (never the real one).
Run: PYTHONPATH=. .venv-mcp/bin/python scripts/test_budget.py"""
import sqlite3
import sys
import tempfile
from datetime import date
from pathlib import Path

from personalbank import budget, budget_ui, paths

SCHEMA = """
CREATE TABLE accounts(key TEXT PRIMARY KEY, iban TEXT, name TEXT, currency TEXT, owners TEXT,
  balance REAL, balance_type TEXT, synced_at TEXT, alias TEXT, closed INTEGER DEFAULT 0, earmarks TEXT);
CREATE TABLE transactions(id TEXT PRIMARY KEY, account_key TEXT NOT NULL, status TEXT, booking_date TEXT,
  amount REAL NOT NULL, currency TEXT, counterparty TEXT, description TEXT, mcc TEXT, raw TEXT);
CREATE TABLE sessions_meta(label TEXT PRIMARY KEY, valid_until TEXT);
"""

M, K = "XX00S", "XX00R"
TX = [
    # Sam
    (M, "2026-10-30", 40000, None, "Salary October"),
    (M, "2026-11-01", 6000, None, "PLAN bills"),
    (M, "2026-11-01", -4000, None, "PLAN joint"),
    (M, "2026-11-01", -1200.15, None, "Acme Insurance"),
    (M, "2026-11-02", -149, "Mobilo", "Mobilo"),
    (M, "2026-11-02", -299, "FiberNet", "FiberNet"),
    (M, "2026-11-03", -400, "WOLT", "WOLT"),
    (M, "2026-11-05", -99, "NETFLIX.COM", "Netflix"),
    (M, "2026-11-06", -250, "Corner Supermarket", "Supermarket"),
    (M, "2026-11-07", -1200, "City Dentist", "City Dentist"),
    (M, "2026-11-08", -1800, "Grand Department Store", "Department Store"),
    (M, "2026-11-09", -300, "Some Bar <script>alert(1)</script>", None),
    (M, "2026-11-10", -5000, None, "Invest deposit #REF"),
    # Robin
    (K, "2026-10-28", 32000, None, "Salary Oct 2026"),
    (K, "2026-11-01", -6000, None, "PLAN bills"),
    (K, "2026-11-01", -4000, None, "PLAN joint"),
    (K, "2026-11-02", -149, "Mobilo", "Mobilo"),
    (K, "2026-11-03", -600, "Wolt", "Wolt"),
    (K, "2026-11-04", -300, "Online Marketplace", "Online Marketplace"),
    (K, "2026-11-05", 130, None, "MARKETPLACE REFUND"),
    (K, "2026-11-06", -1400, "Family Clinic", "Family Clinic"),
]


def build(db_path: Path) -> None:
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)
    con.execute("INSERT INTO accounts VALUES (?,?,?,?,?,?,?,?,?,0,NULL)", (M, M, "Main", "EUR", '["me"]', 1, "CLBD", "2026-11-14T12:00:00+00:00", "Sam main"))
    con.execute("INSERT INTO accounts VALUES (?,?,?,?,?,?,?,?,?,0,NULL)", (K, K, "Main", "EUR", '["partner"]', 1, "CLBD", "2026-11-14T12:00:00+00:00", "Robin main"))
    con.execute("INSERT INTO sessions_meta VALUES ('me', '2027-05-12T00:00:00+00:00')")
    for i, (acc, d, amt, cp, desc) in enumerate(TX):
        con.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,NULL,NULL)", (f"t{i}", acc, "BOOK", d, amt, "EUR", cp, desc))
    con.commit()
    con.close()


def check_http() -> None:
    """Serve on a random localhost port; the page loads, a foreign Host header is refused, other paths 404."""
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", 0), budget_ui.Handler)
    except OSError as e:
        print(f"skip http check (cannot bind here: {e})")
        return
    port = srv.server_address[1]
    budget_ui.Handler.allowed_hosts = {f"127.0.0.1:{port}"}
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        def get(path, host=None):
            req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={"Host": host} if host else {})
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    return resp.status, resp.headers.get("Content-Security-Policy"), resp.read().decode()
            except urllib.error.HTTPError as e:
                return e.code, None, ""
        code, csp, body = get("/?month=2026-11")
        assert code == 200 and "default-src 'none'" in csp and "Fun money" in body, code
        assert get("/", host="evil.example")[0] == 403
        assert get("/etc/passwd")[0] == 404
        assert get("/?month=bad")[0] == 400
        print("http checks ok")
    finally:
        srv.shutdown()
        srv.server_close()


def main() -> None:
    with tempfile.TemporaryDirectory() as d:
        db_path = Path(d) / "finance.db"
        build(db_path)
        paths.DB_PATH = db_path  # readonly.db() reads paths.DB_PATH at call time
        plan = budget.load_plan(Path(__file__).resolve().parent.parent / "budget.example.json")
        today = date(2026, 11, 14)
        assert all(i["label"] != "Joint account" for i in budget.compute("2026-11", today, plan)["checklist"]), \
            "items with a later 'from' month are skipped"
        for item in plan["checklist"]:
            item.pop("from", None)
        r = budget.compute("2026-11", today, plan)

        status = {(i["person"], i["label"]): i["status"] for i in r["checklist"]}
        assert status[("Sam", "Robin's share of bills")] == "done", status
        assert status[("Robin", "Share of bills to Sam")] == "done"
        assert status[("Sam", "Joint account")] == "done"
        assert status[("Sam", "Rent")] == "due"                      # due 30 Nov
        assert status[("Sam", "Savings goal")] == "not_seen"         # goal, past grace
        assert status[("Sam", "Electricity")] == "late"     # bill, past grace
        assert status[("Sam", "Bank fee")] == "due"          # due 15th

        mx = next(p for p in r["people"] if p["name"] == "Sam")
        kr = next(p for p in r["people"] if p["name"] == "Robin")
        # fun: delivery 400 + department store 1800 + bar 300 (not subscriptions, groceries, dentist, invest, bills, transfers)
        assert abs(mx["spent"] - (400 + 1800 + 300)) < 0.01, mx
        # fun: delivery 600 + marketplace 300 - refund 130 (not the clinic)
        assert abs(kr["spent"] - (600 + 300 - 130)) < 0.01, kr
        caps = {c["key"]: c["spent"] for c in kr["caps"]}
        assert caps == {"delivery": 600, "shopping": 170}, caps
        pots = {p["key"]: p for p in r["pots"]}
        assert pots["joint"]["parts"][0]["spent"] == 250
        assert {x["who"]: x["spent"] for x in pots["health"]["parts"]} == {"Sam": 1200, "Robin": 1400}
        assert mx["days_left"] == 17

        page = budget_ui.render(r)
        assert "<script>" not in page and "&lt;script&gt;" in page, "third-party text must be escaped"
        assert "Fun money" in page and "Transfers &amp; bills" in page

        past = budget.compute("2026-10", today, plan)
        assert all(i["status"] in ("late", "not_seen", "done") for i in past["checklist"])
        try:
            budget.compute("2026-13", today, plan)
            raise AssertionError("bad month accepted")
        except ValueError:
            pass
        from personalbank import mcp_server
        paths.BUDGET_PATH = Path(__file__).resolve().parent.parent / "budget.example.json"
        assert "people" in mcp_server.budget_status("2026-11")
        assert len(mcp_server.get_transactions(limit=5)) == 5

        check_http()
        assert "httpx" not in sys.modules and "jwt" not in sys.modules
        out = Path(tempfile.gettempdir()) / "personal-bank-budget-test.html"
        out.write_text(page)
        print(f"ok - all checks passed. Rendered page: {out}")


if __name__ == "__main__":
    main()
