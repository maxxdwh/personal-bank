"""Paths only. Safe for the read-only MCP server to import: no key access, no network client."""
from pathlib import Path

HOME = Path.home() / ".personal-bank"
KEY_PATH = HOME / "private.key"
CONFIG_PATH = HOME / "config.json"      # {"app_id", "aliases", "closed", "earmarks"}
SESSIONS_PATH = HOME / "sessions.json"  # {label: {session_id, valid_until, accounts}}
DB_PATH = HOME / "finance.db"
BUDGET_PATH = Path(__file__).resolve().parent.parent / "budget.json"  # the plan: amounts, caps, matching rules
