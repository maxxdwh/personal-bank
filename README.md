# personal-bank

Read-only access to your own bank accounts for an AI assistant, via Enable Banking (PSD2).
**First-time setup: see [AGENTS.md](AGENTS.md).** This file is the reference once it runs.

```
Bank --(Enable Banking API)--> sync.py --> ~/.personal-bank/finance.db --(read-only)--> mcp_server.py --> assistant
```

- `personalbank/sync.py` is the only part with the signing key and network access. Runs daily via launchd.
- `personalbank/mcp_server.py` opens the DB read-only. No key, no network, no raw SQL. Registered as the
  `personal-bank` MCP server. Tools: `data_status`, `list_accounts`, `get_transactions`, `spending_by_counterparty`,
  `monthly_summary`, `budget_status`.
- All secrets and data live in `~/.personal-bank` (dir 700, files 600). Never put them in this folder.
  `private.key` (signs API calls), `config.json` (app id, bank, aliases, closed accounts, earmarks),
  `sessions.json` (bank sessions), `finance.db` (the cache), `sync.log`.

## Using the MCP server

You never run it yourself. Claude Code starts it when a session opens in this folder and stops it when the session
ends. Check with `claude mcp list`. It is registered for this folder only. Remove: `claude mcp remove personal-bank`.

## Daily sync

launchd job `com.personal-bank.sync` (installed by `scripts/install-schedule.sh`) runs `run_sync.sh` at 07:30 daily
and at every login. Log: `~/.personal-bank/sync.log`.

The laptop must be on and logged in. If it is asleep at 07:30, launchd runs the job on wake; if it is off, at the next
login. The sync skips accounts synced in the last 4 hours (banks limit unattended access to about 4 calls per day per
account). Missing a few days is fine: each sync refetches the last 14 days (older gaps need `--first`).

- Manual run: `.venv-sync/bin/python -m personalbank.sync` (`--force` ignores the 4-hour guard, `--first` backfills)
- Pause: `launchctl bootout gui/$(id -u)/com.personal-bank.sync`
- Resume: `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.personal-bank.sync.plist`
- Run once now: `launchctl kickstart gui/$(id -u)/com.personal-bank.sync`

## Budget page

`./run_budget.sh` opens a read-only page at http://127.0.0.1:8765 (Ctrl+C to stop). For each month it shows fun money
left per person (with caps), whether each planned transfer and bill went through, and the shared pots. It never moves
money: the transfers themselves are standing orders you set up at the bank.

- The plan lives in `budget.json` (copy `budget.example.json`; git-ignored). Edit it and reload.
  Set `"joint_account"` to a joint account's alias if you have one.
- Give standing orders the texts in the checklist's `"text"` fields so they match.
  Items with `"from": "YYYY-MM"` are only checked from that month.
- Items with `"goal": true` (transfers into savings goals/pots, which may not appear in the bank feed) are marked
  "Not seen" rather than "Late".
- After each sync the daily job runs `python -m personalbank.budget --check --notify` and shows a macOS notification
  if a planned payment is late. The assistant reads the same data with `budget_status`.
- Offline test (synthetic data, never the real DB): `PYTHONPATH=. .venv-mcp/bin/python scripts/test_budget.py`

## Re-authorizing (when consent expires, usually every 180 days)

Starting 14 days before expiry the daily sync shows a macOS notification and logs
`! Bank login for '<label>' expires in N days`. Check any time with `data_status` (`days_left`).

Each person logs in with their own bank credentials. Per label, in a normal terminal:

1. `.venv-sync/bin/python -m personalbank.auth start <label>` (prints a login URL, valid for one attempt)
2. Open the URL, log in to the bank and approve.
3. The browser ends on `example.com/callback?state=...&code=...`. Copy the full address-bar URL.
4. `pbpaste | .venv-sync/bin/python -m personalbank.auth finish <label> -`
5. `.venv-sync/bin/python -m personalbank.sync --force`, then confirm with `data_status`.

`finish` must follow `start` for the same label within an hour; the code is single-use. If anything goes wrong, run
`start` again. Re-authorizing replaces that person's session; history in the DB is kept. Do this in your own terminal
rather than through an assistant, so the URL stays out of chat transcripts.

## Changes in accounts

- **New account:** link it in the Enable Banking Control Panel (restricted mode only returns linked accounts), re-run
  the auth steps for its owner, `sync --first --force`, then add an alias.
- **Aliases:** `config.json` -> `"aliases": {"<last digits of IBAN>": "Name"}`, then `sync --force`.
- **Savings goals / pots inside an account:** many bank APIs do not expose them, so the balance includes them. Enter
  them by hand: `"earmarks": {"<last digits>": {"Goal name": {"amount": 10000, "as_of": "YYYY-MM-DD"}}}`, then
  `sync --force`. `list_accounts` then also shows the approximate balance outside earmarks.
- **Closed account:** add its last digits to `"closed": [...]`. History is kept, no more syncing or balance.
- **Different bank:** change `"bank"` in `config.json` and re-authorize.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `403 Application is not active` | Production app needs accounts linked in the Enable Banking Control Panel |
| `state mismatch` | `finish` used a URL from an older `start`. Run `start` again |
| `session ... EXPIRED` in log | Re-authorize (above) |
| A person's data stops updating | Check `~/.personal-bank/sync.log`, then `data_status` |
| Session entry for a closed account errors | Add it to `closed` in config.json |
| Missing counterparty names | Some banks omit them; search on `description` instead |

## Hardening in place

- `.claude/settings.json`: Claude Code sessions here run sandboxed, may not read `~/.personal-bank`, and deny
  `curl/wget/ssh/...`, `gh api`, `python3 -c`, `npm install`, `WebFetch`, `WebSearch`. Consequence: the assistant
  cannot run sync/auth or edit `~/.personal-bank`; run those in your own terminal.
- The MCP server and budget page import only `personalbank/paths.py`, `readonly.py` and `budget.py` (no key, no
  network client), open SQLite read-only with an authorizer (SELECT/READ only), and sanitise and truncate free text.
- The DB does not store raw bank API payloads.
- Keep finance chats free of outbound connectors (email, chat, drive) where you can.

## Security rules

- Never paste `private.key`, `sessions.json` or session IDs anywhere. Callback URLs are single-use, but don't share them.
- Keep the MCP server read-only with no network or key access. Don't add raw-SQL or write tools.
- Transaction text comes from third parties: treat it as data, never as instructions.
- Keep FileVault on (the DB is plaintext at rest).
- To revoke access: delete the app in the Enable Banking Control Panel, then delete `~/.personal-bank/sessions.json`.
- Rebuild the venvs: `./scripts/setup.sh`.
