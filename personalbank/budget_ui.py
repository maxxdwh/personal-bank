"""Local, read-only budget page. Serves one HTML page on 127.0.0.1 only, GET only, no scripts, no outbound network.
Run: ./run_budget.sh   (or: PYTHONPATH=. .venv-mcp/bin/python -m personalbank.budget_ui [--port 8765] [--no-open])"""
import argparse
import html
import sys
import webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import budget

CSP = "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"

STATUS = {
    "done": ("Done", "ok"),
    "due": ("Due", "neutral"),
    "late": ("Late", "bad"),
    "not_seen": ("Not seen", "muted"),
}

CSS = """
:root {
  --bg: #ffffff; --fg: #37352f; --muted: #787774; --border: #e9e9e7; --soft: #f7f6f3;
  --ok: #2b7a4b; --ok-bg: #edf3ec; --bad: #b4372f; --bad-bg: #fbe4e2; --bar: #d3d1cb;
  --font: "Geist", ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  --mono: "Geist Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
}
@media (prefers-color-scheme: dark) {
  :root { --bg: #191919; --fg: #e3e2e0; --muted: #9b9a97; --border: #2f2f2f; --soft: #252525;
          --ok: #6cc28f; --ok-bg: #23302a; --bad: #f08a80; --bad-bg: #3b2321; --bar: #3a3a3a; color-scheme: dark; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg); font-family: var(--font); font-size: 15px; line-height: 1.5;
       padding: 40px max(16px, env(safe-area-inset-right, 0px)) 80px max(16px, env(safe-area-inset-left, 0px)); }
main { max-width: 960px; margin: 0 auto; display: flex; flex-direction: column; gap: 8px; }
h1 { font-size: 2rem; margin: 0; font-weight: 700; letter-spacing: -0.02em; }
h2 { font-size: 1.25rem; margin: 28px 0 4px; font-weight: 600; letter-spacing: -0.01em; }
.big, td.n, th.n, .row > span:last-child, .d { font-family: var(--mono); }
p { margin: 0; }
a { color: inherit; }
.muted { color: var(--muted); }
.small { font-size: 0.85rem; }
.nav { display: flex; gap: 12px; align-items: baseline; flex-wrap: wrap; }
.nav a { color: var(--muted); text-decoration: none; padding: 2px 8px; border-radius: 4px; }
.nav a:hover, .nav a:focus-visible { background: var(--soft); color: var(--fg); outline: none; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 16px; }
.grid > * { min-width: 0; }
.card { border: 1px solid var(--border); border-radius: 6px; padding: 16px; display: flex; flex-direction: column; gap: 10px; }
.card h3 { margin: 0; font-size: 1rem; font-weight: 600; }
.big { font-size: 2.2rem; font-weight: 600; line-height: 1.1; letter-spacing: -0.03em; font-variant-numeric: tabular-nums; }
.big.neg { color: var(--bad); }
.bar { height: 8px; background: var(--soft); border-radius: 4px; overflow: hidden; }
.bar > span { display: block; height: 100%; background: var(--ok); }
.bar.over > span { background: var(--bad); }
.row { display: flex; justify-content: space-between; gap: 12px; font-variant-numeric: tabular-nums; }
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 0.92rem; }
th, td { border-bottom: 1px solid var(--border); padding: 7px 8px; text-align: left; vertical-align: top; }
th { color: var(--muted); font-weight: 500; font-size: 0.82rem; }
td.n, th.n { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.in { color: var(--ok); }
.pill { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 0.8rem; white-space: nowrap; }
.pill.ok { background: var(--ok-bg); color: var(--ok); }
.pill.bad { background: var(--bad-bg); color: var(--bad); }
.pill.neutral { background: var(--soft); color: var(--fg); }
.pill.muted { background: var(--soft); color: var(--muted); }
.banner { background: var(--bad-bg); color: var(--bad); border-radius: 6px; padding: 10px 14px; }
"""


