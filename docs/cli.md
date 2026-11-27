# Coldlocker CLI

Run `python3 hub.py` from the repository root. Python 3.11+ and the standard library are enough; verification also uses Git. Every command takes `--help`; `python3 hub.py --version` prints the version. Plain commands generally exit 0 on success, 1 on rejected input or failed checks, and 2 on invalid syntax.

Core commands: `scan`, `connect`, `disconnect`, `mcp-run`, `connect-composio`, `list`, `remove`, `pack`, `import-skill`, `import-rule`, `import-subagent`, `import-command`, `import-mcp`, `import-routine`, `import-plugin`, `import-connection`, `import-executor`, `link`, `routine`, `publish-remote`, `agents-import`, `agents-export`, `agents-sync`, `serve`, `check`, `doctor`, `update`.

[Policy pack commands](#policy-pack-commands): `sync-lessons`, `deliver-rules`, `model`, `guard-stats`, `guard-audit`.

On Windows, `python3` may be a Store stub; use `python` or `py -3`. File locks use `msvcrt`. The private store is owner-only on Windows only when Python 3.12.4+ creates it (`mkdir` mode `0o700` sets a protected ACL for SYSTEM, Administrators and the owner); older Pythons leave the parent folder's permissions, so use 3.12.4+. Starting the gateway on Windows is covered in [gateway.md](gateway.md).

## JSON output

`scan`, `list`, `doctor`, `connect`, and `pack install` accept `--json` and write a single object with
`schema_version: 1` to stdout. Read prompts and diagnostics on stderr.

- Exit 0 reports success.
- Exit 1 reports findings, attention, or a refusal.
- Exit 2 reports an incomplete result or an error, including invalid arguments.

`list --json` puts rows in `items`. Scan inventory and safety reports retain
their `version` field. Scan import reports include items held for review and
items whose import failed. Inventory limits can produce a partial report, so
check the exit code even when stdout contains results. Plain and JSON `doctor`
use the same exit code. An expired OAuth grant causes a nonzero exit.

## Scan and import

```sh
python3 hub.py scan
python3 hub.py scan --json
python3 hub.py scan --import
python3 hub.py scan --import --only sc-0123456789ab --yes
python3 hub.py scan --import --update --yes
python3 hub.py scan --safety
python3 hub.py scan --import --approve-risky sc-0123456789ab --yes
```

`scan` is a read-only inventory grouped by agent. `--import` confirms and imports all eligible items; non-interactive runs need `--yes`. `--only`, `--yes` and `--update` require `--import`. `--update` re-imports items that changed in the agent they were scanned from; see [scan.md](scan.md#update). `--approve-risky ID...` (requires `--import`) imports flagged items, never one with an embedded secret. `--safety` prints the [safety report](safety.md) and cannot combine with import options. Discovery flags (`--project`, `--projects-root`, `--home`, `--agent`, `--platform`, `--all`) and statuses are in [scan.md](scan.md). Plain scan exits 1 on invalid input.

`import-plugin <metadata.json>` and `import-connection <metadata.json>` accept strictly validated metadata-only records with `--owner` and `--origin` (see [formats](formats.md)). They never install code, authenticate or contact endpoints.

## Import a skill

```sh
python3 hub.py import-skill .scratch/incoming/example --owner coldlocker --origin upstream
```

The package needs `SKILL.md` with closed `---` frontmatter holding a `name` and nonempty `description`. The name must match the source directory or `--name`: lowercase letters, digits and hyphens, starting with a letter or digit, at most 63 characters. Symlinks anywhere in the package are rejected. Every regular file to copy is checked for secrets and private text, including directories scan skips, such as `node_modules`. The package is copied, minus `.git`, to `.hub-store/catalog/skills/<name>/`.

Reimporting with the same owner and origin replaces the whole package after verifying its stored hash. The previous copy is kept until publication succeeds and restored on failure. An unchanged indexed destination is a no-op.

### From GitHub

```sh
python3 hub.py import-skill https://github.com/<owner>/<repo>[/path][@ref]
```

Fetches one commit (`git fetch --depth 1`) into a temporary directory, then imports the folder at `path` (default: the repository root) with the same checks and safety scan as a local import. `ref` is a branch, tag or commit (default: the default branch). The default name is the folder name (or repository name), and the default origin is `github`. The URL, path, ref and fetched commit SHA are recorded in `.hub-store/git-sources.json` under the skill name and printed. Re-run with a new `@ref` to update the skill; the previous copy goes to `.hub-store/history/`.

## Import a rule

```sh
python3 hub.py import-rule .scratch/incoming/policy.md --name team-policy --origin authored
```

Copies a Markdown file to `.hub-store/catalog/rules/<name>.md`. It applies scan's private-text validation: your own home path prefix becomes `~`, and any other finding (IP, hostname, email, secret, another user's home path) refuses the import with `REFUSED: definition contains <category> at line <n> (<file>)` and writes nothing. The default name is the lowercased source filename without `.md`; an invalid name asks for `--name`.

`import-skill`, `import-rule`, `import-subagent` and `import-command` take `--name`, `--owner` (default `coldlocker`) and `--origin` (default `local-import`, or `github` for a skill imported from a GitHub URL). Owner and origin are short labels, never paths. Each import records a content SHA-256 and an opaque `src-` id (first 12 hex characters). Hashing and manifest layout follow [formats.md](formats.md).

## Import MCP metadata

```sh
python3 hub.py import-mcp .scratch/incoming/example-server.json --origin upstream
```

Records a nonsecret stdio or HTTP definition ([formats](formats.md)) that the gateway and listing endpoint can serve ([gateway](gateway.md)). It never starts or contacts the server or resolves a secret. Sensitive env and header keys need `secret://<name>` references; secret-looking values, private paths or names and unknown fields are rejected. The result goes to `.hub-store/catalog/mcp/servers/<name>.json` with a manifest entry. Set secret values separately in the local store. Supports `--owner` and `--origin`.

Reimports keep existing source-path bindings while the portable command and args still match. Changing a bound field clears that server's bindings and unused private references. An orphan binding without a manifest entry is refused; reconcile that local metadata before reusing the name.

## Import routine metadata

```sh
python3 hub.py import-routine .scratch/incoming/routine.json --origin authored
```

Records a definition only; it never schedules, enables or runs anything. Input needs one owner (a different `--owner` is rejected), one preferred runner from [routines.md](routines.md), a valid schedule (`interval`, `fixed_time`, `cron`, `webhook`), an `overlap` of `skip`, `queue` or `allow`, and `enabled: false`. The definition goes to `.hub-store/catalog/routines/<id>/routine.json`. `prompt` must name an existing ordinary sibling file. It is validated and copied beside the definition; a missing prompt is rejected before the catalog changes.

The manifest hash covers `routine.json` only. Prompt files are not hash-indexed, so a same-owner, same-origin update may replace the prompt, including local edits; save those first. A new prompt name cannot overwrite an unrelated sibling, and a new routine cannot adopt an existing routine directory. Renaming a prompt keeps the old file. Definition and prompt bytes are restored on write failure.

## Safety rules for imports

All import commands refuse shipped names, unindexed destinations, owner or origin changes, inconsistent provenance, missing indexed content, symlinks, nonordinary destinations, and content that differs from its recorded hash. A stored skill containing `.git` content is refused because those bytes are outside the v1 hash; empty skill directories are also outside it and may vanish on update. Conflicting files are never adopted: preserve them separately before retrying.

Same-owner, same-origin updates are allowed when the stored copy matches its manifest. Content, manifest and explicit MCP path bindings are prepared before publication with rollback copies; a failed write restores the previous bytes and private references, and a failed new import removes its partial content. If restoration fails, the command names the `.import-*` directories holding recovery copies. Snapshots are rechecked before each replacement to refuse intervening changes. Imports take no locks and have no crash recovery, so avoid concurrent catalog writes.

## List, update and remove

```sh
python3 hub.py list [--kind skills|rules|mcp_servers|routines|plugins|connections] [--json]
python3 hub.py remove skills example --yes
```

`list` shows listed shipped items (see [skill packs](#skill-packs)) and imported items with kind, name, owner, origin and shipped/imported. `remove <kind> <name>` deletes an imported item's files and local manifest entry with the same staged, rollback-safe publish as import. It refuses shipped items and items owned by another owner (pass that `--owner`), and needs `--yes` or an interactive confirmation. Links made by `hub.py link` that would dangle are listed; link-target files are never deleted.

Reimporting a changed source with the same owner and origin updates the item in place (the command `scan` prints for a changed item). Replaced and removed content is kept under `.hub-store/history/<kind>/<name>/<UTC timestamp>/`. A different owner or origin is refused and the message names the values to pass.

Run `python3 hub.py history <kind> <name>` to preview retention for that item. A changed import or removal saves the current version before publishing and keeps the newest existing copy. After publication it prunes older timestamped copies, leaving two at most per item. If the filesystem runs out of space while saving, Coldlocker prunes the older copies shown in the preview and retries once. If the retry fails, the live catalog and its immediately preceding copy stay intact.

## Skill packs

```sh
python3 hub.py pack list
python3 hub.py pack add pstack
python3 hub.py pack remove pstack
```

Shipped skills other than `hub-discover` come in opt-in packs defined in `packs.json`; today there is one, `pstack`, with the other 57. Without the optional policy pack, `pstack` holds only the 6 that need no pack files; the other 51 come with the pack. A new install lists only `hub-discover` and your imports, through the MCP tools, `list`, `link`, the UI and `publish-remote`. `pack add pstack` lists the rest again. An install that had imports, links or catalog calls before packs existed keeps every pack on. `pack remove` only stops listing: skill files stay in `skills/`, and links made by `hub.py link` are named and left in place. See [formats](formats.md#packsjson).

### Install a pack from GitHub

```sh
python3 hub.py pack install https://github.com/<owner>/<repo>@<40-character commit> --yes --json
python3 hub.py pack uninstall <name> --yes
```

`pack install` asks for confirmation before writing. Outside a terminal it
requires `--yes`. Both formats exit 1 if any entries are held, even when some
entries install successfully. JSON includes one row per pack path in
`entries` with its `kind`, `status` (`installed` or `held`), and either its
installed name and hash or the reason it was held.

`pack install` fetches the repository at that exact commit and reads `coldlocker-pack.json` at its root (see [formats](formats.md#coldlocker-packjson)). A branch, tag, short SHA or missing ref is refused, so the pack can't change under its pins. An entry path that is absolute, contains `..`, `.` or an empty part, or passes through a symlink refuses the whole pack before anything is imported. Each entry whose sha256 matches its pin goes through the same safety scan and staged import as `import-skill`, `import-rule` or `import-mcp`, with origin `pack-<name>`. Unpinned and mismatched entries, and entries the scan refuses, are held: they are not imported and are listed in the output and in `.hub-store/installed-packs.json`. A name that already exists in the catalog refuses the whole pack, and every entry is screened against the catalog as it was before the install. The pack record is written before the first import and lists each item it creates, with its hash. If any import fails, those items are removed again; if that rollback cannot finish, the record stays so `pack uninstall` can finish it. To import a reviewed flagged entry, uninstall and install again with `--approve-risky <path>`. `pack uninstall` removes only the recorded items whose hash and origin still match, keeping copies under `.hub-store/history/`, and names any it leaves in place.

## Link skills

```sh
python3 hub.py link --target .scratch/test-root --dry-run
python3 hub.py link --target .scratch/test-root
```

Creates relative symlinks under the target's `.claude/skills/` and `.agents/skills/` for every listed skill: `hub-discover`, the skills of packs that are on, and skills imported into `.hub-store/catalog/`. All destinations are checked before any write. Refused: conflicting files, directories or links; symlinked parent directories below your home or temp directory (system links such as macOS `/var` above them are fine); the home directory itself; targets under home that are not inside a project containing `.git`; and targets inside home agent config trees. Correct existing links are kept, and a second run prints `Nothing to do`. `--dry-run` prints proposed links, one line each.

To link into your own home skill folders, opt in with `--home`:

```sh
python3 hub.py link --home --dry-run
python3 hub.py link --home --yes
```

`--home` links every skill into `~/.claude/skills` and `~/.agents/skills`. It confirms at a terminal and needs `--yes` otherwise. Unfamiliar files, directories and links there are reported as `Conflict` and skipped while the rest link. Created links are recorded in `.hub-store/links.json` so `remove` can warn about dangling ones.

## Connect clients

```sh
python3 hub.py connect
python3 hub.py connect --agent claude codex
python3 hub.py connect --write --yes
python3 hub.py connect --agent claude codex --check-skill <name> --json
python3 hub.py connect --write --retire-direct
python3 hub.py connect --remote [--agent grok chatgpt ...] [--url https://catalog.example.org/mcp]
```

`connect` prints settings for detected clients and writes nothing. `--write` is the one opt-in exception: after `--yes` or interactive confirmation it backs up and merges the Coldlocker registrations into detected user configs, retaining existing private aliases for permission and approval compatibility. It keeps other settings and rejects symlinked or non-ordinary destinations. `--retire-direct` (with `--write`, running gateway only) also lists and removes user-level direct entries the gateway now serves, in the same backed-up write ([clients.md](clients.md#connect-detected-agents)). While the gateway is stopped, `connect` names imported MCP servers and prints the command that starts it; on Windows, `--write` instead adds each imported stdio server as a `mcp-run <name>` entry, which resolves its `secret://` references at launch ([clients.md](clients.md#imported-mcp-servers)). `--remote` prints OAuth settings for hosted clients independent of local detection, never reads or writes client settings, and refuses `--write`. Supported clients and the hosted-bot status are in [clients.md](clients.md) and [remote.md](remote.md#connect-a-client).

`connect --json` keys results by client in `clients`. Local rows report the
settings path and outcome, with a backup path and the Undo command for a
successful write where applicable. Remote rows report setup instructions.
`--check-skill` uses the selected clients' saved local stdio registrations to
read the named skill's SKILL.md. It reports each read and transport, and exits 1
if a selected registration or read fails.

Run the `disconnect` command printed by `connect --write` to preview undo.
Append `--apply` to confirm. See [Write safety](clients.md#write-safety).

Remote catalog entries use `public_url` in `.hub-store/remote.json` or the
explicit `--url` override. `routine enable --remote` and `connect-composio
--catalog` use the same setting/override. Missing configuration gives setup
instructions; no owner endpoint is assumed. `--url` is rejected for local
`connect` and nonremote routine actions.

## Composio

```sh
python3 hub.py connect-composio --catalog
python3 hub.py connect-composio --catalog --write --yes --api-key-ref secret://composio-key
python3 hub.py connect-composio --url <MCP_URL> --name <NAME>
```

`--catalog` prints the remote-catalog OAuth registration and next steps, so locker skills are reachable wherever Composio reaches. `--write --yes` is the opt-in that registers only the catalog toolkit through the Composio API using a `secret://` project-key reference. Without `--catalog`, the command registers a Composio upstream definition. See [composio.md](composio.md).

## Routines

```sh
python3 hub.py routine enable inventory-digest --runner cron --agent-command 'codex exec'
python3 hub.py routine enable inventory-digest --runner cron --agent-command 'codex exec' --write --yes
python3 hub.py routine status
python3 hub.py routine disable inventory-digest --runner cron --write --yes
python3 hub.py routine enable ID --runner RUNNER --remote
```

`enable` and `disable` print plans by default; `--write` needs `--yes` or interactive confirmation. Local adapters install owned jobs with backups; hosted adapters print prompts and schedules for activation in the chosen app. Definitions stay disabled; per-runner state lives in the private store. `--remote` prints an activation prompt that fetches the published routine on each run; it refuses local runners, `--write` and disable. See [routines.md](routines.md).

## Publish, serve, agents-sync

- `publish-remote [--dry-run] [--include-local] [--yes] [--via cf|api]` builds and verifies the manifest-listed catalog and uploads an immutable revision to the remote server's Workers KV store. `--include-local` also publishes private `.hub-store` imports, listed and confirmed first. See [remote.md](remote.md).
- `serve [--port N]` opens the local setup wizard ([ui.md](ui.md)).
- `agents-import`, `agents-export`, `agents-sync` delegate to amtiYo/agents ([agents-sync.md](agents-sync.md)).
- `import-executor` makes an optional nonsecret copy from a live Executor ([executor-connector.md](executor-connector.md)).

## Check the hub

For an installed catalog, use `python3 hub.py doctor`. It checks schema, MIT
license, catalog hashes, private-state layout and catalog privacy, including
routine prompts. It works from a source snapshot without Git metadata and
does not enforce maintainer commit identities or scan unrelated source and
dependency files. It does not establish hosted connectivity or routine execution.
In a Git installation it still refuses tracked or unignored private state.
With the policy pack installed, `doctor` checks Coldlocker's user-level
`PreToolUse` registrations in Codex `hooks.json` and Claude `settings.json`.
It reads Codex trust with `hooks/list` using a temporary copy of the hook
configuration and runs each registered Coldlocker handler's harmless deny probe
with a temporary home. If Codex reports `modified` or
`untrusted`, open `/hooks`, review and trust the hook, then start a fresh session.
Lines in your private `.hub-store/catalog` that match your own
`.hub-store/private-patterns.txt` are expected there: `doctor` lists them as a
count in a `NOTE` and passes. The same match in shipped catalog content still
fails. `publish-remote` still refuses to proceed with local items unless you
choose `--include-local` or `--shipped-only`.

Source-export contributors run the supported verification recipe:

```sh
python3 -B tools/verify_export.py
```

It requires Git and symlink permission. See [Contributing](../CONTRIBUTING.md)
for scope and platform prerequisites.

The private maintainer checkout runs the stricter checks below. They require
omitted `config/` policy and fixtures and are not supported in this source export.
`doctor` does not replace them or weaken the maintainer identity policy.

```sh
python3 hub.py check
python3 -m unittest discover -s tests -v
```

`check` prints PASS/FAIL for six checks, and every one must pass for exit 0:

1. manifest schema;
2. MIT title and permission grant;
3. manifest paths exist and content hashes match;
4. `config.json` and `.hub-store/` are ignored and untracked;
5. secret and private-path scan;
6. commit emails.

Commit emails: every author email in the checked-out HEAD history must be in `config/commit-email-allowlist.txt` (one address per line). Committers must be listed too, except the exact GitHub web-flow address (`noreply` at `github.com`) when the same commit's author is listed. This reads local commit metadata only, with no network or signature check, and never permits an unlisted author. In pull-request CI, runtime jobs test the merge tree; only when HEAD's two parents match the event's base and head does validation use the base..head range, excluding the synthetic merge identity. Other runs scan HEAD and its ancestors, including detached HEAD. Unrelated branches, remote-tracking branches and tags are outside this checkout check. A branch with no commits has no history to validate. The email check prints SKIP outside a git checkout.

Commit-text CI accepts an allowlisted email only in a complete `Co-authored-by`
trailer. Known automation trailers retain their existing exceptions. Inline
addresses, private hostnames, tokens and local private patterns remain checked.
CI currently runs on Linux only. The Windows verification helper requires Bash beside the Git installation and exports that
directory to later job steps, ahead of the system WSL launcher.
The Windows helper uses the runner's job temp directory for test fixtures so long
checkout paths do not push routine imports and history beyond Windows limits.

Scan failures report file, line and rule name, never the match. Git repositories scan `git ls-files`; elsewhere the scan walks files except `.git`, `.scratch`, `.worktrees`, `.hub-store` and `.agent-hub-ops`. The ignore check needs a Git repository.

The scanner detects GitHub, OpenAI, AWS and Slack token shapes, bearer values, private key blocks, absolute home paths, IPv4 and tailnet addresses, plaintext emails and private hostnames. Numeric single-label hostnames match alone or near host/network context. `.internal`, `.lan` and `.ts.net` match in prose; `.local` and `.home` need nearby host/network context. Semver-shaped package names and common encoding labels are exempt, as are the reserved email domains (`example`, `invalid`, `test`, `example.com`, `example.net`, `example.org`), the fixture labels listed in `checks/scan.py`, and `config/commit-email-allowlist.txt`. The scanner cannot prove arbitrary text holds no secret; keep values in the ignored local store.

Add your own names, handles and host labels to the ignored `.hub-store/private-patterns.txt`, one case-insensitive regular expression per line. `check` reports tracked lines matching any as `private-pattern` without printing the match, and fails on an invalid pattern. The file itself is never tracked or scanned.

## Update the checkout

`python3 hub.py update` updates a Git checkout of Coldlocker on `main`. It
refuses a dirty tree or another branch and changes nothing. Otherwise it prints
`Rollback SHA: <old>`, runs `git fetch origin main` and
`git merge --ff-only FETCH_HEAD`, then runs the new checkout's installation checks
and a [hook self-test](hooks.md#update-self-test). If the merge refuses,
HEAD stays put. If an installation check or the self-test fails or raises,
it runs `git reset --keep <old>` and exits 1. Run `doctor` to check gateway and
OAuth health after an update. On
success it prints the rollback command.

## Policy pack commands

`sync-lessons`, `deliver-rules`, `model`, `guard-stats` and `guard-audit` belong to the
optional owner policy pack ([core and policy split](core-vs-policy.md)). Nothing
in the install, scan, import, connect, use and update journey needs them.

### Audit installed hooks for tampering

```sh
python3 hub.py guard-audit
python3 hub.py guard-audit --json
```

Compares the Claude Code and Codex hook files with the receipts that
`hooks/install.py` wrote and names each added, missing or changed hook, changed
setting and missing or invalid receipt. It prints `PASS`, `FAIL` or `INCOMPLETE`
and exits 0, 1 or 2. See [hook tamper and drift audit](hooks.md#hook-tamper-and-drift-audit).

### Serve proven lessons to every client

```sh
python3 hub.py sync-lessons --lessons <LESSONS_DIR>
```

Mirrors the proven lessons listed in that folder's `briefing.md` into the catalog
as lesson rules, so any MCP client lists them with `hub_list_rules` and reads
them with `hub_get_item`; `publish-remote --include-local` serves them remotely.
Rerun it after the lesson source changes, retires or withdraws a lesson. Rules are described in
[formats](formats.md#delivered-lessons).

### Deliver rules and proven lessons

```sh
python3 hub.py deliver-rules --target <repo> --rule <name> --dry-run
python3 hub.py deliver-rules --target <repo> --rule <name>
```

Import records content only. Delivery copies selected rules into
`.coldlocker/rules/` and maintains an `AGENTS.md` section directing agents to
read them. No human review flag is required. Preview shows the selected files
and proposed section. Commit the delivered files, `.coldlocker/rules.json`
ownership hashes, and instructions in the target repo.

Proven lessons must carry a versioned four-part JSON record containing source
evidence and what went wrong, the fix, the prevention rule, and replay evidence.
The proof names the before and after commits and trees, expected failure,
FAIL/exit 1 before, PASS/exit 0 after, receipt reference, and the check digest.
Privacy and release-check must both have passed. Incomplete records are refused
before writes; the lesson source's event handler retains them in its private pending list
and log and retries on a later event. There are no approval cards or schedules.

Delivery marks eligible lessons for the verify loop. Ordinary fragments remain
instructions only. Repeating delivery is idempotent. An unchanged owned snapshot
may be updated automatically; foreign or modified files and symlinks are preserved.
Existing instructions remain. Repository-specific exports are explicitly bound to
the selected target by the event handler's configuration. Private source details
remain in the lesson source; opaque references and sanitized summaries travel with the rule.
The local exporter and checker are trusted code. These records preserve their
proof; they are not a cryptographic signature for arbitrary third-party files.

In OpenCode, open the target repository and ask the agent to follow the managed
rules in `AGENTS.md`. To apply the ColdFix project holdout, set
`COLDLOCKER_LESSONS` when you run `deliver-rules`. The command checks the target's
`origin` against `held-back-projects.txt` using the same check as the hook. If a
project enters the holdout after a lesson was delivered, use `--withdraw` to
remove the active copy. The core catalog can list and read entries while the
lesson store is unavailable.

Failed, unsafe, or retired latest revisions are withdrawn automatically by the
lesson source's event handler. `deliver-rules --target <repo> --rule <name> --withdraw`
archives an unchanged owned snapshot under `.coldlocker/archive/` and removes
its managed instruction link and active registration. Imported lesson copies
are also removed from the catalog with their history retained. Foreign or edited
files are preserved and the item remains pending. Interrupted writes stay pending
and fail verification until a later event completes them.

Ownership fingerprints normalize CRLF to LF across Git checkout conversion.
Each replacement rechecks its destination to preserve edits made during delivery.
The managed instructions tell agents to apply only completed registrations.

Rule delivery retains replaced file inodes under `.coldlocker/backups/` before
installing with a no-clobber operation. Concurrent replacements are preserved;
conflicts leave the registry pending for retry. Unchanged delivery creates no backup.
Durable write intents restore a missing destination from its retained version
before a later delivery reads instructions or ownership.

Backup contents are ignored by Git. Atomic recovery records and a pending marker
keep an interrupted delivery recoverable and prevent verification until retry.
