# Capability matrix

What each client gets from Coldlocker today, and the evidence for it. Skill and
routine acceptance details are in [product acceptance](product-acceptance.md);
connection setup is in [client setup](clients.md).

Statuses:

- **verified**: a recorded run or a test in this repository shows it working.
- **documented**: the setup path is written down, but no end-to-end run is recorded.
- **fallback**: Coldlocker prints the entry or steps; the user applies them by hand.
- **unsupported**: Coldlocker doesn't provide it for this client.
- **unknown**: nothing establishes it either way.

Columns:

Catalog access, skill use and upstream tool execution require separate evidence.
A catalog listing only establishes access.

- **Catalog read**: the client can read entries through the catalog tools (`hub_list_*`, `hub_get_item`).
- **Skill use**: a model in the client has applied a catalog skill in a task.
- **MCP via gateway**: the client can call an upstream tool through the local [gateway](gateway.md). Fixture proofs establish execution against test servers only.
- **Rules**: catalog rules readable through `hub_list_rules`, or delivered into a repository with [`deliver-rules`](cli.md#deliver-rules-and-proven-lessons).
- **Lessons**: proven ColdFix lessons delivered through repository rules or injected at session and subagent start by the [Coldlocker hook](hooks.md#coldlocker-hook).
- **Hooks/guards** (optional [policy pack](OVERVIEW.md#core-and-policy-split), like Lessons and Modes): keep-going, briefing, owner, publication and dispatch hooks. They provide best-effort checks before tool calls. [Credentials boundary](hooks.md#credentials-boundary)
- **Modes**: the `chat:` / `work:` session switch, owner-defined custom modes (`~/.coldlocker/modes.json`) and the `COLDLOCKER_START_IN_CHAT=1` switch that starts sessions without injected context in the [Coldlocker hook](hooks.md#coldlocker-hook).

| Client | Catalog read | Skill use | MCP via gateway | Rules | Lessons | Hooks/guards | Modes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Claude Code | verified ([run](release.md#recorded-local-client-acceptance)) | verified ([run](product-acceptance.md#compatibility-and-evidence)) | verified, fake providers only ([proof](gateway.md#fixture-proofs)) | documented ([rules](cli.md#deliver-rules-and-proven-lessons)) | verified, policy-pack tests ([hook behavior](hooks.md#coldlocker-hook)) | verified, policy-pack tests ([hook requirements](hooks.md#what-each-hook-needs)) | verified, policy-pack tests ([hook behavior](hooks.md#coldlocker-hook)) |
| Codex CLI | verified ([run](release.md#recorded-local-client-acceptance)) | verified ([run](product-acceptance.md#compatibility-and-evidence)) | verified, fake providers only ([proof](gateway.md#fixture-proofs)) | documented ([rules](cli.md#deliver-rules-and-proven-lessons)) | verified, policy-pack tests ([startup budget](hooks.md#scoped-transcript-advice-and-startup-budget)) | verified, policy-pack tests ([hook requirements](hooks.md#what-each-hook-needs)) | documented ([hooks](hooks.md#coldlocker-hook)) |
| Cursor CLI | documented, tools listed ([VM run](clients.md#fresh-vm-acceptance)) | unknown | documented ([setup](clients.md#through-the-gateway)) | documented ([rules](cli.md#deliver-rules-and-proven-lessons)) | unsupported | unsupported | unsupported |
| OpenCode | verified, local stdio discovery and supporting-file read with a scripted provider ([run](product-acceptance.md#opencode-use-journey)). Fallback: paste the printed stdio entry ([setup](clients.md#connect-detected-agents)) | unknown for model-driven use. Scripted task result [recorded](product-acceptance.md#opencode-use-journey) | documented ([setup](clients.md#through-the-gateway)) | verified, synthetic lesson ([rules](cli.md#deliver-rules-and-proven-lessons)) | verified, repository rule read by OpenCode 1.19.6 with a local model stub ([setup](cli.md#deliver-rules-and-proven-lessons)) | unsupported | unsupported |
| Gemini CLI | documented, connected ([VM run](clients.md#fresh-vm-acceptance)) | unknown | documented ([entry shapes](clients.md#connect-detected-agents)) | documented ([rules](cli.md#deliver-rules-and-proven-lessons)) | unsupported | unsupported | unsupported |
| VS Code | documented, handshake only ([VM run](clients.md#fresh-vm-acceptance)) | unknown | documented ([entry shapes](clients.md#connect-detected-agents)) | documented ([rules](cli.md#deliver-rules-and-proven-lessons)) | unsupported | unsupported | unsupported |
| Claude Desktop (local) | documented, handshake only ([VM run](clients.md#fresh-vm-acceptance)) | unknown | unsupported, stdio only ([notes](clients.md#per-client-notes)) | documented ([rules](cli.md#deliver-rules-and-proven-lessons)) | unsupported | unsupported | unsupported |
| Windsurf | fallback, snippet printed ([notes](clients.md#per-client-notes)) | unknown | unknown | fallback ([notes](clients.md#per-client-notes)) | unsupported | unsupported | unsupported |
| Hermes Agent | fallback, YAML printed ([notes](clients.md#per-client-notes)) | unknown | fallback ([notes](clients.md#per-client-notes)) | fallback ([notes](clients.md#per-client-notes)) | unsupported | unsupported | unsupported |
| Hosted Bot (remote) | verified ([run](product-acceptance.md#compatibility-and-evidence)) | verified, user-reported ([run](product-acceptance.md#compatibility-and-evidence)) | unsupported ([hosted](hosted-later.md)) | documented ([remote](remote.md#verify-catalog-access-in-each-bot)) | unsupported | unsupported | unsupported |
| Claude.ai and Claude Desktop connector (remote) | documented ([remote](remote.md#connect-a-client)) | unknown | unsupported ([hosted](hosted-later.md)) | documented ([remote](remote.md#verify-catalog-access-in-each-bot)) | unsupported | unsupported | unsupported |
| ChatGPT (remote) | documented ([remote](remote.md#connect-a-client)) | unknown | unsupported ([hosted](hosted-later.md)) | documented ([remote](remote.md#verify-catalog-access-in-each-bot)) | unsupported | unsupported | unsupported |
| Muse Code (remote) | documented ([remote](remote.md#connect-a-client)) | unknown | unknown | documented ([remote](remote.md#verify-catalog-access-in-each-bot)) | unsupported | unsupported | unsupported |
| OpenMausBot (remote) | documented ([remote](remote.md#connect-a-client)) | unknown | unknown | documented ([remote](remote.md#verify-catalog-access-in-each-bot)) | unsupported | unsupported | unsupported |
| T3 remote OAuth | unknown ([remote](remote.md#connect-a-client)) | unknown | unknown | unknown | unsupported | unsupported | unsupported |
| Composio sessions | documented ([Composio](composio.md)) | unknown | unsupported ([hosted](hosted-later.md)) | documented ([Composio](composio.md)) | unsupported | unsupported | unsupported |
| MausBot | fallback, local Codex or Claude Code ([remote](remote.md#connect-a-client)) | unknown | unknown | unknown | unsupported | unsupported | unsupported |
| Dots, hosted Muse | unknown ([remote](remote.md#connect-a-client)) | unknown | unsupported ([hosted](hosted-later.md)) | unknown | unsupported | unsupported | unsupported |

Notes:

- Hooks, modes, and session-start lesson injection require Coldlocker's hook.
  Claude Code and Codex load it after [`hooks/install.py`](hooks.md#what-each-hook-needs).
  Hook injection also requires a lessons folder.
- T3 runs the local Codex and Claude Code CLIs, so their rows apply to it
  ([acceptance](product-acceptance.md#compatibility-and-evidence)).
- Rows for local clients rate their local connection only. Their remote
  catalog connections (Claude Code, Codex, Cursor, Hermes, Gemini CLI) are
  documented at most, with no recorded run; see
  [remote setup](remote.md#connect-a-client).
- Rules marked documented have no recorded run of a model applying a delivered rule.
- Native scheduling is out of scope for v1 and unsupported for every client:
  Coldlocker adds no scheduler, and routines run when an agent is asked. The
  native schedule trial produced no run; client schedulers remain unproved
  ([acceptance](product-acceptance.md#release-boundary)).
- Update this table in the same change that adds or removes evidence for a cell.
