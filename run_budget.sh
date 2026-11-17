#!/bin/zsh
# Opens the local, read-only budget page at http://127.0.0.1:8765 (Ctrl+C to stop).
# Uses the read-only venv: no signing key, no network access beyond localhost.
cd "$(dirname "$0")" || exit 1
PYTHONPATH="$PWD" exec .venv-mcp/bin/python -m personalbank.budget_ui "$@"
