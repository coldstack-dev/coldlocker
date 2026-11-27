# Coldlocker

One local hub for skills, rules, hooks, and MCP configs across Claude Code, Codex, and Cursor instead of three copies. Scan existing agent setups into a private local catalog, then connect each agent over MCP (OpenCode and hosted bots supported; connections and plugins are listed, not run). MIT licensed; see [LICENSE](LICENSE).

**No telemetry.** Network: none for local `doctor`, `scan`, `list`, `check`, local `import-*`, `link`, `connect`, or stdio `mcp_endpoint.py`. Opening the local loopback UI (`serve`) stays local except the Sign-in step, which automatically sends HTTPS OAuth discovery requests to configured remote MCP servers before you click Sign in. Outbound network also runs when you invoke: `git clone`, `hub.py update`, and `import-skill`/`pack install` from GitHub URLs (`git`); `gateway.py install` (`npm ci` from npm); gateway/wizard OAuth sign-in/refresh and imported remote MCP servers (HTTPS); `mcp-run` (launches the imported stdio program, whose network use depends on that program); `publish-remote` (Cloudflare Workers KV via API/`cf`/`wrangler`); `connect-composio --catalog --write --yes` (Composio API); `agents-*` (external `agents` CLI); and optional policy-pack hooks (`gh`, `git ls-remote`, read-only `ssh`).

Release status: unreleased. Local catalog acceptance is recorded; the broader [cross-agent product journey](docs/product-acceptance.md) remains to be proven. See [evidence and release gates](docs/release.md).

## Quickstart

You need Git, Python 3.11+ and Claude Code or Codex CLI (Windows: Python 3.12.4+ and `py -3` instead of `python3`). Clone into a permanent folder: connected agents remember its path. See [install details](docs/install.md).

1. **Install.** `doctor` should print `PASS` or `SKIP`, with no `FAIL`.

   ```sh
   git clone https://github.com/coldstack-dev/coldlocker
   cd coldlocker
   python3 hub.py doctor
   ```

2. **Scan.** `scan` lists the agent content it found and changes nothing (reads local agent configs and your user crontab via `crontab -l`). See the [scanner](docs/scan.md) and [guided import](docs/IMPORT-PROMPT.md).

   ```sh
   python3 hub.py scan
   ```

   ```text
   claude: skills 2, rules 1, routines 0, mcp_servers 1, ...
     sc-a92f81d1bfc4 sample (skill) new
     sc-4ba4b59b2b42 claude (rule) new
   ```

3. **Import.** `scan --import --yes` copies what the scan found into the private catalog, holding back anything that needs review. Every import runs the [safety scan](docs/safety.md): high findings block it unless you pass `--approve-risky` after review, and embedded secrets never import.

   ```sh
   python3 hub.py scan --import --yes
   python3 hub.py list
   ```

   Single items: `python3 hub.py import-skill <path>`, `python3 hub.py import-rule <path.md>`, `python3 hub.py import-routine <file.json>`, `python3 hub.py import-mcp <file.json>`; options `--owner O`, `--origin L`, `--name N`. Definitions use `secret://<name>` references, never values ([MCP auth](docs/mcp-auth.md)). A new install lists only `hub-discover` and your imports; [skill packs](docs/cli.md#skill-packs) add shipped skills indexed in [manifest.json](manifest.json).

4. **Connect.** Preview the client entries, then apply them with backups.

   ```sh
   python3 hub.py connect --agent claude codex
   python3 hub.py connect --agent claude codex --write --yes
   claude mcp list
   ```

   - **Gateway running.** Apply `coldlocker-gateway`, restart the client, and call an imported tool.
   - **Gateway unavailable.** Apply `coldlocker` to browse the catalog. To run imported tools, follow the gateway setup command in the preview, check `python3 gateway.py status`, then preview and apply again.
   - **Native stdio (Windows).** Apply the entries, restart the client, and call the imported stdio tool through its server's catalog name.

   `claude mcp list` should show `coldlocker` as connected. The [client guide](docs/clients.md) covers Cursor, OpenCode, and manual setup with `<HUB_DIR>`. See [imported servers](docs/clients.md#imported-mcp-servers) for credentials and duplicate entries. Hosted bots use a read-only [remote catalog](docs/remote.md); see [Hosted Bot setup](docs/hosted-bot-bootstrap.md).

5. **Use.** Ask a connected agent:

   > Find a skill in Coldlocker, read its supporting files, and use it for my task.
   > List the routines and read one when I ask you to follow it.

   Agents fetch entries with `hub_get_item` from `mcp_endpoint.py`. To put skills on disk instead, preview `python3 hub.py link --target <root> --dry-run`, then run it without `--dry-run`. Routines are instructions, not timers ([scheduling](docs/routines.md)); the optional [gateway](docs/gateway.md) runs imported MCP servers behind one endpoint, and [Composio](docs/composio.md) MCPs stay in Composio.

6. **Update.** Edit the original source and scan again. In Setup, review **Update available**, select items, and confirm. From the CLI, take IDs from `python3 hub.py scan` and run `python3 hub.py scan --import --update --only <ID>...` ([scan update guide](docs/scan.md#update)). Connected local clients read new content on their next fetch; for hosted clients, [publish the updated catalog](docs/self-host.md#4-use-it-and-keep-it-current). `python3 hub.py remove <kind> <name> --yes` removes an import. To update Coldlocker itself, run `python3 hub.py update` ([upgrade guide](docs/upgrade.md)).

## Checks and limitations

A public source export excludes private maintainer configuration, workflow files and local state. For the exported catalog, verify local health, safety, and MCP endpoint behavior with:

```sh
python3 hub.py doctor
python3 -B -m unittest discover -s tests -p test_safety.py -v
python3 -B -m unittest discover -s tests -p test_endpoint.py -v
```

Source-export contributors run `python3 -B tools/verify_export.py`. See [Contributing](CONTRIBUTING.md) for the private maintainer checks, `python3 hub.py check` and full unittest discovery. These checks do not approve publication.

## Reference

- [Overview and product goal](docs/OVERVIEW.md). Maintainer checkouts also include `VISION.md`.
- [Install details](docs/install.md), [commands](docs/cli.md), [imports and links](docs/reference.md), [file formats](docs/formats.md)
- [Client setup](docs/clients.md), [capability matrix](docs/capability-matrix.md), [wizard](docs/ui.md), [Executor](docs/executor-connector.md), [agents sync](docs/agents-sync.md)
- [Related projects](docs/related.md), [catalog search experiment](docs/catalog-search.md), [acceptance](docs/acceptance.md)
- [Changelog](CHANGELOG.md) and [security reporting](SECURITY.md)
- [Core and policy split](docs/OVERVIEW.md#core-and-policy-split) and [optional hooks](docs/hooks.md)
