import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

import connect
import hub
import safety_fixtures
import test_ui


REPO = Path(__file__).resolve().parents[1]


class ScanUITests(unittest.TestCase):
    stop = test_ui.UITests.stop
    request = test_ui.UITests.request
    post = test_ui.UITests.post
    def setUp(self):
        test_ui.UITests.setUp(self)
        self.home = self.fixtures / 'home'
        shutil.copytree(REPO / 'tests/fixtures/scan/linux', self.home)
        config = self.home / '.config/opencode/opencode.jsonc'
        text = config.read_text(encoding='utf-8').replace('// synthetic configuration', '').replace(',\n}', '\n}')
        config.with_suffix('.json').write_text(text, encoding='utf-8')
        config.unlink()
        self.server.scan_home = self.home
        self.server.scan_platform = 'linux'
        self.server.scan_env = {}
        self.server.scan_runner = lambda command: ''

    def test_scan_csrf_immutable_inputs_and_round_trip(self):
        before = {p: p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
        self.assertEqual(self.request('/api/scan', {}, {'X-CSRF-Token': 'wrong'})[0], 403)
        report = self.post('scan', {})
        for kind in ('skill', 'routine', 'mcp_server', 'plugin', 'connection'):
            self.assertTrue(any(i['kind'] == kind for i in report['items']), kind)
        ids = [i['id'] for i in report['items'] if i['importable']]
        result = self.post('scan/import', {'ticket': report['ticket'], 'ids': ids})
        self.assertEqual(set(result['imported']), set(ids))
        self.assertEqual(result['rejected'], [])
        after = self.post('scan', {})
        self.assertFalse(any(i['id'] in ids and i['importable'] for i in after['items']))
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        self.assertEqual(self.post_status()['check'], 'Passed')
        process = subprocess.run([sys.executable, '-c', 'import sys; from pathlib import Path; from checks import checker; sys.exit(checker.run(Path(sys.argv[1])))', str(self.root)], cwd=REPO, capture_output=True)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)

    def test_scan_reports_depth_bound_for_redacted_codex_home(self):
        codex_home = self.fixtures / 'alternate-codex'
        self.server.scan_env = {'CODEX_HOME': str(codex_home)}
        boundary = codex_home / 'skills/a/b/c/d/e'
        boundary.mkdir(parents=True)
        source = '<codex-home>/skills'
        self.assertFalse(any(row['source'] == source for row in self.post('scan', {})['incomplete_inventory']))
        nested = boundary / 'nested-skill'
        nested.mkdir()
        (nested / 'SKILL.md').write_text(
            '---\nname: nested-skill\ndescription: Nested fixture\n---\nBody.\n', encoding='utf-8')
        report = self.post('scan', {})
        self.assertNotIn('nested-skill', {item['name'] for item in report['items']})
        self.assertNotIn(str(codex_home), json.dumps(report['incomplete_inventory']))
        self.assertIn({
            'source': source, 'limit': 5, 'reason': 'reached the depth limit of 5',
            'next_step': 'Narrow that folder and scan again.',
        }, report['incomplete_inventory'])

    def test_reviewed_scan_updates_skill_and_routine_for_connected_client(self):
        skill = self.home / '.claude/skills/review-notes/SKILL.md'
        skill.parent.mkdir(parents=True)
        protected = skill.parent.with_name('protected-notes') / 'SKILL.md'
        protected.parent.mkdir()
        units = self.home / '.config/systemd/user'
        units.mkdir(parents=True, exist_ok=True)
        service = units / 'review-job.service'
        service.with_suffix('.timer').write_text('[Timer]\nOnCalendar=daily\n', encoding='utf-8')

        def write_sources(version):
            skill.write_text(safety_fixtures.front('review-notes', f'Take {version} notes.'), encoding='utf-8')
            protected.write_text(safety_fixtures.front('protected-notes', f'Keep {version} notes.'), encoding='utf-8')
            service.write_text(f'[Service]\nExecStart=/bin/echo {version}\n', encoding='utf-8')

        def selection(report):
            return [item for item in report['items'] if item['name'] in ('review-notes', 'review-job', 'protected-notes')]

        write_sources('first')
        report = self.post('scan', {})
        original_ids = [item['id'] for item in selection(report)]
        self.assertEqual(len(original_ids), 3)
        result = self.post('scan/import', {'ticket': report['ticket'], 'ids': original_ids})
        self.assertEqual(result['imported'], original_ids)

        for name in ('mcp_endpoint.py', 'calllog.py', 'packs.py', 'store.py', 'portable.py'):
            shutil.copyfile(REPO / name, self.root / name)
        shutil.copytree(REPO / 'checks', self.root / 'checks')
        client = self.enterContext(subprocess.Popen(
            [sys.executable, '-B', str(self.root / 'mcp_endpoint.py')],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding='utf-8'))

        def rpc(identity, method, params):
            client.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': identity, 'method': method, 'params': params}) + '\n')
            client.stdin.flush()
            message = json.loads(client.stdout.readline())
            self.assertEqual(message['id'], identity)
            return message['result']

        self.assertIn('tools', rpc(1, 'initialize', {'protocolVersion': '2025-03-26'})['capabilities'])

        def get_item(identity, kind, name):
            result = rpc(identity, 'tools/call', {'name': 'hub_get_item', 'arguments': {'kind': kind, 'name': name}})
            return json.loads(result['content'][0]['text'])['files']

        self.assertIn('Take first notes.', next(iter(get_item(2, 'skill', 'review-notes').values())))
        self.assertIn('[Service]\nExecStart=/bin/echo first\n', get_item(3, 'routine', 'review-job').values())

        local_edit = self.local / 'skills/protected-notes/SKILL.md'
        local_edit.write_text('Independent catalog edit.\n', encoding='utf-8')
        write_sources('second')
        report = self.post('scan', {})
        items = {item['name']: item for item in selection(report)}
        candidates = [items[name] for name in ('review-notes', 'review-job')]
        self.assertTrue(all(item['updatable'] for item in candidates))
        ids = [item['id'] for item in candidates]
        self.assertTrue(set(ids).isdisjoint(original_ids))
        conflict = items['protected-notes']
        self.assertEqual(conflict['status'], 'conflict')
        self.assertFalse(conflict.get('updatable'))
        self.assertEqual(self.request('/api/scan/import', {'ticket': report['ticket'], 'ids': [conflict['id']]})[0], 400)
        prompt = self.local / 'routines/review-job/prompt.md'
        self.assertIn('first', prompt.read_text(encoding='utf-8'))
        result = self.post('scan/import', {'ticket': report['ticket'], 'ids': ids})
        self.assertCountEqual(result['updated'], ids)
        self.assertEqual(result['imported'], [])
        self.assertEqual(result['rejected'], [])
        self.assertEqual(local_edit.read_text(encoding='utf-8'), 'Independent catalog edit.\n')
        self.assertIn('Take second notes.', next(iter(get_item(4, 'skill', 'review-notes').values())))
        self.assertIn('[Service]\nExecStart=/bin/echo second\n', get_item(5, 'routine', 'review-job').values())
        self.assertFalse(json.loads((prompt.parent / 'routine.json').read_text(encoding='utf-8'))['enabled'])

    def test_scan_update_preflight_preserves_a_prompt_edited_after_rescan(self):
        units = self.home / '.config/systemd/user'
        units.mkdir(parents=True, exist_ok=True)
        service = units / 'review-job.service'
        service.with_suffix('.timer').write_text('[Timer]\nOnCalendar=daily\n', encoding='utf-8')
        service.write_text('[Service]\nExecStart=/bin/echo first\n', encoding='utf-8')
        report = self.post('scan', {})
        item = next(item for item in report['items'] if item['name'] == 'review-job')
        self.post('scan/import', {'ticket': report['ticket'], 'ids': [item['id']]})
        service.write_text('[Service]\nExecStart=/bin/echo second\n', encoding='utf-8')
        report = self.post('scan', {})
        item = next(item for item in report['items'] if item['name'] == 'review-job')
        self.assertTrue(item['updatable'])
        prompt = self.local / 'routines/review-job/prompt.md'
        original_import = hub.import_item

        def independently_edit(args, root):
            prompt.write_text('Independent prompt edit.\n', encoding='utf-8')
            return original_import(args, root)

        with patch.object(hub, 'import_item', side_effect=independently_edit) as importer:
            result = self.post('scan/import', {'ticket': report['ticket'], 'ids': [item['id']]})
        self.assertEqual(importer.call_count, 1)
        self.assertEqual(result['updated'], [])
        self.assertEqual(result['rejected'], [item['id']])
        self.assertEqual(prompt.read_text(encoding='utf-8'), 'Independent prompt edit.\n')

    def test_crontab_routine_item_carries_runner_schedule_and_timezone(self):
        self.server.scan_env = {'TZ': 'America/Chicago'}
        self.server.scan_runner = lambda command: '0 9 * * 1 /usr/local/bin/weekly-report\n'
        report = self.post('scan', {})
        item = next(i for i in report['items'] if i['kind'] == 'routine' and i['source'] == '<system:crontab>')
        self.assertEqual((item['runner'], item['schedule'], item['timezone']),
                         ('cron', {'type': 'cron', 'expression': '0 9 * * 1'}, 'America/Chicago'))
        self.post('scan/import', {'ticket': report['ticket'], 'ids': [item['id']]})
        again = next(i for i in self.post('scan', {})['items'] if i['name'] == item['name'] and i['kind'] == 'routine')
        self.assertEqual((again['status'], again['runner'], again['timezone']), ('already_in_locker', 'cron', 'America/Chicago'))
        details = json.loads(self.request('/api/status')[1])['inventory']['routines']['details']
        self.assertEqual(details[item['name']], {'runner': 'cron', 'schedule': {'type': 'cron', 'expression': '0 9 * * 1'}, 'timezone': 'America/Chicago'})

    def test_wizard_keeps_connection_of_needs_review_server_unticked_until_server_approved(self):
        safety_fixtures.write(self.home, safety_fixtures.MALICIOUS['unknown-remote'][2])
        report = self.post('scan', {})
        server = next(i for i in report['items'] if i['kind'] == 'mcp_server' and i['name'] == 'unknown-remote')
        connection = next(i for i in report['items'] if i['kind'] == 'connection' and i['name'] == 'unknown-remote')
        self.assertEqual((server['status'], connection['status']), ('needs_review', 'needs_review'))
        self.assertFalse(connection['importable'])
        self.assertEqual(connection['review'], server['review'])
        ids = [i['id'] for i in report['items'] if i['importable']]
        result = self.post('scan/import', {'ticket': report['ticket'], 'ids': ids})
        self.assertNotIn(connection['id'], result['imported'])
        self.assertFalse((self.root / '.hub-store/catalog/connections/unknown-remote.json').exists())
        report = self.post('scan', {})
        server = next(i for i in report['items'] if i['kind'] == 'mcp_server' and i['name'] == 'unknown-remote')
        connection = next(i for i in report['items'] if i['kind'] == 'connection' and i['name'] == 'unknown-remote')
        result = self.post('scan/import', {'ticket': report['ticket'], 'ids': [server['id']], 'approved': [server['id']]})
        self.assertEqual(set(result['imported']), {server['id'], connection['id']})
        self.assertTrue((self.root / '.hub-store/catalog/connections/unknown-remote.json').is_file())

    def post_status(self):
        code, body = self.request('/api/status')
        self.assertEqual(code, 200)
        return json.loads(body)

    def test_scan_selection_expiry_replay_and_stale_content(self):
        report = self.post('scan', {})
        for item in report['items']:
            if not item['importable']:
                self.assertEqual(self.request('/api/scan/import', {'ticket': report['ticket'], 'ids': [item['id']]})[0], 400)
        for ids in ([], ['sc-unknown'], 'bad', [None]):
            self.assertEqual(self.request('/api/scan/import', {'ticket': report['ticket'], 'ids': ids})[0], 400)
        item = next(i for i in report['items'] if i['kind'] == 'skill' and i['importable'])
        source = self.home / item['source'].removeprefix('~/') / 'SKILL.md'
        source.write_text(source.read_text(encoding='utf-8') + '\nChanged after scan.\n', encoding='utf-8')
        result = self.post('scan/import', {'ticket': report['ticket'], 'ids': [item['id']]})
        self.assertEqual(result['imported'], [])
        self.assertEqual(self.request('/api/scan/import', {'ticket': report['ticket'], 'ids': [item['id']]})[0], 400)
        report = self.post('scan', {})
        self.server.jobs[report['ticket']]['time'] -= 901
        self.assertEqual(self.request('/api/scan/import', {'ticket': report['ticket'], 'ids': [item['id']]})[0], 400)

    def test_canaries_absent_from_all_ui_responses(self):
        report = self.post('scan', {})
        transcripts = [json.dumps(report)]
        transcripts.append(json.dumps(self.post('scan/import', {'ticket': report['ticket'], 'ids': [i['id'] for i in report['items'] if i['importable']]})))
        for route in ('/', '/app.js', '/style.css', '/api/status', '/favicon.ico'):
            code, body = self.request(route)
            self.assertIn(code, (200, 204))
            transcripts.append(body.decode('latin-1') if route == '/favicon.ico' else body.decode())
        transcripts.append(json.dumps(self.post('connect', {})))
        for secret in ('canary-secret-value-0001', 'canary-neutral-value-0002'):
            for text in transcripts:
                self.assertNotIn(secret, text)

    def test_connect_snippets_without_config_write(self):
        report = self.post('connect', {})
        self.assertEqual({i['agent'] for i in report['agents']}, set(connect.AGENTS))
        originals = {p: p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
        self.assertNotIn('ticket', report)
        self.assertTrue(all('coldlocker' in i['snippet'] for i in report['agents']))
        self.assertEqual(self.request('/api/connect/write', {'confirm': True})[0], 404)
        self.assertEqual(self.request('/api/connect/write', {}, {'X-CSRF-Token': 'wrong'})[0], 403)
        self.assertEqual({p: p.read_bytes() for p in originals}, originals)
        self.assertFalse(list(self.home.rglob('*.bak')))

    def test_static_xss_safe_and_node_smoke(self):
        js = '\n'.join(path.read_text(encoding='utf-8') for path in (REPO / 'ui').glob('*.js'))
        for unsafe in ('innerHTML', 'outerHTML', 'insertAdjacentHTML', 'document.write(', 'new-password'):
            self.assertNotIn(unsafe, js)
        self.assertIn('textContent', js)
        if not shutil.which('node'):
            self.skipTest('Node is unavailable; JS smoke test skipped')
        result = subprocess.run(['node', str(REPO / 'tests/ui_scan_smoke.cjs')], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_import_confirms_counts_and_shows_result(self):
        if not shutil.which('node'):
            self.skipTest('Node is unavailable; JS smoke test skipped')
        result = subprocess.run(['node', str(REPO / 'tests/ui_import_message_smoke.cjs')], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_approve_checkbox_gates_import(self):
        if not shutil.which('node'):
            self.skipTest('Node is unavailable; JS smoke test skipped')
        result = subprocess.run(['node', str(REPO / 'tests/ui_approve_smoke.cjs')], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_connect_unsupported_agent_keeps_copy_fallback(self):
        path = self.home / '.config/opencode/opencode.json'
        path.rename(path.with_suffix('.jsonc'))
        result = self.post('connect', {})
        item = next(i for i in result['agents'] if i['agent'] == 'opencode')
        self.assertIn('coldlocker', item['snippet'])
