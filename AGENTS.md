# Coldlocker — Agent Guide

1. **Install:** `git clone https://github.com/coldstack-dev/coldlocker && cd coldlocker` (Python 3.11+; Windows: 3.12.4+ with `py -3`).
2. **Verify (read-only):** `python3 hub.py doctor --json` (check `exit_code: 0`, `failed_checks: []`, `expired_oauth: []`, `oauth_expiry_details_available: true`; plain `doctor` prints `PASS`/`SKIP`, no `FAIL`) and, when hooks are installed, `python3 hub.py guard-audit --json` (`status`: `PASS`/`FAIL`/`INCOMPLETE`).
3. **First read-only command:** `python3 hub.py scan --json` (reads local agent configs and `crontab -l`, writes nothing; exits `0` clean, `1` review/conflict findings, `2` incomplete; plain `hub.py scan` exits `0` even with findings) and `python3 hub.py check` for repo checks. Use [docs/IMPORT-PROMPT.md](docs/IMPORT-PROMPT.md) for guided setup.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success / `PASS` (note: plain `scan` also exits `0` when reporting findings) |
| `1` | Findings (`scan --json`), failed checks/expired OAuth (`doctor`), drift (`guard-audit`), or refusal |
| `2` | Incomplete check (`INCOMPLETE` / `incomplete_inventory`), unavailable diagnostics, or invalid arguments |

## Approval gate (mandatory)

Never run any mutating command or flag without explicit user approval:
- `scan --import`, `import-*`, `import-executor --apply`, `remove`, `pack add|remove|install|uninstall`
- `connect --write`, `disconnect --apply`, `connect-composio` (local `--name`/`--url` or `--catalog --write`), `link` (without `--dry-run`), `routine enable|disable --write`
- `publish-remote` (without `--dry-run`), `agents-import|export|sync`, `update`, `sync-lessons`, `deliver-rules` (without `--dry-run`)
- `gateway.py install|prepare|start|stop|refresh|sync|token`, `gateway.py register-executor --apply`, `store.py set|delete`, `hooks/install.py --write`
- Never pass `--apply`, `--yes`, `--write`, or `--approve-risky` without the user's explicit go-ahead.
- Never read, print, or ask the user to paste secret values; use `secret://<name>` references only.
- If `MAINTAINING.md` exists in this checkout, read it before starting work.
