#!/bin/zsh
# First-time setup. Run in a normal terminal (NOT through an AI agent: it needs network access and ~/.personal-bank).
#   ./scripts/setup.sh
# Safe to re-run: it rebuilds the virtualenvs and never overwrites an existing config or key.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PYTHON:-python3}
DATA="$HOME/.personal-bank"

$PY -c 'import sys; sys.exit(sys.version_info < (3, 10))' || { echo "Python 3.10+ is required (set PYTHON=/path/to/python3)"; exit 1; }

echo "1/4 two virtualenvs (sync = key + network; mcp = read-only server)"
rm -rf .venv-sync .venv-mcp
$PY -m venv .venv-sync && .venv-sync/bin/pip install -q --upgrade pip && .venv-sync/bin/pip install -q -r requirements-sync.txt
$PY -m venv .venv-mcp  && .venv-mcp/bin/pip install -q --upgrade pip && .venv-mcp/bin/pip install -q -r requirements-mcp.txt

echo "2/4 private data folder $DATA (owner-only)"
mkdir -p "$DATA" && chmod 700 "$DATA" && chmod 700 "$PWD"
if [[ ! -f "$DATA/config.json" ]]; then
  (umask 077; cat > "$DATA/config.json" <<'JSON'
{
  "app_id": "PASTE-YOUR-ENABLE-BANKING-APPLICATION-ID",
  "bank": {"name": "YOUR BANK NAME", "country": "XX"},
  "consent_days": 180,
  "aliases": {},
  "closed": [],
  "earmarks": {}
}
JSON
  )
  echo "   wrote a template config.json: fill in app_id and bank"
fi
[[ -f "$DATA/private.key" ]] && chmod 600 "$DATA/private.key" || echo "   no private.key yet: see AGENTS.md, step 2"

echo "3/4 check that the read-only server loads without key/network code"
PYTHONPATH="$PWD" .venv-mcp/bin/python -c 'import sys; from personalbank import mcp_server; assert "httpx" not in sys.modules and "jwt" not in sys.modules; print("   ok")'

echo "4/4 register the MCP server with Claude Code (this project folder only)"
if command -v claude >/dev/null; then
  claude mcp remove personal-bank 2>/dev/null || true
  claude mcp add personal-bank -e PYTHONPATH="$PWD" -- "$PWD/.venv-mcp/bin/python" -m personalbank.mcp_server
else
  echo "   'claude' not found. For another MCP client, register this command:"
  echo "   PYTHONPATH=$PWD $PWD/.venv-mcp/bin/python -m personalbank.mcp_server"
fi
echo "done. Next: AGENTS.md, step 2 (Enable Banking app)."
