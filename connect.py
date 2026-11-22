import copy
import datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

from client_paths import codex_home
from importers import json_object
from portable import print_json, python_command
from store import ordinary_path

SKILLS_REACH = ('every imported skill is already readable from any connected agent through the Coldlocker MCP tools '
                '(hub_list_skills, hub_get_item); link --target <a project folder> additionally places them as native skills inside that one project')


TOKEN_VARIABLE = 'COLDLOCKER_GATEWAY_TOKEN'
LEGACY_SERVERS = {'agentlocker': 'coldlocker', 'agentlocker-gateway': 'coldlocker-gateway'}
AGENTS = ('claude', 'codex', 'cursor', 'opencode', 'claude_desktop', 'gemini', 'windsurf', 'vscode', 'hermes')
LABELS = {'claude': 'Claude', 'codex': 'Codex', 'cursor': 'Cursor', 'opencode': 'OpenCode',
          'claude_desktop': 'Claude Desktop', 'gemini': 'Gemini CLI', 'windsurf': 'Windsurf',
          'vscode': 'VS Code', 'hermes': 'Hermes Agent'}
PATHS = {
    'linux': ('.claude.json', '.codex/config.toml', '.cursor/mcp.json', '.config/opencode/opencode.json',
              '.config/Claude/claude_desktop_config.json', '.gemini/settings.json',
              '.codeium/windsurf/mcp_config.json', '.config/Code/User/mcp.json', '.hermes/config.yaml'),
    'darwin': ('.claude.json', '.codex/config.toml', '.cursor/mcp.json', '.config/opencode/opencode.json',
               'Library/Application Support/Claude/claude_desktop_config.json', '.gemini/settings.json',
               '.codeium/windsurf/mcp_config.json', 'Library/Application Support/Code/User/mcp.json', '.hermes/config.yaml'),
    'win32': ('.claude.json', '.codex/config.toml', '.cursor/mcp.json', '.config/opencode/opencode.json',
              'AppData/Roaming/Claude/claude_desktop_config.json', '.gemini/settings.json',
              '.codeium/windsurf/mcp_config.json', 'AppData/Roaming/Code/User/mcp.json', '.hermes/config.yaml'),
}


MINIMUM_PYTHON = (3, 10)


class TomlUnavailable(ValueError):
    pass


def toml(text):
    try:
        import tomllib
    except ImportError:
        raise TomlUnavailable('Codex TOML requires Python 3.11 or later') from None
    return tomllib.loads(text)


def config_paths(home, platform=None, env=None):
    home = Path(home).absolute()
    platform = platform or sys.platform
    platform = 'win32' if platform.startswith('win') else platform
    if platform not in PATHS:
        raise ValueError('unsupported platform')
    env = os.environ if env is None else env
    paths = dict(zip(AGENTS, (home / part for part in PATHS[platform])))
    if platform == 'win32' and env.get('APPDATA'):
        roaming = Path(env['APPDATA'])
        paths['claude_desktop'] = roaming / 'Claude/claude_desktop_config.json'
        paths['vscode'] = roaming / 'Code/User/mcp.json'
    paths['codex'] = codex_home(home, env, platform) / 'config.toml'
    if env.get('CLAUDE_CONFIG_DIR'):
        paths['claude'] = Path(env['CLAUDE_CONFIG_DIR']) / '.claude.json'
    if env.get('XDG_CONFIG_HOME'):
        paths['opencode'] = Path(env['XDG_CONFIG_HOME']) / 'opencode/opencode.json'
    return {agent: path.absolute() for agent, path in paths.items()}


def detect(home, platform=None, env=None):
    home = Path(home).absolute()
    platform = platform or sys.platform
    platform = 'win32' if platform.startswith('win') else platform
    env = os.environ if env is None else env
    paths = config_paths(home, platform, env)
    found = {}
    for agent, path in paths.items():
        markers = [path, path.parent]
        if agent == 'claude':
            markers = [path, Path(env['CLAUDE_CONFIG_DIR']) if env.get('CLAUDE_CONFIG_DIR') else home / '.claude']
        if agent == 'cursor':
            markers += [home / ('Library/Application Support/Cursor' if platform == 'darwin' else
                               'AppData/Roaming/Cursor' if platform == 'win32' else '.config/Cursor')]
        if agent == 'windsurf':
            markers += [home / ('Library/Application Support/Windsurf' if platform == 'darwin' else
                                'AppData/Roaming/Windsurf' if platform == 'win32' else '.codeium/windsurf')]
        if agent == 'vscode':
            markers += [path.parent]
        if any(marker.exists() for marker in markers):
            found[agent] = path.absolute()
    return found


def quote_path(path):
    if sys.platform.startswith('win'):
        return f'"{path}"'
    return shlex.quote(str(path))


def interpreter():
    if not sys.platform.startswith('win'):
        found = shutil.which('python3')
        if found and os.path.isabs(found):
            code = 'import sys; sys.exit(sys.version_info < %r)' % (MINIMUM_PYTHON,)
            try:
                if subprocess.run([found, '-c', code], capture_output=True, timeout=30).returncode == 0:
                    return found
            except (OSError, subprocess.SubprocessError):
                pass
    return sys.executable


def shown_root(root):
    root = Path(root)
    candidates = [os.environ.get('PWD'), os.path.dirname(os.path.abspath(sys.argv[0])) if sys.argv and sys.argv[0] else None]
    for candidate in candidates:
        if candidate and os.path.isabs(candidate):
            try:
                if Path(candidate).samefile(root):
                    return Path(os.path.abspath(candidate))
            except OSError:
                pass
    return Path(os.path.abspath(root))


def stdio(agent, python, args):
    if agent == 'opencode':
        return {'type': 'local', 'command': [python, *args]}
    entry = {'command': python, 'args': list(args)}
    if agent == 'vscode':
        entry['type'] = 'stdio'
    return entry


