"""Monthly budget view over the read-only cache.

For one calendar month it answers: did the planned transfers and bills go through, how much fun money does each
person have left, and how are the shared pots doing. The plan (amounts, caps, matching rules) lives in budget.json in
the project folder. Read-only: this module never moves money. It imports only personalbank.readonly and personalbank.paths.

Usage: python -m personalbank.budget [--month YYYY-MM] [--check] [--notify]"""
import argparse
import calendar
import json
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone

from . import paths
from .readonly import db, txt

GRACE_DAYS = 3        # a transfer counts as late this many days after its due date
MATCH_WINDOW = 5      # look this many days either side of the due date for the matching transaction
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
OTHER = {"key": "other", "label": "Other", "kind": "fun"}


def load_plan(path=None) -> dict:
    p = path or paths.BUDGET_PATH
    if not p.exists():
        raise FileNotFoundError(f"No budget plan found at {p}")
    return json.loads(p.read_text())


def parse_month(month: str | None, today: date) -> tuple[date, date]:
    if month is None:
        y, m = today.year, today.month
    else:
        if not MONTH_RE.match(month):
            raise ValueError("month must be YYYY-MM")
        y, m = int(month[:4]), int(month[5:])
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def _shift(first: date, months: int) -> str:
    y, m = divmod(first.year * 12 + first.month - 1 + months, 12)
    return f"{y:04d}-{m + 1:02d}"


def due_date(day, first: date, last: date) -> date:
    if day == "last_working_day":
        d = last
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        return d
    return first.replace(day=min(int(day), last.day))


class Classifier:
    """First matching category wins. Patterns are case-insensitive regexes tried against counterparty and
    description separately, so ^ anchors to the start of either field."""

    def __init__(self, categories: list[dict]):
        self.rules = [(c, [re.compile(p, re.IGNORECASE) for p in c.get("patterns", [])]) for c in categories]
        self.labels = {c["key"]: c.get("label", c["key"]) for c in categories} | {"other": "Other"}

    def __call__(self, t: dict) -> dict:
        fields = (t["counterparty"] or "", t["description"] or "")
        for c, pats in self.rules:
            sign = c.get("sign", "any")
            if (sign == "in" and t["amount"] <= 0) or (sign == "out" and t["amount"] >= 0):
                continue
            if any(p.search(f) for p in pats for f in fields):
                return c
        return OTHER


def _account_keys(c) -> dict[str, set[str]]:
    keys: dict[str, set[str]] = {}
    for r in c.execute("SELECT key, alias, name FROM accounts"):
        for label in (r["alias"], r["name"]):
            if label:
                keys.setdefault(label.lower(), set()).add(r["key"])
    return keys


def _rows(c, keys, lo: date, hi: date) -> list[dict]:
    if not keys:
        return []
    keys = sorted(keys)
    q = ("SELECT id, account_key, booking_date, amount, counterparty, description, status FROM transactions "
         f"WHERE account_key IN ({','.join('?' * len(keys))}) AND booking_date BETWEEN ? AND ? "
         "ORDER BY booking_date DESC, id")
    return [dict(r) for r in c.execute(q, [*keys, lo.isoformat(), hi.isoformat()])]


def _label(t: dict) -> str:
    return txt(t["counterparty"] or t["description"] or "(no description)", 60)


def _find(item: dict, rows: list[dict], used: set, lo: str, hi: str) -> dict | None:
    """Best transaction for a checklist item: text pattern + amount within tolerance, else an exact amount (+-1)."""
    exp = float(item["amount"])
    tol = float(item.get("tolerance", 1))
    pat = re.compile(item["match"], re.IGNORECASE) if item.get("match") else None
    cands = [t for t in rows if t["id"] not in used and t["booking_date"] and lo <= t["booking_date"] <= hi
             and (t["amount"] > 0) == (exp > 0)]
    if pat:
        strong = [t for t in cands if abs(t["amount"] - exp) <= tol
                  and any(pat.search(f or "") for f in (t["counterparty"], t["description"]))]
        if strong:
            return min(strong, key=lambda t: abs(t["amount"] - exp))
    exact = [t for t in cands if abs(t["amount"] - exp) <= 1]
    return exact[0] if exact else None