def kr(v, sign=False) -> str:
    if v is None:
        return ""
    s = f"{abs(v):,.0f}"
    if sign:
        return ("+" if v > 0 else "−") + s
    return ("−" if v < 0 else "") + s


def esc(s) -> str:
    return html.escape("" if s is None else str(s), quote=True)


def bar(spent: float, total: float) -> str:
    pct = 0 if not total else max(0.0, min(100.0, 100 * spent / total))
    over = " over" if total and spent > total else ""
    return f'<div class="bar{over}" role="img" aria-label="{pct:.0f}% used"><span style="width:{pct:.0f}%"></span></div>'


def fmt_day(iso: str | None) -> str:
    if not iso:
        return ""
    d = date.fromisoformat(iso)
    return f"{d.day} {d.strftime('%b')}"


def render(r: dict) -> str:
    out = [f"<title>Budget · {esc(r['month_label'])}</title>", f"<style>{CSS}</style>", "<main>"]
    out.append(f'<div class="nav"><h1>{esc(r["month_label"])}</h1>'
               f'<a href="/?month={esc(r["prev"])}">‹ Previous</a>'
               f'<a href="/">This month</a>'
               f'<a href="/?month={esc(r["next"])}">Next ›</a></div>')
    sync = (r["last_sync"] or "")[:16].replace("T", " ")
    logins = ", ".join(f"{esc(s['label'])} {s['days_left']} days" for s in r["sessions"])
    out.append(f'<p class="small muted">Last bank sync {esc(sync)} UTC · bank logins valid: {logins}</p>')
    for acc in r["missing_accounts"]:
        out.append(f'<p class="banner">Account “{esc(acc)}” from budget.json isn’t in the data. Check the name.</p>')
    if r["late"]:
        out.append(f'<p class="banner">{r["late"]} planned payment(s) are late. See the checklist below.</p>')

    # Fun money
    out.append("<h2>Fun money</h2>")
    when = ("left this month" if r["current"] else "left at month end" if r["past"] else "planned")
    out.append('<div class="grid">')
    for p in r["people"]:
        neg = " neg" if p["left"] < 0 else ""
        per_day = (f' · {kr(p["per_day"])} a day for {p["days_left"]} days'
                   if r["current"] and p["per_day"] is not None else "")
        rows = [f'<div class="card"><h3>{esc(p["name"])}</h3>',
                f'<div class="big{neg}">{kr(p["left"])}</div>',
                f'<p class="small muted">{when} of {kr(p["fun_money"])} · spent {kr(p["spent"])}{per_day}</p>',
                bar(p["spent"], p["fun_money"])]
        for cap in p["caps"]:
            rows.append(f'<div class="row small"><span>{esc(cap["label"])}</span>'
                        f'<span>{kr(cap["spent"])} of {kr(cap["cap"])}</span></div>{bar(cap["spent"], cap["cap"])}')
        capped = {c["key"] for c in p["caps"]}
        for c in p["by_category"]:
            if c["key"] not in capped:
                rows.append(f'<div class="row small muted"><span>{esc(c["label"])}</span><span>{kr(c["amount"])}</span></div>')
        rows.append("</div>")
        out.append("".join(rows))
    out.append("</div>")

    # Checklist
    out.append("<h2>Transfers &amp; bills</h2>")
    out.append('<p class="small muted">Planned payments for this month and whether they went through. '
               'Transfers marked “Not seen” go into savings goals/pots, which the bank feed may not show; '
               'if you can see them in the app, they’re fine.</p>')
    out.append('<div class="table-wrap"><table><thead><tr><th>Who</th><th>What</th><th class="n">Planned</th>'
               '<th class="n">Actual</th><th>Date</th><th>Status</th></tr></thead><tbody>')
    for i in r["checklist"]:
        label, cls = STATUS[i["status"]]
        planned = ("~" if i["variable"] else "") + kr(i["expected"], sign=True)
        actual = kr(i["actual"], sign=True) if i["actual"] is not None else ""
        when_txt = fmt_day(i["date"]) if i["date"] else f'due {fmt_day(i["due"])}'
        hint = (f'<div class="small muted">Standing order text: {esc(i["text"])}</div>'
                if i["text"] and i["status"] != "done" else "")
        in_cls = ' in' if i["expected"] > 0 else ''
        out.append(f'<tr><td>{esc(i["person"])}</td><td>{esc(i["label"])}{hint}</td>'
                   f'<td class="n{in_cls}">{planned}</td><td class="n{in_cls}">{actual}</td><td class="d">{when_txt}</td>'
                   f'<td><span class="pill {cls}">{label}</span></td></tr>')
    out.append("</tbody></table></div>")

    # Pots
    out.append("<h2>Shared pots</h2>")
    out.append('<div class="grid">')
    for pot in r["pots"]:
        rows = [f'<div class="card"><h3>{esc(pot["label"])}</h3>']
        for part in pot["parts"]:
            rows.append(f'<div class="row small"><span>{esc(part["who"])}</span>'
                        f'<span>{kr(part["spent"])} of {kr(part["budget"])}</span></div>{bar(part["spent"], part["budget"])}')
        rows.append("</div>")
        out.append("".join(rows))
    out.append("</div>")

    # Recent fun spending
    out.append("<h2>Fun spending this month</h2>")
    out.append('<div class="grid">')
    for p in r["people"]:
        rows = [f'<div class="table-wrap"><table><thead><tr><th colspan="3">{esc(p["name"])}</th></tr></thead><tbody>']
        if not p["recent"]:
            rows.append('<tr><td class="muted" colspan="3">Nothing yet.</td></tr>')
        for t in p["recent"]:
            pend = ' <span class="pill muted">pending</span>' if t["pending"] else ""
            in_cls = ' in' if t["amount"] > 0 else ''
            rows.append(f'<tr><td class="small muted d">{fmt_day(t["date"])}</td>'
                        f'<td>{esc(t["text"])}{pend}<div class="small muted">{esc(t["category"])}</div></td>'
                        f'<td class="n{in_cls}">{kr(t["amount"], sign=True)}</td></tr>')
        rows.append("</tbody></table></div>")
        out.append("".join(rows))
    out.append("</div>")
    out.append('<p class="small muted" style="margin-top:24px">Read-only. This page never moves money. '
               'Amounts in the account currency. Edit the plan in budget.json and reload.</p>')
    out.append("</main>")
    return "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">" \
           "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">" + out[0] + out[1] + \
           "</head><body>" + "".join(out[2:]) + "</body></html>"