def entries(root, agent, gateway=None, absolute=True, native=()):
    root = Path(os.path.abspath(root)) if absolute else Path(root)
    python = interpreter() if absolute or sys.platform.startswith('win') else 'python3'
    result = {'coldlocker': stdio(agent, python, [str(root / 'mcp_endpoint.py')])}
    for name in native:
        result[name] = stdio(agent, python, [str(root / 'hub.py'), 'mcp-run', name])
    if gateway and gateway.get('status') == 'running' and agent != 'claude_desktop':
        snippets = gateway.get('snippets', {})
        gateway_url = gateway.get('url') or json.loads(snippets['Claude'])['mcpServers']['coldlocker-gateway']['url']
        if agent == 'codex':
            result.update(toml(snippets['Codex'])['mcp_servers'])
        elif agent in ('gemini', 'vscode', 'windsurf', 'hermes'):
            remote = {'gemini': {'httpUrl': gateway_url}, 'vscode': {'type': 'http', 'url': gateway_url},
                      'windsurf': {'serverUrl': gateway_url}, 'hermes': {'url': gateway_url}}[agent]
            if gateway.get('tokenRequired'):
                reference = '${env:' + TOKEN_VARIABLE + '}' if agent in ('vscode', 'windsurf') else '${' + TOKEN_VARIABLE + '}'
                remote['headers'] = {'Authorization': 'Bearer ' + reference}
            result['coldlocker-gateway'] = remote
        else:
            source = 'Cursor' if agent == 'opencode' else LABELS[agent]
            remote = json.loads(snippets[source])['mcpServers']['coldlocker-gateway']
            if agent == 'opencode':
                remote = {**remote, 'type': 'remote'}
                if 'headers' in remote:
                    remote['headers'] = {k: re.sub(r'\$\{(?:env:)?([^}]+)\}', r'{env:\1}', v)
                                         for k, v in remote['headers'].items()}
            result['coldlocker-gateway'] = remote
        remote = result['coldlocker-gateway']
        parsed = urlsplit(remote.get('url') or remote.get('httpUrl') or remote.get('serverUrl', ''))
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('unsafe gateway URL')
    return result


def render_toml(servers):
    lines = []
    for name, entry in servers.items():
        lines.append('[mcp_servers.' + name + ']')
        for key, value in entry.items():
            if not isinstance(value, dict):
                lines.append(key + ' = ' + json.dumps(value, ensure_ascii=False))
        for key, value in entry.items():
            if isinstance(value, dict):
                lines.append('[mcp_servers.' + name + '.' + key + ']')
                lines.extend(json.dumps(k) + ' = ' + json.dumps(v) for k, v in value.items())
        lines.append('')
    return '\n'.join(lines)


def render(root, agent, gateway=None, absolute=True, native=()):
    servers = entries(root, agent, gateway, absolute=absolute, native=native)
    if agent == 'hermes':
        lines = ['mcp_servers:']
        for name, entry in servers.items():
            lines.append(f'  {name}:')
            if 'command' in entry:
                lines.extend([f'    command: {json.dumps(entry["command"])}',
                              f'    args: {json.dumps(entry["args"])}'])
            else:
                lines.append(f'    url: {json.dumps(entry["url"])}')
                if 'headers' in entry:
                    lines.append('    headers:')
                    lines.extend(f'      {json.dumps(k)}: {json.dumps(v)}' for k, v in entry['headers'].items())
        return '\n'.join(lines)
    if agent == 'codex':
        return render_toml(servers)
    key = 'mcp' if agent == 'opencode' else 'servers' if agent == 'vscode' else 'mcpServers'
    return json.dumps({key: servers}, indent=2)


def connect_snippets(root):
    return {LABELS[agent]: render(root, agent, absolute=False) for agent in ('claude', 'codex', 'cursor')}


def gateway_connection(root, state_reader=None):
    python = python_command()
    hint = f'{python} gateway.py prepare && {python} gateway.py start'
    try:
        import executor_connector
        if executor_connector.configuration(root)['backend'] == 'executor':
            return executor_connector.connection(root)
        from gateway import state
        from gateway_adapter import LOOPBACK
        current = (state_reader or state)(root / '.hub-store/gateway')
        port = current['port'] if current else None
        if type(port) is not int or not 1024 <= port <= 65535:
            return {'status': 'stopped', 'command': hint, 'snippets': {}}
    except (ImportError, OSError, ValueError, KeyError, TypeError):
        return {'status': 'stopped', 'command': hint, 'snippets': {}}
    url = f'http://{LOOPBACK}:{port}/mcp'
    claude = {'type': 'http', 'url': url}
    cursor = {'url': url}
    codex = '[mcp_servers.coldlocker-gateway]\nurl = ' + json.dumps(url) + '\n'
    if current.get('require_token'):
        claude['headers'] = {'Authorization': 'Bearer ${' + TOKEN_VARIABLE + '}'}
        cursor['headers'] = {'Authorization': 'Bearer ${env:' + TOKEN_VARIABLE + '}'}
        codex += 'bearer_token_env_var = ' + json.dumps(TOKEN_VARIABLE) + '\n'
    return {'status': 'running', 'url': url, 'command': hint, 'tokenRequired': bool(current.get('require_token')), 'snippets': {
        'Claude': json.dumps({'mcpServers': {'coldlocker-gateway': claude}}, indent=2),
        'Cursor': json.dumps({'mcpServers': {'coldlocker-gateway': cursor}}, indent=2),
        'Codex': codex}}


def ordinary(path):
    ordinary_path(path)
    for item in (path, *path.parents):
        if item.exists() and not (item.is_file() if item == path else item.is_dir()):
            raise ValueError('refusing non-ordinary destination')


def table_headers(text):
    multiline = None
    offset = 0
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        if multiline is None and stripped.startswith('['):
            header = stripped.split('#', 1)[0].strip()
            try:
                value = toml(header + '\n__locker_probe = 0\n')
                path = []
                while isinstance(value, dict) and len(value) == 1:
                    key, value = next(iter(value.items()))
                    path.append(key)
                if path and path[-1] == '__locker_probe':
                    yield offset, path[:-1]
                else:
                    yield offset, []
            except ValueError:
                yield offset, []
        i = 0
        while i < len(line):
            if multiline:
                if line.startswith(multiline, i):
                    i += 3
                    multiline = None
                elif multiline == '"""' and line[i] == '\\':
                    i += 2
                else:
                    i += 1
            elif line[i] == '#':
                break
            elif line.startswith(('"""', "'''"), i):
                multiline = line[i:i + 3]
                i += 3
            elif line[i] in ('"', "'"):
                quote = line[i]
                i += 1
                while i < len(line):
                    if line[i] == '\\' and quote == '"':
                        i += 2
                    elif line[i] == quote:
                        i += 1
                        break
                    else:
                        i += 1
            else:
                i += 1
        offset += len(line)


