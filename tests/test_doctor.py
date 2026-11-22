import asyncio
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from checks.validators import digest
from test_hub import HubCase, PACK_MISSING

needs_pack = unittest.skipIf(PACK_MISSING, "policy pack not installed")


class DoctorTests(HubCase):
    def setUp(self):
        super().setUp()
        self.environment.update(CODEX_HOME="", CLAUDE_CONFIG_DIR="")
        self.import_skill()

    def test_codex_hook_trust_is_required(self):
        import hub

        home = self.base / "home"
        codex = home / ".codex"
        codex.mkdir(parents=True)
        if PACK_MISSING:
            self.assertIn("SKIP: codex hook in force: policy pack not installed", self.cli("doctor"))
            return

        from hooks.install import hook_command

        command = hook_command(os.sys.executable, os.name == "nt", self.root / "hooks/coldlocker_hook.py",
                               ("--root", self.root.as_posix()))
        hooks = codex / "hooks.json"
        original = json.dumps({"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": command}]}]}})
        hooks.write_text(original, encoding="utf-8")
        config = codex / "config.toml"
        key = f"{hooks.resolve()}:pre_tool_use:0:0"
        server = self.base / "app_server.py"
        server.write_text('''\
import json, os, sys, tomllib
from pathlib import Path

home = Path(os.environ["CODEX_HOME"])
source = home / "hooks.json"
command = json.loads(source.read_text(encoding="utf-8"))["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
state = tomllib.loads((home / "config.toml").read_text(encoding="utf-8")).get("hooks", {}).get("state", {})
entry = state.get(f"{source.resolve()}:pre_tool_use:0:0", {})
digest = entry.get("trusted_hash")
trust = "untrusted" if digest is None else "trusted" if digest == "fixture-current" else "modified"
assert json.loads(sys.stdin.readline())["method"] == "initialize"
print('{"id":1,"result":{}}', flush=True)
assert json.loads(sys.stdin.readline()) == {"method": "initialized"}
request = json.loads(sys.stdin.readline())
assert request["method"] == "hooks/list" and request["params"]["cwds"] == [str(Path.cwd())]
hook = {"eventName": "preToolUse", "handlerType": "command", "command": command,
        "enabled": entry.get("enabled", True), "trustStatus": trust, "matcher": None}
print(json.dumps({"id": request["id"], "result": {"data": [{"hooks": [hook], "errors": []}]}}), flush=True)
''', encoding="utf-8")
        spawn = asyncio.create_subprocess_exec

        async def fixture_app_server(executable, subcommand, *args, **kwargs):
            self.assertEqual((executable, subcommand), ("codex", "app-server"))
            self.assertNotEqual(Path(kwargs["env"]["CODEX_HOME"]), codex)
            return await spawn(os.sys.executable, "-B", str(server), **kwargs)

        with patch.dict(os.environ, {**self.environment, "CODEX_HOME": str(codex)}, clear=True):
            with (patch.object(hub, "ROOT", self.root), patch("sys.stdout", new_callable=io.StringIO) as output,
                  patch.object(asyncio, "create_subprocess_exec", new=fixture_app_server)):
                for digest, enabled, code, message in (
                    ("fixture-old", True, 1, "modified"), (None, True, 1, "untrusted"),
                    ("fixture-current", False, 1, "disabled"), ("fixture-current", True, 0, "PASS: codex hook in force"),
                ):
                    with self.subTest(digest=digest, enabled=enabled):
                        state = f"[hooks.state.{json.dumps(key)}]\nenabled = {json.dumps(enabled)}\n"
                        if digest is not None:
                            state += f"trusted_hash = {json.dumps(digest)}\n"
                        config.write_text(state, encoding="utf-8")
                        output.seek(0)
                        output.truncate()
                        result = hub.doctor(None)
                        self.assertEqual(result, code, output.getvalue())
                        self.assertIn(message, output.getvalue())
                        if code:
                            self.assertIn("/hooks", output.getvalue())
                        self.assertEqual(config.read_text(encoding="utf-8"), state)
        self.assertEqual(hooks.read_text(encoding="utf-8"), original)
        self.assertEqual(set(codex.iterdir()), {hooks, config})

    def test_claude_requires_registration_and_a_denial(self):
        home = self.base / "home"
        settings = home / ".claude/settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text("{}", encoding="utf-8")
        if PACK_MISSING:
            self.assertIn("SKIP: claude hook in force: policy pack not installed", self.cli("doctor"))
            return

        from hooks.install import hook_command

        self.assertIn("SKIP: claude hook in force: Coldlocker PreToolUse hook has not been installed", self.cli("doctor"))
        settings.with_name("settings.json.coldlocker-receipt.json").write_text("{}", encoding="utf-8")
        self.assertIn("FAIL: claude hook in force: Coldlocker PreToolUse hook is not registered",
                      self.cli("doctor", code=1))

        script = self.root / "hooks/coldlocker_hook.py"
        command = hook_command(os.sys.executable, False, script, ("--root", self.root.as_posix()))
        config = json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash|Edit", "hooks": [
            {"type": "command", "command": command}]}]}})
        settings.write_text(config, encoding="utf-8")
        self.assertIn("PASS: claude hook in force", self.cli("doctor"))
        self.assertEqual(settings.read_text(encoding="utf-8"), config)
        self.assertEqual(list(home.rglob("decisions.jsonl")), [])

        script.write_text("print('{}')\n", encoding="utf-8")
        self.assertIn("FAIL: claude hook in force: harmless deny probe did not return the expected denial",
                      self.cli("doctor", code=1))

    @needs_pack
    def test_installed_hook_lessons_status(self):
        from hooks.install import hook_command
        for client in ("claude", "codex"):
            for flag, env, expected in (
                ((), "", f"WARN: {client} hook injects no lessons"),
                (("--lessons", "/x with spaces"), "", f"PASS: {client} hook injects lessons from /x with spaces"),
                (("--lessons=/configured",), "", f"PASS: {client} hook injects lessons from /configured"),
                (("--lessons", "/first", "--lessons", "/last"), "", f"PASS: {client} hook injects lessons from /last"),
                ((), "/y", f"PASS: {client} hook injects lessons from /y"),
                (("--lessons", "/x"), "/y", f"PASS: {client} hook injects lessons from /y"),
            ):
                with self.subTest(client=client, flag=flag, env=env):
                    self.environment.update(COLDLOCKER_LESSONS=env, CODEX_HOME="")
                    home = self.base / "home"
                    config = home / (".claude/settings.json" if client == "claude" else ".codex/hooks.json")
                    config.parent.mkdir(parents=True, exist_ok=True)
                    command = hook_command("python3", hook=Path("/installed checkout/hooks/coldlocker_hook.py"),
                                           args=("--root", "/installed checkout", *flag))
                    config.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [
                        {"type": "command", "command": command}]}]}}))
                    output = self.cli("doctor")
                    self.assertIn(expected, output)
                    if not flag and not env:
                        self.assertIn("--hook-lessons <DIR>", output)
                    config.unlink()

    @needs_pack
    def test_mixed_installed_hooks_report_each_lessons_configuration(self):
        from hooks.install import hook_command
        self.environment.update(COLDLOCKER_LESSONS="", CODEX_HOME="")
        config = self.base / "home/.claude/settings.json"
        config.parent.mkdir(parents=True)
        commands = [hook_command("python3", hook=Path("/installed/hooks/coldlocker_hook.py"),
                                 args=("--root", "/installed", *flags))
                    for flags in ((), ("--lessons", "/configured"))]
        config.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [
            {"type": "command", "command": command} for command in commands]}]}}))
        output = self.cli("doctor")
        self.assertIn("WARN: claude hook injects no lessons", output)
        self.assertIn("PASS: claude hook injects lessons from /configured", output)

    @needs_pack
    def test_unrelated_shell_syntax_does_not_hide_installed_hook(self):
        from hooks.install import hook_command
        self.environment.update(COLDLOCKER_LESSONS="", CODEX_HOME="")
        config = self.base / "home/.claude/settings.json"
        config.parent.mkdir(parents=True)
        command = hook_command("python3", hook=Path("/installed/hooks/coldlocker_hook.py"),
                               args=("--root", "/installed"))
        unrelated = 'echo "$(printf "%s" "' + "'" + '\")"'
        config.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [
            {"type": "command", "command": unrelated},
            {"type": "command", "command": command}]}]}}))
        self.assertIn("WARN: claude hook injects no lessons", self.cli("doctor"))

    @needs_pack
    def test_only_installed_hooks_have_lessons_status(self):
        self.environment.update(COLDLOCKER_LESSONS="", CODEX_HOME="")
        output = self.cli("doctor")
        self.assertNotIn("hook injects", output)
        from hooks.install import hook_command
        config = self.base / "home/.claude/settings.json"
        config.parent.mkdir(parents=True)
        command = hook_command("python3", hook=Path("/installed/hooks/coldlocker_hook.py"),
                               args=("--root", "/installed"))
        config.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [
            {"type": "command", "command": command}]}]}}))
        self.assertIn("WARN: claude hook injects no lessons", self.cli("doctor"))

    def test_user_commit_identity_does_not_fail_installation_health(self):
        self.git("init", "-q")
        self.git("add", ".")
        self.git("-c", "user.name=Fixture", "-c", "user.email=someone@example.org",
                 "commit", "-qm", "fixture")
        output = self.cli("doctor")
        self.assertEqual(output.count("PASS:"), 6 - PACK_MISSING)
        self.assertNotIn("someone@example.org", output)
        self.assertIn("FAIL: commit emails allowlisted", self.cli("check", code=1))

    def test_archive_without_any_git_ancestor_is_healthy(self):
        with tempfile.TemporaryDirectory() as directory:
            import shutil
            target = Path(directory) / "installed catalog"
            shutil.copytree(self.root, target)
            env = {**self.environment, "GIT_CEILING_DIRECTORIES": directory}
            before = {p.relative_to(target): p.read_bytes() for p in target.rglob("*") if p.is_file()}
            result = subprocess.run([os.sys.executable, "hub.py", "doctor"], cwd=target,
                                    env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout.count("PASS:"), 6 - PACK_MISSING)
            self.assertEqual(before, {p.relative_to(target): p.read_bytes() for p in target.rglob("*") if p.is_file()})

    def test_private_configuration_and_secret_store_are_not_scanned(self):
        external = self.base / "private state"
        external.mkdir()
        self.write("config.json", {"store_path": str(external)})
        private = external / "secrets" / "fixture"
        private.parent.mkdir()
        secret = "sk" + "-proj-" + "a" * 30
        private.write_text(secret)
        output = self.cli("doctor")
        self.assertNotIn(str(external), output)
        self.assertNotIn(secret, output)
        self.assertEqual(private.read_text(), secret)

    def test_doctor_does_not_scan_unrelated_source_or_dependencies(self):
        path = self.root / "remote/node_modules/fixture/metadata.txt"
        path.parent.mkdir(parents=True)
        path.write_text("someone@example.org\n")
        self.cli("doctor")

    def test_changed_import_hash_still_fails(self):
        (self.local / "skills/sample/nested/data.txt").write_text("changed\n")
        self.assertIn("FAIL: manifest content hashes", self.cli("doctor", code=1))

    def test_json_names_failed_check_without_raw_error(self):
        (self.local / "skills/sample/nested/data.txt").write_text("changed\n")
        output = self.cli("doctor", "--json", code=1)
        report = json.loads(output)
        self.assertEqual(report.get("schema_version"), 1)
        self.assertEqual(report["exit_code"], 1)
        self.assertEqual(report["failed_checks"], ["manifest content hashes"])
        self.assertNotIn("content hash mismatch", output)

    def test_python_bytecode_cache_does_not_change_skill_digest(self):
        skill = self.local / "skills/sample"
        before = digest(skill)
        cache = skill / "nested/__pycache__"
        cache.mkdir()
        (cache / "data.cpython-314.pyc").write_bytes("/".join(("", "home", "someone")).encode() + b"\0bytecode")
        self.assertEqual(before, digest(skill))
        output = self.cli("doctor")
        self.assertIn("PASS: manifest content hashes", output)
        self.assertIn("PASS: catalog secret and private path scan", output)

    def test_catalog_private_text_is_reported_without_value(self):
        path = self.local / "skills/sample/nested/data.txt"
        email = "@".join(("fixture", "fixture.example.net"))
        path.write_text(email + "\n")
        manifest = self.manifest()
        manifest["skills"][0]["sha256"] = digest(path.parents[1])
        self.write(".hub-store/catalog/manifest.json", manifest)
        output = self.cli("doctor", code=1)
        self.assertIn("email-address", output)
        self.assertNotIn(email, output)

    def test_owner_private_pattern_in_private_catalog_is_expected_but_not_in_shipped_files(self):
        label = "zebulon quux"
        (self.root / ".hub-store/private-patterns.txt").write_text("zebulon[ ]quux\n", encoding="utf-8")
        path = self.local / "skills/sample/nested/data.txt"
        path.write_text(label + "\n")
        manifest = self.manifest()
        manifest["skills"][0]["sha256"] = digest(path.parents[1])
        self.write(".hub-store/catalog/manifest.json", manifest)
        output = self.cli("doctor")
        self.assertIn("PASS: catalog secret and private path scan", output)
        self.assertIn("NOTE: 1 line(s) of private catalog content match your private patterns (expected; "
                      "publish-remote sends local items only with --include-local)", output)
        self.assertNotIn("data.txt", output)
        self.assertNotIn(label, output)
        rule = self.root / "rules/shipped.md"
        rule.parent.mkdir()
        rule.write_text("# Shipped\n" + label + "\n", encoding="utf-8")
        checksum = digest(rule)
        self.write("manifest.json", {"schema_version": 1, "name": "coldlocker", "license": "MIT", "skills": [],
                                     "rules": [{"name": "shipped", "path": "rules/shipped.md", "owner": "coldlocker",
                                                "provenance": {"source_id": "src-" + checksum[:12], "origin": "authored"},
                                                "sha256": checksum}],
                                     "mcp_servers": [], "routines": []})
        output = self.cli("doctor", code=1)
        self.assertIn("FAIL: catalog secret and private path scan: rules/shipped.md:2: private-pattern", output)
        self.assertNotIn(label, output)

    def private_rule(self):
        (self.root / ".hub-store/private-patterns.txt").write_text("zebulon[ ]quux\n", encoding="utf-8")
        rule = self.local / "rules/mine.md"
        rule.parent.mkdir(parents=True)
        rule.write_text("# Mine\nzebulon quux\n", encoding="utf-8")
        return rule

    def shipped_manifest(self, collection, entry):
        self.write("manifest.json", {"schema_version": 1, "name": "coldlocker", "license": "MIT", "skills": [],
                                     "rules": [], "mcp_servers": [], "routines": [], collection: [entry]})

    def test_shipped_entry_pointing_into_the_private_catalog_fails(self):
        rule = self.private_rule()
        checksum = digest(rule)
        self.shipped_manifest("rules", {"name": "mine", "path": ".hub-store/catalog/rules/mine.md", "owner": "coldlocker",
                                        "provenance": {"source_id": "src-" + checksum[:12], "origin": "authored"},
                                        "sha256": checksum})
        output = self.cli("doctor", code=1)
        self.assertIn("FAIL: catalog secret and private path scan: shipped manifest must not reference the local catalog",
                      output)
        self.assertNotIn("zebulon quux", output)

    def test_shipped_routine_prompt_traversing_into_the_private_catalog_fails(self):
        rule = self.private_rule()
        local = digest(rule)
        self.write(".hub-store/catalog/manifest.json", {
            "schema_version": 1, "name": "coldlocker", "license": "MIT", "skills": [], "mcp_servers": [],
            "routines": [], "rules": [{"name": "mine", "path": "rules/mine.md", "owner": "coldlocker",
                                       "provenance": {"source_id": "src-" + local[:12], "origin": "authored"},
                                       "sha256": local}]})
        folder = self.root / "routines/shipped-routine"
        folder.mkdir(parents=True)
        (folder / "routine.json").write_text(json.dumps({
            "id": "shipped-routine", "description": "Shipped routine", "owner": "coldlocker", "runner": "cron",
            "schedule": {"type": "interval", "every_seconds": 3600}, "timezone": "UTC",
            "prompt": "../../.hub-store/catalog/rules/mine.md", "overlap": "skip", "timeout_seconds": 60,
            "enabled": False}), encoding="utf-8")
        checksum = digest(folder / "routine.json")
        self.shipped_manifest("routines", {"id": "shipped-routine", "path": "routines/shipped-routine/routine.json",
                                           "owner": "coldlocker",
                                           "provenance": {"source_id": "src-" + checksum[:12], "origin": "authored"},
                                           "sha256": checksum})
        output = self.cli("doctor", code=1)
        self.assertIn("FAIL: catalog secret and private path scan: .hub-store/catalog/rules/mine.md:2: private-pattern",
                      output)
        self.assertNotIn("zebulon quux", output)

    def test_catalog_prompt_is_screened_even_though_definition_hash_is_unchanged(self):
        routine = self.base / "incoming routine"
        routine.mkdir()
        value = {"id": "fixture-routine", "description": "Fixture routine", "owner": "coldlocker",
                 "runner": "cron", "schedule": {"type": "interval", "every_seconds": 3600},
                 "timezone": "UTC", "prompt": "prompt.md", "overlap": "skip",
                 "timeout_seconds": 60, "enabled": False}
        (routine / "routine.json").write_text(json.dumps(value))
        (routine / "prompt.md").write_text("Fixture prompt.\n")
        self.cli("import-routine", routine / "routine.json")
        email = "@".join(("fixture", "fixture.example.net"))
        (self.local / "routines/fixture-routine/prompt.md").write_text(email + "\n")
        self.assertIn("email-address", self.cli("doctor", code=1))

    def test_tracked_private_state_still_fails(self):
        self.git("init", "-q")
        self.write("config.json", {})
        self.git("add", "-f", "config.json")
        self.assertIn("FAIL: installation state", self.cli("doctor", code=1))

    def test_invalid_config_and_symlinked_state_are_refused(self):
        self.write("config.json", [])
        self.assertIn("FAIL: installation state", self.cli("doctor", code=1))
        (self.root / "config.json").unlink()
        preserved = self.base / "preserved.json"
        preserved.write_text("{}")
        try:
            (self.root / "config.json").symlink_to(preserved)
        except OSError as error:
            self.skipTest(str(error))
        self.assertIn("FAIL: installation state", self.cli("doctor", code=1))
        self.assertEqual(preserved.read_text(), "{}")


if __name__ == "__main__":
    unittest.main()
