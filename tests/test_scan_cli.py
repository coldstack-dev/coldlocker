import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from connect import python_command
import hub
import scanner
from test_scanner import CANARY, FIXTURES, ROOT, snapshot


class ScanCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'locker'
        self.home = Path(self.temp.name) / 'home'
        shutil.copytree(FIXTURES / 'linux', self.home)
        self.root.mkdir()
        for name in ('manifest.json', 'LICENSE', '.gitignore'):
            shutil.copyfile(ROOT / name, self.root / name)
        for name in ('skills', 'rules', 'routines', 'mcp'):
            if (ROOT / name).is_dir():
                shutil.copytree(ROOT / name, self.root / name)

    def command(self, *argv, tty=False, answer='yes'):
        args = hub.parser().parse_args(['scan', '--home', str(self.home), *argv])
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(scanner, 'ROOT', self.root), patch.object(sys.stdin, 'isatty', return_value=tty), patch('builtins.input', return_value=answer), patch.object(scanner, 'default_runner', return_value=''), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = args.action(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_json_import_success_and_refusal(self):
        empty = Path(self.temp.name) / 'empty'
        empty.mkdir()
        self.home = empty
        code, output, _ = self.command('--import', '--json')
        self.assertEqual(code, 1)
        self.assertTrue(output.startswith('{'), output)
        self.assertEqual(json.loads(output).get('schema_version'), 1)
        code, output, _ = self.command('--import', '--yes', '--json')
        self.assertEqual(code, 0)
        report = json.loads(output)
        self.assertEqual(report.get('schema_version'), 1)
        self.assertEqual(report['imported'], [])

    def test_dry_run_json_and_human_do_not_write(self):
        before = snapshot(self.home), snapshot(self.root)
        code, output, error = self.command('--json')
        self.assertEqual(code, 1)
        report = json.loads(output)
        self.assertEqual(report.get('schema_version'), 1)
        self.assertEqual(report['version'], 1)
        self.assertNotIn(CANARY, output + error)
        code, output, error = self.command()
        self.assertEqual(code, 0)
        self.assertIn('claude:', output)
        self.assertIn('plugins', output)
        self.assertIn('connections', output)
        self.assertNotIn(CANARY, output + error)
        self.assertEqual(before, (snapshot(self.home), snapshot(self.root)))

    def test_depth_bound_reports_omitted_skill_in_json_and_text(self):
        base = self.home / '.claude/skills'
        boundary = base / 'a/b/c/d/boundary-skill'
        boundary.mkdir(parents=True)
        (boundary / 'SKILL.md').write_text(
            '---\nname: boundary-skill\ndescription: Boundary fixture\n---\nBody.\n', encoding='utf-8')
        nested = base / 'a/b/c/d/outside/nested-skill'
        nested.parent.mkdir()
        nested.mkdir()
        (nested / '__pycache__').mkdir()
        (nested.parent / 'empty').mkdir()
        for truncated in (False, True):
            with self.subTest(truncated=truncated):
                if truncated:
                    (nested / 'SKILL.md').write_text(
                        '---\nname: nested-skill\ndescription: Nested fixture\n---\nBody.\n', encoding='utf-8')
                code, output, _ = self.command('--json')
                self.assertEqual(code, 2 if truncated else 1)
                report = json.loads(output)
                names = {item['name'] for item in report['items']}
                self.assertIn('boundary-skill', names)
                self.assertNotIn('nested-skill', names)
                warnings = [row for row in report['incomplete_inventory'] if row['source'] == '~/.claude/skills']
                _, human, _ = self.command()
                if truncated:
                    self.assertEqual(warnings, [{
                        'source': '~/.claude/skills', 'limit': 5,
                        'reason': 'reached the depth limit of 5', 'next_step': 'Narrow that folder and scan again.',
                    }])
                    self.assertIn('Incomplete inventory: ~/.claude/skills reached the depth limit of 5. Narrow that folder and scan again.', human)
                else:
                    self.assertEqual(warnings, [])
                    self.assertNotIn('Incomplete inventory: ~/.claude/skills', human)

    def test_projects_root_cap_reports_incomplete_inventory(self):
        projects = Path(self.temp.name) / 'projects'
        projects.mkdir()
        for number in range(1001):
            (projects / f'fixture-{number}').touch()
        code, output, _ = self.command('--projects-root', str(projects), '--json')
        self.assertEqual(code, 2)
        self.assertIn({'source': '<external>/projects', 'limit': 1000}, json.loads(output)['incomplete_inventory'])

    def test_deep_assets_of_found_skill_do_not_report_depth_truncation(self):
        skill = self.home / '.claude/skills/asset-fixture'
        assets = skill / 'assets/a/b/c/d/e'
        assets.mkdir(parents=True)
        (assets / 'example.txt').write_text('Local fixture.\n', encoding='utf-8')
        (skill / 'SKILL.md').write_text(
            '---\nname: asset-fixture\ndescription: Skill with assets\n---\nBody.\n', encoding='utf-8')
        code, output, _ = self.command('--json')
        self.assertEqual(code, 1)
        report = json.loads(output)
        self.assertTrue(any(item['kind'] == 'skill' and item['name'] == 'asset-fixture' for item in report['items']))
        self.assertFalse(any(row['source'] == '~/.claude/skills' for row in report['incomplete_inventory']))

    def test_skill_payload_walk_does_not_report_depth_truncation(self):
        skill = self.home / '.claude/skills/payload-fixture'
        assets = skill / 'assets/a/b/c/d/e/f/g/h'
        assets.mkdir(parents=True)
        (assets / 'example.txt').write_text('Local fixture.\n', encoding='utf-8')
        (skill / 'SKILL.md').write_text(
            '---\nname: payload-fixture\ndescription: Skill with deep assets\n---\nBody.\n', encoding='utf-8')
        code, output, _ = self.command('--json')
        self.assertEqual(code, 1)
        report = json.loads(output)
        item = next(item for item in report['items'] if item['name'] == 'payload-fixture')
        self.assertEqual(item['kind'], 'skill')
        self.assertTrue(item['importable'])
        self.assertEqual(report['incomplete_inventory'], [])

    def test_deep_sources_of_found_plugin_do_not_report_depth_truncation(self):
        plugin = self.home / '.claude/plugins/cache/source-fixture'
        metadata = plugin / '.claude-plugin'
        metadata.mkdir(parents=True)
        (metadata / 'plugin.json').write_text('{"name":"source-fixture","version":"1.0.0"}\n', encoding='utf-8')
        sources = plugin / 'src/a/b/c/d/e/f/g/h/i/j/k'
        sources.mkdir(parents=True)
        (sources / 'example.txt').write_text('Local fixture.\n', encoding='utf-8')
        code, output, _ = self.command('--json')
        self.assertEqual(code, 1)
        report = json.loads(output)
        self.assertTrue(any(item['kind'] == 'plugin' and item['name'] == 'source-fixture' for item in report['items']))
        self.assertFalse(any(row['source'] == '~/.claude/plugins' for row in report['incomplete_inventory']))

    def test_intentionally_shallow_scans_do_not_report_depth_truncation(self):
        for relative in ('.t3/userdata/logs/run', '.codex/rules/nested', '.codex/prompts/nested'):
            folder = self.home / relative
            folder.mkdir(parents=True)
            (folder / 'note.md').write_text('Local fixture.\n', encoding='utf-8')
        code, output, _ = self.command('--json')
        self.assertEqual(code, 1)
        sources = {row['source'] for row in json.loads(output)['incomplete_inventory']}
        self.assertTrue(sources.isdisjoint({'~/.t3/userdata', '~/.codex/rules', '~/.codex/prompts'}), sources)

    def test_compact_and_all_views_preserve_json(self):
        _, data, _ = self.command('--json')
        report = json.loads(data)
        _, compact, _ = self.command()
        _, expanded, _ = self.command('--all')
        _, all_data, _ = self.command('--all', '--json')
        self.assertEqual(json.loads(all_data), report)
        hidden = []
        for item in report['items']:
            self.assertIn(item['id'], expanded)
            if item['status'] in ('vendor', 'duplicate', 'already_in_locker'):
                self.assertNotIn(item['id'], compact)
                hidden.append(item)
            else:
                self.assertIn(item['id'], compact)
                if item['status'] == 'skipped':
                    line = next(line for line in compact.splitlines() if item['id'] in line)
                    self.assertIn('reason:', line)
                    for flag in item['flags']:
                        self.assertIn(flag, line)
        self.assertTrue(hidden)
        self.assertLess(len(compact.splitlines()), len(expanded.splitlines()))
        for agent in report['agents']:
            items = [i for i in hidden if i['agent'] == agent]
            if not items:
                continue
            section = compact.split(agent + ': ', 1)[1].split('\n')
            collapsed = next(line for line in section if 'Collapsed:' in line)
            for kind in ('vendor', 'plugin_cache', 'duplicate', 'already_in_locker'):
                count = sum(('plugin_cache' if i['status'] == 'vendor' and i['ownership'] == 'plugin_cache'
                             else i['status']) == kind for i in items)
                self.assertIn(f'{kind} {count}', collapsed)
        self.assertNotIn(CANARY, compact + expanded)

    def test_human_view_covers_every_status_and_collapsed_category(self):
        items = []
        for index, (status, ownership) in enumerate([
            ('new', 'user'), ('connect', 'user'), ('conflict', 'user'), ('skipped', 'user'),
            ('vendor', 'vendor'), ('vendor', 'system'), ('vendor', 'plugin_cache'),
            ('duplicate', 'user'), ('already_in_locker', 'user')]):
            items.append({'id': f'item-{index}', 'agent': 'codex', 'name': 'fixture',
                          'kind': 'rule', 'status': status, 'ownership': ownership,
                          'flags': ['needs-manual'] if status == 'skipped' else []})
        report = {'counts': {'codex': {'rules': len(items)}}, 'items': items}
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            scanner.human(report)
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 6)
        self.assertEqual(lines[0], 'codex: rules 9')
        for item in items[:4]:
            self.assertIn(item['id'], output.getvalue())
        self.assertIn('[reason: needs-manual]', output.getvalue())
        self.assertIn('vendor 2, plugin_cache 1, duplicate 1, already_in_locker 1', lines[-1])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            scanner.human(report, all_items=True)
        self.assertEqual(len(output.getvalue().splitlines()), 10)
        for item in items:
            self.assertIn(item['id'], output.getvalue())
        self.assertNotIn('Collapsed:', output.getvalue())

    def test_noninteractive_import_requires_yes_and_prints_summary(self):
        before = snapshot(self.root)
        code, output, error = self.command('--import')
        self.assertEqual(code, 1)
        self.assertIn('claude:', output)
        self.assertIn('--yes', error)
        self.assertEqual(before, snapshot(self.root))

    def test_bare_import_confirms_all_and_refusal_writes_nothing(self):
        before = snapshot(self.root)
        code, output, error = self.command('--import', tty=True, answer='no')
        self.assertEqual(code, 1)
        self.assertEqual(before, snapshot(self.root))
        code, output, error = self.command('--import', tty=True)
        self.assertEqual(code, 0, error + output)
        self.assertIn('"rejected": 0', output)
        self.assertNotEqual(before, snapshot(self.root))
        self.assertNotIn(python_command() + ' hub.py connect', output)

    def test_rejected_item_prints_its_real_reason(self):
        elsewhere = Path(self.temp.name) / 'elsewhere'
        elsewhere.mkdir()
        catalog = self.root / '.hub-store/catalog'
        catalog.mkdir(parents=True)
        (catalog / 'rules').symlink_to(elsewhere, target_is_directory=True)
        _, data, _ = self.command('--json')
        item = next(i for i in json.loads(data)['items'] if i['status'] == 'new' and i['kind'] == 'rule')
        code, output, error = self.command('--import', '--yes', '--only', item['id'])
        self.assertEqual(code, 1)
        self.assertIn(f"Rejected {item['id']}: store symlinks are forbidden", error)
        self.assertNotIn('item could not be imported', error)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_json_import_protected_destination_reports_failure(self):
        catalog = self.root / '.hub-store/catalog'
        catalog.mkdir(parents=True)
        (catalog / 'rules').write_text('Preserve this file', encoding='utf-8')
        _, output, _ = self.command('--json')
        item = next(i for i in json.loads(output)['items'] if i['status'] == 'new' and i['kind'] == 'rule')
        code, output, error = self.command('--import', '--yes', '--json', '--only', item['id'])
        self.assertEqual(code, 2)
        report = json.loads(output)
        self.assertIn(f"Rejected {item['id']}:", error)
        self.assertNotIn('item could not be imported', error)
        self.assertEqual(report['errors'], [item['id']])
        self.assertEqual(report['rejected'], [item['id']])
        self.assertEqual((catalog / 'rules').read_text(encoding='utf-8'), 'Preserve this file')

    def test_only_selects_one_and_yes_skips_confirmation(self):
        _, output, _ = self.command('--json')
        item = next(i for i in json.loads(output)['items'] if i['status'] == 'new' and i['kind'] == 'rule')
        code, output, error = self.command('--import', '--only', item['id'], '--yes')
        self.assertEqual(code, 0, output + error)
        self.assertEqual(json.loads(output.splitlines()[-1])['imported'], [item['id']])
        self.assertNotIn(CANARY, output + error)

    def test_plugin_import_is_opt_in(self):
        _, output, _ = self.command('--json')
        plugins = [i for i in json.loads(output)['items'] if i['kind'] == 'plugin' and i['importable']]
        self.assertTrue(plugins)
        code, output, error = self.command('--import', '--yes')
        self.assertEqual(code, 0, output + error)
        imported = set(json.loads(output.splitlines()[-1])['imported'])
        self.assertTrue(imported)
        self.assertFalse(imported & {i['id'] for i in plugins})
        code, output, error = self.command('--import', '--yes', '--only', plugins[0]['id'])
        self.assertEqual(code, 0, output + error)
        self.assertEqual(json.loads(output.splitlines()[-1])['imported'], [plugins[0]['id']])

    def test_agent_filter_updates_counts_and_selection(self):
        code, output, _ = self.command('--agent', 'opencode', '--json')
        self.assertEqual(code, 1)
        report = json.loads(output)
        self.assertEqual(set(report['agents']), {'opencode'})
        self.assertTrue(all(i['agent'] == 'opencode' for i in report['items']))
        self.assertEqual(sum(report['totals']['statuses'].values()), len(report['items']))
        self.assertEqual(report['totals']['mcp_servers'], 2)

    def test_next_step_printed_when_connect_exists(self):
        (self.root / 'connect.py').write_text('', encoding='utf-8')
        code, output, _ = self.command('--import', '--yes')
        self.assertEqual(code, 0)
        self.assertTrue(output.rstrip().endswith(python_command() + ' hub.py connect'))

    def test_next_step_uses_the_running_interpreter_on_windows(self):
        (self.root / 'connect.py').write_text('', encoding='utf-8')
        with patch('sys.platform', 'win32'), patch('sys.executable', 'C:\\Python312\\python.exe'):
            code, output, _ = self.command('--import', '--yes')
        self.assertEqual(code, 0)
        self.assertEqual(output.rstrip().splitlines()[-1], 'C:\\Python312\\python.exe hub.py connect')

    def test_cli_invalid_input_exits_two_without_exception_details(self):
        process = subprocess.run([sys.executable, str(ROOT / 'hub.py'), 'scan', '--home', str(self.home / 'missing'), '--json'], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(process.returncode, 2)
        self.assertEqual(json.loads(process.stdout).get('schema_version'), 1)
        self.assertNotIn('Traceback', process.stderr)
        self.assertNotIn(str(self.home), process.stderr)

    def test_unknown_agent_exits_nonzero_naming_valid_agents(self):
        process = subprocess.run([sys.executable, str(ROOT / 'hub.py'), 'scan', '--home', str(self.home), '--agent', 'nonesuch'], capture_output=True, text=True, encoding='utf-8')
        self.assertNotEqual(process.returncode, 0)
        for agent in ('claude', 'codex', 'cursor', 'opencode', 'agents', 'grok', 't3', 'system'):
            self.assertIn(agent, process.stderr)

    def test_supported_agent_with_nothing_found_says_where_it_looked(self):
        empty = Path(self.temp.name) / 'empty'
        empty.mkdir()
        self.home = empty
        for agent, looked in (('opencode', '~/.config/opencode'), ('grok', '~/.grok')):
            code, output, _ = self.command('--agent', agent)
            self.assertEqual(code, 0)
            self.assertIn(f'{agent}: nothing found', output)
            self.assertIn(str(Path(looked)), output)
            self.assertNotIn(str(empty), output)

    def test_nothing_found_is_not_added_to_json(self):
        empty = Path(self.temp.name) / 'empty'
        empty.mkdir()
        self.home = empty
        _, output, _ = self.command('--agent', 'opencode', '--json')
        self.assertEqual(json.loads(output)['counts'], {})

    def test_default_scan_hints_project_option_only_when_none_given(self):
        _, output, _ = self.command()
        self.assertIn('--project <dir>', output)
        project = Path(self.temp.name) / 'project'
        project.mkdir()
        _, output, _ = self.command('--project', str(project))
        self.assertNotIn('--project <dir>', output)
        _, output, _ = self.command('--json')
        self.assertNotIn('--project <dir>', output)

    def test_write_options_without_import_rejected(self):
        with self.assertRaises(ValueError):
            self.command('--yes')
        with self.assertRaises(ValueError):
            self.command('--only', 'sc-000000000000')


if __name__ == '__main__':
    unittest.main()