def preserve_environment_references(group, servers):
    servers = copy.deepcopy(servers)
    existing = group.get('coldlocker-gateway', group.get('agentlocker-gateway', {}))
    desired = servers.get('coldlocker-gateway', {})
    if not isinstance(existing, dict) or not isinstance(desired, dict):
        return servers
    token_variable = desired.get('bearer_token_env_var')
    if isinstance(token_variable, str) and existing.get('bearer_token_env_var') == token_variable.replace('COLDLOCKER_', 'AGENTLOCKER_'):
        desired['bearer_token_env_var'] = existing['bearer_token_env_var']
    for field in ('headers', 'env_http_headers'):
        previous = existing.get(field, {})
        if not isinstance(previous, dict):
            continue
        for key, value in desired.get(field, {}).items():
            if isinstance(value, str) and previous.get(key) == value.replace('COLDLOCKER_', 'AGENTLOCKER_'):
                desired[field][key] = previous[key]
    return servers


def registration_aliases(group, servers):
    servers = dict(servers)
    for old, new in LEGACY_SERVERS.items():
        if old not in group:
            continue
        if new in group:
            if group[old] != group[new]:
                raise ValueError('conflicting MCP alias definitions; reconcile them before reconnecting')
            raise ValueError('duplicate MCP aliases; reconcile them before reconnecting')
        if new in servers:
            servers[old] = servers.pop(new)
    return servers


def merge_toml(text, servers):
    before = toml(text)
    expected = copy.deepcopy(before)
    group = expected.setdefault('mcp_servers', {})
    if not isinstance(group, dict):
        raise ValueError('invalid MCP collection')
    servers = preserve_environment_references(group, servers)
    servers = registration_aliases(group, servers)
    group.update(servers)
    if before == expected:
        return text
    headers = list(table_headers(text)) + [(len(text), [])]
    chunks = []
    position = 0
    for (start, path), (end, _) in zip(headers, headers[1:]):
        if len(path) >= 2 and path[0] == 'mcp_servers' and path[1] in servers:
            chunks.append(text[position:start])
            chunks.extend(line for line in text[start:end].splitlines(keepends=True) if line.lstrip().startswith('#'))
            position = end
    chunks.append(text[position:])
    newline = '\r\n' if '\r\n' in text else '\n'
    result = ''.join(chunks)
    result += (newline if result and not result.endswith('\n') else '') + newline
    result += render_toml(servers).replace('\n', newline)
    if toml(result) != expected:
        raise ValueError('unsupported TOML layout; existing configuration preserved')
    return result


def skip_space(text, index):
    while text[index] in ' \t\r\n':
        index += 1
    return index


def json_members(text, start):
    decoder = json.JSONDecoder()
    found = []
    index = skip_space(text, start + 1)
    while text[index] != '}':
        key, position = decoder.raw_decode(text, index)
        value = skip_space(text, skip_space(text, position) + 1)
        _, end = decoder.raw_decode(text, value)
        found.append((key, index, value, end))
        index = skip_space(text, end)
        if text[index] == ',':
            index = skip_space(text, index + 1)
    return found, index


def line_indent(text, index):
    line = text[text.rfind('\n', 0, index) + 1:index]
    return line[:len(line) - len(line.lstrip(' \t'))]


def splice_members(text, start, updates, unit, newline, inline=None):
    found, close = json_members(text, start)
    existing = {key: (value, end) for key, _, value, end in found}
    compact = unit is None or bool(found) and '\n' not in text[start:close]
    if inline is None:
        inline = compact or bool(found) and '\n' not in text[found[0][2]:found[0][3]]
    base = line_indent(text, start)
    child = line_indent(text, found[-1][1]) if found and not compact else base + (unit or '')

    def dump(value, indent, flat):
        if flat:
            return json.dumps(value, ensure_ascii=False)
        return json.dumps(value, indent=unit, ensure_ascii=False).replace('\n', newline + indent)

    edits = []
    added = []
    current = json.loads(text[start:close + 1])
    for key, value in updates.items():
        if key in existing:
            if current[key] != value:
                begin, end = existing[key]
                member = next(item[1] for item in found if item[0] == key)
                edits.append((begin, end, dump(value, line_indent(text, member), '\n' not in text[begin:end])))
        else:
            added.append(json.dumps(key, ensure_ascii=False) + ': ' + dump(value, child, inline))
    if added:
        if compact:
            body = ', '.join(added)
            edits.append((found[-1][3], found[-1][3], ', ' + body) if found else (start + 1, close, body))
        else:
            body = (',' + newline).join(child + item for item in added)
            if found:
                edits.append((found[-1][3], found[-1][3], ',' + newline + body))
            else:
                edits.append((start + 1, close, newline + body + newline + base))
    for begin, end, replacement in sorted(edits, reverse=True):
        text = text[:begin] + replacement + text[end:]
    return text


def imported_servers(root):
    from mcp_endpoint import LOCAL, entries as catalog, read_server
    result = {}
    try:
        for entry in catalog(Path(root), 'mcp_servers'):
            if entry['path'].startswith(LOCAL + '/'):
                try:
                    result[entry['name']] = read_server(Path(root), entry).get('transport')
                except (OSError, ValueError):
                    result[entry['name']] = None
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}
    return result


def foreign_servers(path, agent, hub):
    try:
        text = path.read_text(encoding='utf-8') if path.is_file() else ''
        group = ((toml(text) if agent == 'codex' else json_object(text)) if text.strip() else {}).get(group_key(agent), {})
    except (OSError, ValueError, AttributeError):
        return set()
    if not isinstance(group, dict):
        return set()
    def owned(name, entry):
        argv = entry.get('command') if isinstance(entry.get('command'), list) else entry.get('args')
        return isinstance(argv, list) and len(argv) >= 3 and argv[-3:] == [hub, 'mcp-run', name]
    return {re.sub('[^a-z0-9-]+', '-', str(name).lower()).strip('-') for name, entry in group.items()
            if not (isinstance(entry, dict) and owned(name, entry))}


