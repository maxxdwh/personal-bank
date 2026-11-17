#!/bin/zsh
# Called by launchd. Timestamped, secret-free log lines in ~/.personal-bank/sync.log
cd "$(dirname "$0")" || exit 1
echo "--- $(date '+%Y-%m-%d %H:%M:%S')"
.venv-sync/bin/python -m personalbank.sync
echo "exit=$?"
# Budget check (read-only venv): notify if a planned transfer or bill is late. Never fails the job.
[[ -f budget.json ]] && PYTHONPATH="$PWD" .venv-mcp/bin/python -m personalbank.budget --check --notify || true
