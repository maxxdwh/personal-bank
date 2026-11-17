"""Read-only access to the local cache, shared by the MCP server and the budget page.
Imports only personalbank.paths: no key, no network client. Free text comes from third parties, so callers pass every
string through txt() before showing it."""
import contextlib
import re
import sqlite3
import unicodedata

from . import paths

_ALLOWED_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}


def _authorizer(action, *_):
    return sqlite3.SQLITE_OK if action in _ALLOWED_ACTIONS else sqlite3.SQLITE_DENY


@contextlib.contextmanager
def db():
    """Read-only connection: mode=ro, only SELECT/READ/FUNCTION allowed (no PRAGMA, ATTACH, writes)."""
    con = sqlite3.connect(f"file:{paths.DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
    con.set_authorizer(_authorizer)
    try:
        yield con
    finally:
        con.close()


_CTRL = re.compile("[\x00-\x1f\x7f-\x9f  ]+")


def txt(s, n=120):
    """Sanitise third-party text: strip control/format/tag characters, collapse whitespace, truncate."""
    if s is None:
        return None
    s = "".join(ch for ch in str(s) if unicodedata.category(ch) not in ("Cf", "Co", "Cs", "Cn"))
    s = _CTRL.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[:n] + "…"