def run_server(root, name):
    from gateway_adapter import VARIABLE, launch_settings, unset_references
    from gateway_render import render_server
    from mcp_endpoint import entries as catalog, read_server
    from store import FileStore
    root = Path(root)
    found = [read_server(root, entry) for entry in catalog(root, 'mcp_servers') if entry['name'] == name]
    servers = {name: render_server(found[0], root)} if found else {}
    config = launch_settings({'mcpServers': servers}, root)['mcpServers'].get(name)
    if not config or config.get('type') != 'stdio':
        raise ValueError(f'{name} is not a stdio MCP server in the catalog')
    store = FileStore(root)
    missing = unset_references({'mcpServers': {name: config}}, store).get(name)
    if missing:
        raise ValueError(f'{name} needs ' + ', '.join('secret://' + item for item in missing) + '; add it to the private store (docs/mcp-auth.md)')

    def resolve(text):
        return VARIABLE.sub(lambda match: store.resolve('secret://' + bytes.fromhex(match.group(1)).decode()), text)
    environment = {**os.environ, **{key: resolve(value) for key, value in config.get('env', {}).items()}}
    command = resolve(config['command'])
    wanted = 'python3' if found[0].get('command') == 'python3' else command
    command = shutil.which(wanted, path=environment.get('PATH')) or command
    return subprocess.run([command, *(resolve(arg) for arg in config['args'])], env=environment).returncode


def group_key(agent):
    return 'mcp_servers' if agent == 'codex' else 'mcp' if agent == 'opencode' else 'servers' if agent == 'vscode' else 'mcpServers'


def served_by_gateway(root):
    import hub_ui
    result = hub_ui.gateway_connection(Path(root))
    served = result.get('up')
    if served is None:
        raise ValueError('--retire-direct could not list the servers the gateway serves; nothing changed')
    names = set(served) | {item['name'] for item in result.get('down', ())}
    return {name for name in served
            if not any(other != name and (other.startswith(name + '-') or name.startswith(other + '-')) for other in names)}


def client_token_matches(root, value):
    from gateway import TOKEN_SECRET
    from store import FileStore
    store = FileStore(Path(root))
    return bool(value) and store.has(TOKEN_SECRET) and \
        hmac.compare_digest(value.encode(), store.resolve('secret://' + TOKEN_SECRET).encode())


def identical_imports(root, home, platform, environment, served, path, agent, group):
    from checks.scanners.common import ERRORS, slug
    from checks.scanners.inventory import Inventory
    try:
        inventory = Inventory(home, (), platform, environment, lambda command: '', Path(root), live=False)
        for name, entry in group.items():
            if slug(name) in served:
                inventory.server(agent, path, name, entry, opencode=agent == 'opencode')
        items = [item for item in inventory.finish()['items'] if item['kind'] == 'mcp_server']
    except ERRORS:
        return set()
    same = {item['name'] for item in items if item['status'] == 'already_in_locker'}
    return same - {item['name'] for item in items if item['status'] != 'already_in_locker'}


def direct_entries(text, agent, retirable):
    from checks.scanners.common import slug
    value = (toml(text) if agent == 'codex' else json_object(text)) if text.strip() else {}
    group = value.get(group_key(agent), {})
    if not isinstance(group, dict):
        return []
    kept = {'coldlocker', 'coldlocker-gateway', *LEGACY_SERVERS}
    group = {name: entry for name, entry in group.items() if name not in kept}
    identical = retirable(agent, group)
    return [name for name in group if slug(name) in identical]


def retire_toml(text, names):
    expected = toml(text)
    for name in names:
        del expected['mcp_servers'][name]
    headers = list(table_headers(text)) + [(len(text), [])]
    chunks = []
    position = 0
    for (start, path), (end, _) in zip(headers, headers[1:]):
        if len(path) >= 2 and path[0] == 'mcp_servers' and path[1] in names:
            chunks.append(text[position:start])
            chunks.extend(line for line in text[start:end].splitlines(keepends=True) if line.lstrip().startswith('#'))
            position = end
    chunks.append(text[position:])
    result = ''.join(chunks)
    if toml(result) != expected:
        raise ValueError('unsupported TOML layout for --retire-direct; existing configuration preserved')
    return result


def retire_json(text, agent, names, *, owned=None):
    expected = json_object(text)
    key = group_key(agent)
    for name in names:
        del expected[key][name]
    begin = next(value for name, _, value, _ in json_members(text, text.index('{'))[0] if name == key)
    found, close = json_members(text, begin)
    spans = []
    kept = [member for member in found if member[0] not in names]
    for index, (name, start, _, end) in enumerate(found):
        if owned is not None:
            if name in owned:
                spans.append(owned[name])
            if index + 1 < len(found) and (name in names or kept and name == kept[-1][0]):
                comma = skip_space(text, end)
                spans.append((comma, comma + 1))
            continue
        if name not in names:
            continue
        if index + 1 < len(found):
            spans.append((start, found[index + 1][1]))
        else:
            spans.append((kept[-1][3], end) if kept else (begin + 1, close))
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    for start, end in reversed(merged):
        text = text[:start] + text[end:]
    if json_object(text) != expected:
        raise ValueError('unsupported JSON layout for --retire-direct; existing configuration preserved')
    return text


def merge_json(text, agent, servers):
    value = json_object(text) if text.strip() else {}
    if not isinstance(value, dict):
        raise ValueError('configuration must be an object')
    key = 'mcp' if agent == 'opencode' else 'servers' if agent == 'vscode' else 'mcpServers'
    group = value.setdefault(key, {})
    if not isinstance(group, dict):
        raise ValueError('invalid MCP collection')
    servers = preserve_environment_references(group, servers)
    servers = registration_aliases(group, servers)
    if all(group.get(k) == v for k, v in servers.items()):
        return text
    if not text.strip():
        return json.dumps({key: {**group, **servers}}, indent=2) + '\n'
    match = re.search(r'\n([ \t]+)"', text)
    unit = match[1] if match else None
    newline = '\r\n' if '\r\n' in text else '\n'
    root = text.index('{')
    members, _ = json_members(text, root)
    for name, _, begin, _ in members:
        if name == key:
            return splice_members(text, begin, servers, unit, newline)
    return splice_members(text, root, {key: servers}, unit, newline, inline=False)