def compute(month: str | None = None, today: date | None = None, plan: dict | None = None) -> dict:
    today = today or date.today()
    plan = plan if plan is not None else load_plan()
    first, last = parse_month(month, today)
    lo_d, hi_d = first.isoformat(), last.isoformat()
    current, past = first <= today <= last, today > last
    classify = Classifier(plan.get("categories", []))
    people_plan = plan.get("people", [])

    with db() as c:
        keys = _account_keys(c)
        last_sync = c.execute("SELECT MAX(synced_at) FROM accounts").fetchone()[0]
        sessions = [dict(r) for r in c.execute("SELECT label, valid_until FROM sessions_meta ORDER BY label")]
        window = (first - timedelta(days=MATCH_WINDOW + 2), last + timedelta(days=MATCH_WINDOW + 2))
        rows = {p["name"]: _rows(c, keys.get(p["account"].lower(), set()), *window) for p in people_plan}
        missing_accounts = [p["account"] for p in people_plan if p["account"].lower() not in keys]
        joint_name = plan.get("joint_account")
        joint_rows = _rows(c, keys.get(joint_name.lower(), set()), first, last) if joint_name else []
        if joint_name and joint_name.lower() not in keys:
            missing_accounts.append(joint_name)

    now = datetime.now(timezone.utc)
    sess = [{"label": txt(s["label"], 40), "days_left": (datetime.fromisoformat(s["valid_until"]) - now).days}
            for s in sessions]

    # 1. Checklist: planned transfers and bills
    used: set = set()
    checklist = []
    for item in plan.get("checklist", []):
        if item.get("from") and first.strftime("%Y-%m") < item["from"]:
            continue  # not part of the plan yet in this month
        due = due_date(item.get("day", 1), first, last)
        lo = (due - timedelta(days=MATCH_WINDOW)).isoformat()
        hi = (due + timedelta(days=MATCH_WINDOW)).isoformat()
        hit = _find(item, rows.get(item["person"], []), used, lo, hi)
        if hit:
            used.add(hit["id"])
            status = "done"
        elif not past and (not current or today <= due + timedelta(days=GRACE_DAYS)):
            status = "due"
        else:
            status = "not_seen" if item.get("goal") else "late"
        checklist.append({
            "person": item["person"], "label": item["label"], "expected": item["amount"],
            "actual": hit["amount"] if hit else None, "date": hit["booking_date"] if hit else None,
            "due": due.isoformat(), "status": status, "goal": bool(item.get("goal")),
            "variable": float(item.get("tolerance", 1)) > 1, "text": item.get("text"),
        })

    # 2. Per person: fun money and pots
    pot_spend: dict[str, dict[str, float]] = {}
    people = []
    for p in people_plan:
        by_cat: dict[str, float] = {}
        recent = []
        for t in rows[p["name"]]:
            if not (lo_d <= (t["booking_date"] or "") <= hi_d) or t["id"] in used:
                continue
            cat = classify(t)
            if cat["kind"] == "fun":
                by_cat[cat["key"]] = by_cat.get(cat["key"], 0.0) - t["amount"]
                recent.append({"date": t["booking_date"], "text": _label(t), "category": classify.labels[cat["key"]],
                               "amount": t["amount"], "pending": t["status"] == "PDNG"})
            elif cat["kind"] == "pot":
                pot_spend.setdefault(cat["key"], {}).setdefault(p["name"], 0.0)
                pot_spend[cat["key"]][p["name"]] -= t["amount"]
        spent = round(sum(by_cat.values()), 2)
        left = round(p["fun_money"] - spent, 2)
        days_left = (last - today).days + 1 if current else (0 if past else (last - first).days + 1)
        order = [k for k in [*p.get("caps", {}), "subscriptions", "other"]] + sorted(by_cat)
        seen, by_list = set(), []
        for k in order:
            if k in by_cat and k not in seen:
                seen.add(k)
                by_list.append({"key": k, "label": classify.labels.get(k, k), "amount": round(by_cat[k], 2)})
        people.append({
            "name": p["name"], "account": p["account"], "fun_money": p["fun_money"], "spent": spent, "left": left,
            "days_left": days_left, "per_day": round(left / days_left, 2) if days_left else None,
            "by_category": by_list,
            "caps": [{"key": k, "label": classify.labels.get(k, k), "cap": v, "spent": round(by_cat.get(k, 0.0), 2)}
                     for k, v in p.get("caps", {}).items()],
            "recent": recent[:25],
        })

    for t in joint_rows:
        cat = classify(t)
        if t["amount"] < 0 and cat["kind"] not in ("skip", "income"):
            pot_spend.setdefault("joint", {}).setdefault("Joint account", 0.0)
            pot_spend["joint"]["Joint account"] -= t["amount"]

    pots = []
    for key, cfg in plan.get("pots", {}).items():
        spent_by = pot_spend.get(key, {})
        budget = cfg["budget"]
        if isinstance(budget, dict):
            parts = [{"who": who, "budget": b, "spent": round(spent_by.get(who, 0.0), 2)} for who, b in budget.items()]
        else:
            parts = [{"who": "Together", "budget": budget, "spent": round(sum(spent_by.values()), 2)}]
        pots.append({"key": key, "label": cfg.get("label", key), "parts": parts})

    return {
        "month": first.strftime("%Y-%m"), "month_label": first.strftime("%B %Y"), "today": today.isoformat(),
        "current": current, "past": past, "prev": _shift(first, -1), "next": _shift(first, 1),
        "last_sync": last_sync, "sessions": sess, "missing_accounts": missing_accounts,
        "checklist": checklist, "late": sum(1 for i in checklist if i["status"] == "late"),
        "people": people, "pots": pots,
    }