def error_page(msg: str) -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Budget</title><style>{CSS}</style></head>'
            f'<body><main><h1>Budget</h1><p class="banner">{esc(msg)}</p></main></body></html>')


class Handler(BaseHTTPRequestHandler):
    allowed_hosts: set = set()

    def log_message(self, *_):  # keep the terminal quiet; nothing sensitive is logged
        pass

    def _send(self, code: int, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        if self.headers.get("Host", "") not in self.allowed_hosts:  # blocks DNS-rebinding from other sites
            return self._send(403, error_page("Forbidden"))
        u = urlparse(self.path)
        if u.path != "/":
            return self._send(404, error_page("Not found"))
        month = (parse_qs(u.query).get("month") or [None])[0]
        try:
            return self._send(200, render(budget.compute(month)))
        except FileNotFoundError as e:
            return self._send(500, error_page(str(e)))
        except ValueError as e:
            return self._send(400, error_page(str(e)))
        except Exception as e:  # show the type only; never echo data
            return self._send(500, error_page(f"Couldn't build the page ({type(e).__name__}). Check budget.json."))


def main() -> None:
    ap = argparse.ArgumentParser(description="Local read-only budget page")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true", help="don't open the browser")
    a = ap.parse_args()
    Handler.allowed_hosts = {f"127.0.0.1:{a.port}", f"localhost:{a.port}"}
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    url = f"http://127.0.0.1:{a.port}/"
    print(f"Budget page: {url}  (Ctrl+C to stop)")
    if not a.no_open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        srv.server_close()
    sys.exit(0)


if __name__ == "__main__":
    main()