def jsonc_only(path, agent):
    return agent == 'opencode' and not path.exists() and path.with_suffix('.jsonc').exists()


def opencode_pair(path, agent):
    alternative = path.with_suffix('.jsonc')
    return alternative if agent == 'opencode' and path.suffix == '.json' and path.exists() and alternative.exists() else None


def scratch_part(root):
    root = Path(root).resolve()
    scratch = next((part for part in root.parts if part in ('.scratch', '.worktrees')), None)
    if scratch:
        return scratch
    try:
        if (root / '.git').is_file():
            return '.git'
    except OSError:
        pass
    return None


def syntax_error(path, agent):
    try:
        ordinary(path)
    except ValueError:
        return None
    if not path.exists():
        return None
    try:
        text = path.read_bytes().decode('utf-8')
        if agent == 'codex':
            toml(text)
        elif text.strip():
            json_object(text)
    except TomlUnavailable:
        raise
    except ValueError as error:
        return str(error)
    except OSError:
        return None
    return None


def warn(message, output):
    output.flush()
    print(message, file=sys.stderr)


def manual_toml(root, gateway):
    text = render(root, 'codex')
    if gateway.get('status') == 'running':
        text += '\n' + gateway['snippets']['Codex']
    return text


def display(path, home):
    try:
        return '~/' + path.relative_to(home).as_posix()
    except ValueError:
        return str(path)


class MigrationRefused(ValueError):
    pass


def migrate_registration_names(text, agent, policy_directory):
    if agent not in ('claude', 'codex'):
        raise ValueError('--migrate-names supports Claude and Codex only')
    value = (toml(text) if agent == 'codex' else json_object(text)) if text.strip() else {}
    key = 'mcp_servers' if agent == 'codex' else 'mcpServers'
    group = value.get(key, {})
    if not isinstance(group, dict):
        raise MigrationRefused('invalid MCP collection; existing configuration preserved')
    original = copy.deepcopy(group)
    registration_aliases(group, {})
    aliases = {old: new for old, new in LEGACY_SERVERS.items() if old in group}
    if not aliases:
        return text
    policies = [json.dumps({k: v for k, v in value.items() if k != key})]
    if agent == 'claude':
        for name in ('settings.json', 'settings.local.json'):
            policy = policy_directory / name
            ordinary(policy)
            if policy.exists():
                data = json_object(policy.read_text(encoding='utf-8'))
                policies.append(json.dumps({k: v for k, v in data.items() if k != 'hooks'}))
    for old in aliases:
        pattern = r'mcp__' + re.escape(old) + r'(?=__|["*])|["\s]' + re.escape(old) + r'(?=["_*])'
        if any(re.search(pattern, policy) for policy in policies):
            raise MigrationRefused('legacy MCP name is referenced by client policy; reconcile permissions before --migrate-names')
    edits = []
    if agent == 'codex':
        for start, parts in table_headers(text):
            if len(parts) >= 2 and parts[0] == key and parts[1] in aliases:
                end = text.find('\n', start)
                end = len(text) if end == -1 else end
                header = text[start:end]
                header = re.sub(r'(\.\s*)' + r'(?:' + re.escape(parts[1]) + r'|[\"\']' + re.escape(parts[1]) + r'[\"\'])'
                                + r'(?=\s*[.\]])', lambda match: match[1] + aliases[parts[1]], header, count=1)
                edits.append((start, end, header))
    else:
        for name, _, begin, _ in json_members(text, text.index('{'))[0]:
            if name == key:
                for name, start, _, _ in json_members(text, begin)[0]:
                    if name in aliases:
                        _, end = json.JSONDecoder().raw_decode(text, start)
                        edits.append((start, end, json.dumps(aliases[name])))
    for start, end, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end:]
    if agent == 'codex':
        expected = {aliases.get(name, name): server for name, server in original.items()}
        try:
            migrated = toml(text).get(key)
        except ValueError:
            migrated = None
        if migrated != expected:
            raise MigrationRefused('legacy MCP registration is not a [mcp_servers.<name>] table; rename it manually')
    return text


def plan(path, agent, servers, migrate_names=False, policy_directory=None, served=None):
    ordinary(path)
    if jsonc_only(path, agent):
        raise ValueError('OpenCode JSONC requires manual merge; existing configuration preserved')
    before = path.read_bytes() if path.exists() else None
    text = before.decode('utf-8') if before is not None else ''
    if migrate_names:
        text = migrate_registration_names(text, agent, policy_directory or path.parent / '.claude')
    after = merge_toml(text, servers) if agent == 'codex' else merge_json(text, agent, servers)
    retired = direct_entries(after, agent, served) if served and 'coldlocker-gateway' in servers else []
    if retired:
        after = retire_toml(after, retired) if agent == 'codex' else retire_json(after, agent, retired)
    return path, before, after.encode('utf-8'), retired


def digest(data):
    return hashlib.sha256(data).hexdigest()


def client_document(text, agent):
    value = (toml(text) if agent == 'codex' else json_object(text)) if text.strip() else {}
    if not isinstance(value, dict):
        raise ValueError('expected a client configuration object')
    return value


def owned_spans(text, agent, names):
    group = client_document(text, agent).get(group_key(agent), {})
    if not isinstance(group, dict) or not set(names) <= group.keys():
        raise ValueError('owned registration is missing or modified; refusing undo')
    spans = {}
    if agent == 'codex':
        for start, path in table_headers(text):
            if len(path) != 2 or path[0] != 'mcp_servers' or path[1] not in names:
                continue
            name = path[1]
            newline = '\r\n' if text.startswith('[mcp_servers.' + name + ']\r\n', start) else '\n'
            block = render_toml({name: group[name]}).replace('\n', newline)
            if text.startswith(block, start):
                spans[name] = (start, start + len(block))
    else:
        for key, _, begin, _ in json_members(text, text.index('{'))[0]:
            if key == group_key(agent):
                for name, start, _, end in json_members(text, begin)[0]:
                    if name in names:
                        line = text.rfind('\n', 0, start) + 1
                        if not text[line:start].strip():
                            start = line
                        spans[name] = (start, end)
    if set(spans) != set(names):
        raise ValueError('owned registration is missing or modified; refusing undo')
    return spans