def late_items(today: date | None = None, plan: dict | None = None) -> list[str]:
    """Late checklist items for this month, plus last month during the first days of a new month."""
    today = today or date.today()
    plan = plan if plan is not None else load_plan()
    months = [None] + ([_shift(today.replace(day=1), -1)] if today.day <= GRACE_DAYS + 2 else [])
    out = []
    for m in months:
        r = compute(m, today, plan)
        out += [f"{i['person']}: {i['label']} ({r['month_label']}, due {i['due']})"
                for i in r["checklist"] if i["status"] == "late"]
    return out


def _notify(msg: str) -> None:
    subprocess.run(["osascript", "-e", "on run argv", "-e", 'display notification (item 1 of argv) with title "personal-bank budget"',
                    "-e", "end run", msg], capture_output=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="Read-only monthly budget check")
    ap.add_argument("--month", help="YYYY-MM (default: this month)")
    ap.add_argument("--check", action="store_true", help="list late transfers/bills; exit 1 if any")
    ap.add_argument("--notify", action="store_true", help="with --check: show a macOS notification if any are late")
    a = ap.parse_args()
    if a.check:
        late = late_items()
        for line in late:
            print(f"! late: {line}")
        if late and a.notify:
            _notify(f"{len(late)} planned payment(s) not seen yet: {late[0]}" + (" …" if len(late) > 1 else ""))
        sys.exit(1 if late else 0)
    r = compute(a.month)
    print(f"{r['month_label']}  (late items: {r['late']})")
    for p in r["people"]:
        print(f"  {p['name']}: fun money left {p['left']:,.0f} of {p['fun_money']:,.0f}")
    for i in r["checklist"]:
        print(f"  [{i['status']:>8}] {i['person']}: {i['label']} {i['expected']:,.0f}")


if __name__ == "__main__":
    main()