def entry_digest(text, spans):
    return digest(b'\0'.join(text[start:end].encode() for _, (start, end) in sorted(spans.items())))


def undo_arguments(agent, change):
    path, before, after, retired = change
    flags = [agent, str(path), digest(after)]
    key = group_key(agent)
    previous = client_document((before or b'').decode(), agent).get(key, {})
    text = after.decode()
    current = client_document(text, agent)[key]
    if not retired and all(name in current and current[name] == entry for name, entry in previous.items()):
        names = sorted(current.keys() - previous.keys())
        if names:
            spans = owned_spans(text, agent, names)
            flags += ['--entries', entry_digest(text, spans), *names]
    return flags


def disconnect(args):
    path = Path(args.config)
    allowed = {Path(paths[AGENTS.index(args.agent)]).name for paths in PATHS.values()}
    if args.agent == 'opencode':
        allowed.add('opencode.jsonc')
    if path.name not in allowed:
        raise ValueError('unexpected client config filename; refusing undo')
    ordinary(path)
    current = path.read_bytes()
    if args.backup and digest(current) == args.written:
        backup, expected = args.backup
        backup = Path(backup)
        if backup.parent != path.parent or not re.fullmatch(
                re.escape(path.name) + r'\.\d{8}T\d{12}Z\.bak', backup.name):
            raise ValueError('unexpected connect-time backup path; refusing undo')
        ordinary(backup)
        result = backup.read_bytes()
        if digest(result) != expected:
            raise ValueError('connect-time backup changed; refusing undo')
        description = 'restore the pre-connect config'
    else:
        if not args.entries:
            raise ValueError('config changed after connect replaced, retired or migrated registrations; refusing undo')
        signature, *names = args.entries
        text = current.decode()
        spans = owned_spans(text, args.agent, names)
        if not names or entry_digest(text, spans) != signature:
            raise ValueError('owned registration was modified; refusing undo')
        if args.agent == 'codex':
            expected = toml(text)
            for name in names:
                del expected['mcp_servers'][name]
            for start, end in sorted(spans.values(), reverse=True):
                text = text[:start] + text[end:]
            remaining = toml(text)
            if not expected['mcp_servers'] and 'mcp_servers' not in remaining:
                del expected['mcp_servers']
            if remaining != expected:
                raise ValueError('owned TOML registration was modified; refusing undo')
        else:
            text = retire_json(text, args.agent, names, owned=spans)
        result = text.encode()
        description = 'remove ' + ', '.join(names)
    if args.apply:
        save((path, current, result), backup_existing=False)
        print(f'Undo applied: {path}')
    else:
        print(f'Preview: {description} in {path}. Append --apply to this command to confirm.')
    return 0


def file_snapshot(path):
    try:
        with path.open('rb') as stream:
            identity = os.fstat(stream.fileno())
            return stream.read(), (identity.st_dev, identity.st_ino)
    except FileNotFoundError:
        return None, None


def save(change, *, backup_existing=True):
    path, before, after = change[:3]
    ordinary(path)
    current, identity = file_snapshot(path)
    if current != before:
        raise ValueError('destination changed since preview')
    if before == after:
        return None
    backup = None
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            if before is not None:
                os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
            stream.write(after)
            stream.flush()
            os.fsync(stream.fileno())
        ordinary(path)
        latest, latest_identity = file_snapshot(path)
        if latest != current or latest_identity != identity:
            raise ValueError('destination changed while preparing update')
        if backup_existing and before is not None:
            timestamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            candidate = path.with_name(path.name + '.' + timestamp + '.bak')
            descriptor = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            backup = candidate
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(before)
        ordinary(path)
        latest, latest_identity = file_snapshot(path)
        if latest != current or latest_identity != identity:
            raise ValueError('destination changed while preparing update')
        os.replace(temporary, path)
    except BaseException:
        if backup is not None:
            backup.unlink(missing_ok=True)
        raise
    finally:
        Path(temporary).unlink(missing_ok=True)
    return backup


def skill_read_problem(checkout, agent, path, skill):
    from mcp_endpoint import PROTOCOL_VERSIONS, URI_SCHEME

    if agent in ('hermes', 'windsurf'):
        return 'skill checks require a Coldlocker-managed stdio registration'
    try:
        ordinary(path)
        if opencode_pair(path, agent):
            return 'both OpenCode configs exist; keep only the active one before checking'
        text = path.read_text(encoding='utf-8')
        config = toml(text) if agent == 'codex' else json_object(text)
        group = config.get(group_key(agent), {})
        if not isinstance(group, dict):
            return 'invalid MCP collection in the client registration'
        (name, expected), = registration_aliases(group, entries(checkout, agent)).items()
        if group.get(name) != expected:
            return 'no matching Coldlocker stdio registration; rerun connect'
    except (OSError, ValueError):
        return 'cannot read the client registration; rerun connect'
    uri = f'{URI_SCHEME}skills/{skill}/SKILL.md'
    requests = [
        {'id': 1, 'method': 'initialize', 'params': {'protocolVersion': PROTOCOL_VERSIONS[0], 'capabilities': {},
                                                    'clientInfo': {'name': agent, 'version': '1'}}},
        {'method': 'notifications/initialized'},
        {'id': 2, 'method': 'resources/read', 'params': {'uri': uri}},
    ]
    command = expected['command']
    launch = command if isinstance(command, list) else [command, *expected['args']]
    try:
        probe = subprocess.run([launch[0], '-E', '-B', *launch[1:], '--no-call-log'],
                               input=''.join(json.dumps({'jsonrpc': '2.0', **request}) + '\n' for request in requests),
                               encoding='utf-8', capture_output=True, check=True, timeout=15)
        initialized, read = map(json.loads, probe.stdout.splitlines())
        content, = read['result']['contents']
        if initialized['result']['serverInfo']['name'] == 'coldlocker' and read['id'] == 2 \
                and content['uri'] == uri and content['text']:
            return None
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        pass
    return 'Coldlocker did not return the selected skill over stdio'


def run(args, root, env=None, stdin=None, output=None):
    stdin = sys.stdin if stdin is None else stdin
    output = sys.stdout if output is None else output
    skill = getattr(args, 'check_skill', None)
    if skill is not None:
        from checks.validators import name
        name(skill)
        if args.write or getattr(args, 'remote', False) or not args.agent or 'grok' in args.agent:
            raise ValueError('--check-skill requires local --agent selections and is read-only')
    import remote_connect
    if getattr(args, 'remote', False):
        return remote_connect.run(args, output, root)
    as_json = getattr(args, 'json', False)
    report = connect_local(args, root, env, stdin, sys.stderr if as_json else output)
    if as_json:
        print_json(report)
    return report['exit_code']


def connect_local(args, root, env, stdin, output):
    if getattr(args, 'url', None) is not None:
        raise ValueError('--url requires --remote')
    if set(args.agent or ()) - set((*AGENTS, 'grok')):
        raise ValueError('hosted clients require --remote')
    home = Path(args.home).absolute() if args.home else Path.home()
    environment = dict(os.environ if env is None else env)
    if args.home and env is None:
        for key in ('CODEX_HOME', 'CLAUDE_CONFIG_DIR', 'XDG_CONFIG_HOME', 'APPDATA'):
            environment.pop(key, None)
    retire = getattr(args, 'retire_direct', False)
    if retire and not args.write:
        raise ValueError('--retire-direct requires --write')
    found = detect(home, args.platform, environment)
    requested = set(args.agent or AGENTS)
    skill = getattr(args, 'check_skill', None)
    gateway = gateway_connection(Path(root)) if skill is None else {}
    if retire and gateway.get('status') != 'running':
        raise ValueError('--retire-direct needs a running gateway; nothing changed')
    if retire and gateway.get('backend') == 'executor':
        raise ValueError('--retire-direct supports the built-in gateway only, not the Executor backend; nothing changed')
    if retire and gateway.get('tokenRequired') and not client_token_matches(root, environment.get(TOKEN_VARIABLE)):
        raise ValueError(f'--retire-direct needs {TOKEN_VARIABLE} set to the gateway token; nothing changed')
    served = served_by_gateway(root) if retire else None
    imported = imported_servers(root) if skill is None and gateway.get('status') != 'running' else {}
    windows = (args.platform or sys.platform).startswith('win')
    native = sorted(name for name, transport in imported.items()
                    if transport == 'stdio' and name not in ('coldlocker', 'coldlocker-gateway', *LEGACY_SERVERS)) if windows else []
    checkout = shown_root(root)
    scratch = scratch_part(root)
    if scratch:
        location = 'uses Git directory indirection (.git file)' if scratch == '.git' else f'is under {scratch}'
        print(f'WARNING: {checkout} {location}; deleting it breaks every connected client. Clone Coldlocker somewhere permanent and rerun.', file=output)
    detected = 0
    pending = []
    clients = {}
    for agent, path in config_paths(home, args.platform, environment).items():
        if agent not in requested or (agent not in found and not args.agent):
            continue
        missing = agent not in found or (agent == 'opencode' and not (path.exists() or path.is_symlink()) and not jsonc_only(path, agent))
        if jsonc_only(path, agent) and syntax_error(path.with_suffix('.jsonc'), agent) is None:
            path = path.with_suffix('.jsonc')
        detected += not missing
        result = clients[agent] = {'path': str(path), 'status': 'preview'}
        if skill is not None:
            problem = skill_read_problem(checkout, agent, path, skill)
            result.update(skill=skill, transport='stdio', status='failed' if problem else 'verified')
            if problem:
                result['reason'] = problem
                warn(f'{agent}: {problem}', output)
            else:
                print(f'{LABELS[agent]} read {skill} over stdio.', file=output)
            continue
        own = [name for name in native if name not in foreign_servers(path, agent, str(Path(os.path.abspath(checkout)) / 'hub.py'))]
        try:
            if agent not in found:
                print(f'{LABELS[agent]} — not detected; to add it, install and sign in to the client, then rerun connect. Expected config: {path}', file=output)
                result.update(status='not_detected', settings=render(checkout, agent, gateway, native=own))
                print(result['settings'], file=output)
                continue
            if agent in ('hermes', 'windsurf'):
                result.update(status='manual', settings=render(checkout, agent, gateway, native=own))
                print(f'{LABELS[agent]} — {path}', file=output)
                print(result['settings'], file=output)
                note = ('YAML cannot be merged safely without a YAML parser; merge this top-level mcp_servers block manually.'
                        if agent == 'hermes' else
                        'Windsurf is print-only because its current official documentation redirects to Devin Cascade documentation and does not verify the Windsurf user path; merge manually.')
                print(f'{LABELS[agent]} is print-only: {note}', file=output)
                if args.write:
                    warn(f'{agent}: --write is unsupported for this client; {display(path, home)} left unchanged', output)
                    result.update(status='failed', reason='write_unsupported')
                continue
            problem = syntax_error(path, agent)
            if problem is not None:
                kind = 'TOML' if agent == 'codex' else 'JSON'
                warn(f'{agent}: {display(path, home)} is not valid {kind} ({problem}); fix it and rerun', output)
                result.update(status='failed', reason='invalid_config')
                continue
            result['settings'] = render(checkout, agent, gateway, native=own)
            if missing:
                print(f'{LABELS[agent]} — not detected; to add it, create {path} with:', file=output)
                result['status'] = 'not_detected'
                print(result['settings'], file=output)
                continue
            print(f'{LABELS[agent]} — {path}', file=output)
            print(result['settings'], file=output)
            if agent in ('claude', 'codex', 'opencode'):
                print('Skills: ' + SKILLS_REACH + '. Run: ' + python_command() + ' ' + quote_path(checkout / 'hub.py') + ' link --target <a project folder>', file=output)
            pair = opencode_pair(path, agent)
            if args.write and pair:
                warn(f'OpenCode: both {path} and {pair} exist and the loaded one is not documented; refusing to write either. Merge manually or remove one.', output)
                result.update(status='failed', reason='ambiguous_config')
            elif args.write and jsonc_only(path, agent):
                result['status'] = 'manual'
                print('OpenCode: manual merge (jsonc) - paste this snippet into ' + str(path.with_suffix('.jsonc')), file=output)
            elif args.write:
                change = plan(path, agent, entries(checkout, agent, gateway, native=own), getattr(args, 'migrate_names', False),
                              Path(environment.get('CLAUDE_CONFIG_DIR') or home / '.claude'),
                              served and (lambda agent, group, path=path: identical_imports(
                                  root, home, args.platform, environment, served, path, agent, group)))
                if change[1] == change[2]:
                    result['status'] = 'already_connected'
                    print('already connected', file=output)
                else:
                    print('@@ ' + agent + ': add/replace only ' + ', '.join(entries(checkout, agent, gateway, native=own)) + ' (timestamped .bak of an existing file before atomic replace)', file=output)
                    if change[3]:
                        print('@@ ' + agent + ': remove direct entries the gateway now serves: ' + ', '.join(change[3]), file=output)
                    pending.append((agent, change))
        except TomlUnavailable as error:
            print(f'{LABELS[agent]} — add by hand to {path}:', file=output)
            print(manual_toml(checkout, gateway), file=output)
            warn(f'{agent}: {error}; {display(path, home)} left unchanged, add the block printed above by hand', output)
            result.update(status='failed' if agent in found else 'manual', reason='toml_unavailable')
        except MigrationRefused as error:
            warn(f'{agent}: {error}; {display(path, home)} left unchanged', output)
            result.update(status='failed', reason='migration_refused')
    if skill is not None:
        return {'exit_code': int(any(item['status'] == 'failed' for item in clients.values())), 'clients': clients}
    if not detected:
        print('No matching configured agents detected.', file=output)
    remote = sorted(name for name, transport in imported.items() if transport != 'stdio')
    if windows and remote:
        print('Not written on Windows: ' + ', '.join(remote) + ' (not stdio); add each in the client by hand, see docs/clients.md.', file=output)
    if imported and not windows and gateway.get('backend') != 'executor':
        print('Use the coldlocker entry to browse the catalog while the gateway is stopped.', file=output)
        print('Imported MCP servers (' + ', '.join(sorted(imported)) + ') reach other agents only through the gateway, which is not running. '
              'Start it (Node 20+), then rerun this command: ' + python_command() + ' gateway.py install && ' + gateway['command'], file=output)
    if not args.agent or 'grok' in requested:
        print('Hosted Bot: follow docs/hosted-bot-bootstrap.md; enable the hosted skill per bot.', file=output)
    if 'grok' in requested:
        clients['grok'] = {'status': 'manual', 'guide': 'docs/hosted-bot-bootstrap.md'}
    if pending and not args.yes:
        status = None
        if not stdin.isatty():
            print('Refusing --write without --yes or interactive confirmation.', file=output)
            status = 'refused'
        else:
            print('Write these backed-up connections? [y/N] ', end='', file=output, flush=True)
            if stdin.readline().strip().lower() not in ('y', 'yes'):
                print('Cancelled; no settings changed.', file=output)
                status = 'cancelled'
        if status:
            for agent, _ in pending:
                clients[agent]['status'] = status
            return {'exit_code': 1, 'clients': clients}
    for agent, change in pending:
        result = clients[agent]
        try:
            flags = undo_arguments(agent, change)
            backup = save(change)
        except (OSError, ValueError) as error:
            warn(f'{agent}: could not write {display(change[0], home)} ({getattr(error, "strerror", None) or error})', output)
            result.update(status='failed', reason='write_failed')
            continue
        print('Connected: ' + str(change[0]), file=output)
        if backup:
            print('Backup: ' + str(backup), file=output)
            flags.extend(['--backup', str(backup), digest(change[1])])
        command = [str(sys.executable), str(checkout / 'hub.py'), 'disconnect', *flags]
        result.update(status='connected', backup=str(backup) if backup else None,
                      undo=' '.join(quote_path(arg) for arg in command))
        print('Undo: ' + result['undo'], file=output)
    return {'exit_code': int(any(item['status'] == 'failed' for item in clients.values())), 'clients': clients}


def cli(args, root):
    return run(args, root)


def register(commands, root):
    command = commands.add_parser('connect', help='Print connections for detected agents; --write opts into backed-up config merges.')
    command.add_argument('--home')
    command.add_argument('--platform', choices=tuple(PATHS))
    from remote_connect import AGENTS as REMOTE_AGENTS
    command.add_argument('--agent', choices=tuple(dict.fromkeys((*AGENTS, *REMOTE_AGENTS))), nargs='+', action='extend')
    command.add_argument('--remote', action='store_true', help='Print remote OAuth catalog instructions; never reads or writes client settings.')
    command.add_argument('--url', help='Override public_url for --remote with your HTTPS catalog /mcp URL.')
    command.add_argument('--migrate-names', action='store_true', help='With --write, rename legacy Claude/Codex MCP registrations after checking client policy references.')
    command.add_argument('--write', action='store_true')
    command.add_argument('--retire-direct', action='store_true', help='With --write, also remove direct MCP entries that the running gateway now serves.')
    command.add_argument('--yes', action='store_true')
    command.add_argument('--json', action='store_true', help='Print versioned per-client results.')
    command.add_argument('--check-skill', metavar='NAME', help='Read a skill through selected local client stdio registrations.')
    command.set_defaults(action=lambda args: cli(args, root))
    command = commands.add_parser('disconnect', help='Preview the Undo command printed by connect; append --apply to write.')
    command.add_argument('agent', choices=AGENTS)
    command.add_argument('config')
    command.add_argument('written', help='SHA-256 of the config written by connect')
    command.add_argument('--entries', nargs='+', metavar='HASH_AND_NAMES')
    command.add_argument('--backup', nargs=2, metavar=('PATH', 'SHA256'))
    command.add_argument('--apply', action='store_true')
    command.set_defaults(action=disconnect)
    command = commands.add_parser('mcp-run', help='Run an imported stdio MCP server with its secret:// references resolved; connect writes this on Windows.')
    command.add_argument('name')
    command.set_defaults(action=lambda args: run_server(root, args.name))
