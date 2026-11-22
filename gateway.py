from contextlib import contextmanager
from datetime import datetime, timezone
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request

from gateway_adapter import BindingRefresher, LOOPBACK, SKIPPED_NEXT_STEP, VERSION, drop_unset, environment, launch_settings, prepare, private_directory, redact_log, save, skipped, sync, ui_bindings
from checks.validators import NAME
from portable import lock as acquire, python_command
from store import FileStore, QuietParser, ordinary_path
import executor_connector


ROOT = Path(__file__).resolve().parent
PORT = 43871
RUNTIME_PINS = {'@modelcontextprotocol/client': '2.3.0', 'hono': '4.13.16', '@hono/node-server': '2.1.6',
                'follow-redirects': '1.16.2'}
UPGRADE_MINIMUMS = {'@modelcontextprotocol/client': (2, 0, 0), 'hono': (4, 12, 4), '@hono/node-server': (1, 19, 9),
                    'follow-redirects': (1, 16, 0)}
REFRESH_INTERVAL = 30
REFRESH_LEAD = 60
STARTUP_SECONDS = 90
TOKEN_SECRET = 'gateway-token'
TOKEN_VARIABLE = 'COLDLOCKER_GATEWAY_TOKEN'
TOKEN_KEY = 'coldlocker-gateway'
BOOTSTRAP = """import http from 'node:http';
import net from 'node:net';
import {pathToFileURL} from 'node:url';
const env = name => process.env[name] ?? process.env[name.replace(/^COLDLOCKER_/, 'AGENTLOCKER_')];
const bind = env('COLDLOCKER_BIND');
const port = process.env.PORT;
const authorities = new Set(['127.0.0.1', 'localhost', '[::1]', bind.includes(':') ? '[' + bind + ']' : bind]
  .map(host => (host + ':' + port).toLowerCase()));
const permitted = req => {
  const host = req.headers.host;
  if (typeof host !== 'string' || !authorities.has(host.toLowerCase())) return false;
  const origin = req.headers.origin;
  return origin === undefined || (typeof origin === 'string' && origin.toLowerCase().startsWith('http://') && authorities.has(origin.slice(7).toLowerCase()));
};
const refusal = 'HTTP/1.1 403 Forbidden\\r\\nConnection: close\\r\\nContent-Length: 0\\r\\n\\r\\n';
const emit = http.Server.prototype.emit;
http.Server.prototype.emit = function(event, req, target, ...rest) {
  if (['request', 'checkContinue', 'checkExpectation'].includes(event) && !permitted(req)) {
    target.writeHead(403, {'Content-Type': 'text/plain', 'Connection': 'close'});
    target.end('Forbidden: Host or Origin is not an allowed loopback authority\\n');
    return true;
  }
  if (['upgrade', 'connect'].includes(event) && !permitted(req)) {
    target.end(refusal);
    return true;
  }
  return emit.call(this, event, req, target, ...rest);
};
const listen = net.Server.prototype.listen;
net.Server.prototype.listen = function(port, ...args) {
  if (typeof port !== 'number' && typeof port !== 'string') throw Error('Unsupported listener');
  return listen.call(this, Number(port), env('COLDLOCKER_BIND'), ...args.filter(x => typeof x === 'function'));
};
await import(pathToFileURL(env('COLDLOCKER_ENTRY')).href);
"""


class SetupError(ValueError):
    pass


def install_command():
    return python_command() + ' gateway.py install'


def require_supported():
    if sys.platform not in ('linux', 'darwin'):
        raise SetupError('the gateway runs on Linux and macOS only; on this system skip it and use the stdio route '
                         f'({python_command()} hub.py connect without the gateway)')


def prerequisite(name):
    if not shutil.which(name):
        raise SetupError(f'{name} is required; install it before retrying')


def check_node():
    prerequisite('node')
    result = subprocess.run(['node', '--version'], check=True, capture_output=True, text=True, encoding='utf-8')
    try:
        major = int(result.stdout.strip().removeprefix('v').split('.')[0])
    except ValueError:
        raise SetupError('cannot determine Node version; Node 20+ is required') from None
    if major < 20:
        raise SetupError('Node 20+ is required; upgrade Node before retrying')


def runtime_package(prefix=None):
    prefix = ROOT / '.scratch/mcphub-runtime' if prefix is None else prefix
    return prefix / 'node_modules/@samanhappy/mcphub'


def check_mcphub(prefix=None):
    package = ordinary_path(runtime_package(prefix))
    if not (package / 'package.json').is_file():
        raise SetupError('runtime missing; run ' + install_command())
    if json.loads(ordinary_path(package / 'package.json').read_text(encoding='utf-8')).get('version') != VERSION:
        raise SetupError('runtime version refused; expected MCPHub ' + VERSION)
    if not ordinary_path(package / 'dist/index.js').is_file():
        raise SetupError('runtime incomplete; reinstall the pinned MCPHub package')


def runtime_package_versions(prefix=None):
    prefix = ROOT / '.scratch/mcphub-runtime' if prefix is None else prefix
    versions = {}
    for name in RUNTIME_PINS:
        paths = [prefix / 'node_modules' / name / 'package.json',
                 *runtime_package(prefix).glob('**/node_modules/' + name + '/package.json')]
        versions[name] = []
        for path in paths:
            path = ordinary_path(path)
            try:
                version = json.loads(path.read_text(encoding='utf-8')).get('version')
            except (OSError, ValueError, TypeError, AttributeError):
                raise SetupError('runtime package missing or unreadable: ' + name +
                                 '; preserve the runtime before installing the tracked pins') from None
            if not isinstance(version, str):
                raise SetupError('runtime package version is invalid: ' + name)
            versions[name].append(version)
    return versions


def check_runtime(prefix=None):
    check_mcphub(prefix)
    for name, versions in runtime_package_versions(prefix).items():
        if any(version != RUNTIME_PINS[name] for version in versions):
            raise SetupError('runtime package refused; expected ' + name + ' ' + RUNTIME_PINS[name] +
                             '; run ' + install_command() + ' to upgrade the pinned runtime')


def upgradeable(name, version):
    if version == RUNTIME_PINS[name]:
        return True
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        return False
    return UPGRADE_MINIMUMS[name] <= tuple(map(int, version.split('.'))) < tuple(map(int, RUNTIME_PINS[name].split('.')))


@contextmanager
def installation_lock(exclusive=True):
    path = ordinary_path(private_directory(ROOT / '.scratch') / 'mcphub-install.lock')
    with open(path, 'a', encoding='utf-8') as lock:
        os.chmod(path, 0o600)
        try:
            acquire(lock, exclusive=exclusive, wait=False)
        except BlockingIOError:
            raise SetupError('gateway runtime is busy; stop the gateway before installing or retry after installation') from None
        yield


def install_runtime():
    check_node()
    prerequisite('npm')
    prefix = ordinary_path(ROOT / '.scratch/mcphub-runtime')
    if prefix.exists():
        if not prefix.is_dir():
            raise SetupError('runtime path is not a directory; preserve it before installing the tracked pins')
        if ordinary_path(runtime_package() / 'package.json').exists():
            check_mcphub()
            versions = runtime_package_versions()
            if all(version == RUNTIME_PINS[name] for name, values in versions.items() for version in values):
                return
            if any(not upgradeable(name, version) for name, values in versions.items() for version in values):
                raise SetupError('unfamiliar runtime package version; preserve the runtime before installing the tracked pins')
    if state(ROOT / '.hub-store/gateway'):
        raise SetupError('stop the gateway before upgrading its runtime')
    previous = os.umask(0o077)
    try:
        scratch = private_directory(ROOT / '.scratch')
        staged = ordinary_path(scratch / ('mcphub-install-' + secrets.token_hex(12)))
        staged.mkdir(mode=0o700)
        env = environment({}, FileStore(ROOT))
        env['HOME'] = str(private_directory(staged / 'home'))
        env['npm_config_cache'] = str(private_directory(scratch / 'npm-cache'))
        for name in ('package.json', 'package-lock.json'):
            shutil.copyfile(ROOT / 'gateway' / name, staged / name)
        subprocess.run(['npm', 'ci', '--ignore-scripts', '--prefix', str(staged)], env=env, check=True)
        check_runtime(staged)
        if state(ROOT / '.hub-store/gateway'):
            raise SetupError('stop the gateway before upgrading its runtime')
        backup = None
        if prefix.exists():
            backup = ordinary_path(scratch / ('mcphub-runtime.backup-' + secrets.token_hex(12)))
            if backup.exists():
                raise SetupError('runtime backup destination already exists; retry installation')
            prefix.rename(backup)
        try:
            staged.rename(prefix)
        except OSError:
            if backup is not None and not prefix.exists():
                backup.rename(prefix)
            raise
    finally:
        os.umask(previous)


def install():
    require_supported()
    with installation_lock():
        install_runtime()


def ps(pid, field, *, utc=False):
    env = dict(os.environ, LC_ALL='C')
    if utc:
        env['TZ'] = 'UTC'
    try:
        result = subprocess.run(['ps', '-ww', '-o', field + '=', '-p', str(pid)], capture_output=True, text=True,
                                env=env, encoding='utf-8', timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() or None


def identity(pid):
    if sys.platform != 'linux':
        return ps(pid, 'lstart')
    try:
        text = Path('/proc').joinpath(str(pid), 'stat').read_text(encoding='utf-8')
        return text.rsplit(')', 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def is_supervisor(pid, *, checkout=None):
    script = (ROOT if checkout is None else checkout) / 'gateway.py'
    if sys.platform != 'linux':
        interpreter, found, rest = (ps(pid, 'args') or '').partition(f" {script} _run")
        return bool(found) and rest[:1] in ('', ' ') and re.fullmatch(r'python[\w.]*', Path(interpreter).name, re.I) is not None
    try:
        argv = Path('/proc', str(pid), 'cmdline').read_bytes().decode().split('\0')
    except OSError:
        return False
    return argv[1:3] == [str(script), '_run'] and re.fullmatch(r'python[\w.]*', Path(argv[0]).name, re.I) is not None


def listeners(port):
    if sys.platform != 'linux':
        output = subprocess.run(['lsof', '-nP', f'-iTCP:{port}', '-sTCP:LISTEN', '-Fpn'],
                                capture_output=True, text=True, encoding='utf-8').stdout
        found, pid = [], None
        for line in output.splitlines():
            if line.startswith('p'):
                pid = int(line[1:])
            elif line.startswith('n') and pid is not None:
                found.append((line[1:], (pid,)))
        return found
    output = subprocess.run(['ss', '-H', '-ltnp', 'sport = :' + str(port)],
                            check=True, capture_output=True, text=True, encoding='utf-8').stdout
    return [(line.split()[3], tuple(int(pid) for pid in re.findall(r'pid=(\d+),', line)))
            for line in output.splitlines()]


def state(runtime, *, checkout=None):
    path = ordinary_path(runtime / 'pid.json')
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding='utf-8'))
    pid = value['pid']
    if type(pid) is not int or pid <= 1:
        raise ValueError('invalid process identity')
    current = identity(pid)
    if not current or current != value['identity']:
        return None
    return value if is_supervisor(pid, checkout=checkout) else None


def settings(runtime):
    return json.loads(ordinary_path(runtime / 'mcp_settings.json').read_text(encoding='utf-8'))


def child_environment(runtime_settings, store, runtime, port, package, dashboard=False):
    env = environment(runtime_settings, store)
    env.update(PORT=str(port), DISABLE_WEB='false' if dashboard else 'true', NODE_ENV='production',
               MCPHUB_SETTING_PATH=str(runtime / 'mcp_settings.json'),
               COLDLOCKER_BIND=LOOPBACK, COLDLOCKER_ENTRY=str(package / 'dist/index.js'),
               HOME=str(private_directory(runtime / 'home')))
    env['ADMIN_PASSWORD'] = store.resolve('secret://gateway-admin')
    env['JWT_SECRET'] = store.resolve('secret://gateway-jwt')
    return env


def token_value(store):
    if not store.has(TOKEN_SECRET):
        store.put(TOKEN_SECRET, secrets.token_hex(32))
    return store.resolve('secret://' + TOKEN_SECRET)


def apply_token(runtime, store, required):
    current = settings(runtime)
    keys = [key for key in current.get('bearerKeys', []) if key.get('id') not in (TOKEN_KEY, 'agentlocker-gateway')]
    owned = len(keys) != len(current.get('bearerKeys', []))
    routing = current.setdefault('systemConfig', {}).setdefault('routing', {})
    if required:
        keys.append({'id': TOKEN_KEY, 'name': TOKEN_KEY, 'token': token_value(store),
                     'enabled': True, 'kind': 'system', 'accessType': 'all',
                     'allowedGroups': [], 'allowedServers': []})
        routing['enableBearerAuth'] = True
    elif owned:
        routing['enableBearerAuth'] = False
    current['bearerKeys'] = keys
    save(runtime / 'mcp_settings.json', current)


def client_headers(root, running):
    if not running or not running.get('require_token'):
        return {}
    return {'Authorization': 'Bearer ' + token_value(FileStore(root))}


def write_token_file(root, destination):
    token = token_value(FileStore(root))
    path = ordinary_path(destination)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
        for name in (TOKEN_VARIABLE, 'AGENTLOCKER_GATEWAY_TOKEN'):
            handle.write(name + '=' + token + '\n')


def supervise(port, auto_refresh=False, dashboard=False, require_token=False):
    os.umask(0o077)
    runtime = ROOT / '.hub-store/gateway'
    with open(ordinary_path(runtime / 'supervisor.lock'), 'a', encoding='utf-8') as lock:
        os.chmod(runtime / 'supervisor.lock', 0o600)
        acquire(lock, wait=False)
        run_supervisor(runtime, port, auto_refresh, dashboard, require_token)


@installation_lock(exclusive=False)
def run_supervisor(runtime, port, auto_refresh, dashboard=False, require_token=False):
    store = FileStore(ROOT)
    check_node()
    check_runtime()
    package = runtime_package()
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    bootstrap = ordinary_path(runtime / 'bootstrap.mjs')
    bootstrap.write_text(BOOTSTRAP, encoding='utf-8')
    bootstrap.chmod(0o600)
    for name in ('gateway-admin', 'gateway-jwt'):
        if not store.has(name):
            store.put(name, secrets.token_hex(32))
    apply_token(runtime, store, require_token)
    log_lock = threading.Lock()
    known = set()
    with open(ordinary_path(runtime / 'gateway.log'), 'a', encoding='utf-8') as log:
        os.chmod(runtime / 'gateway.log', 0o600)

        def remember():
            with log_lock:
                known.update(part for name in store.names()
                             for part in store.resolve('secret://' + name).splitlines() if part)

        def diagnostic(line):
            with log_lock:
                try:
                    try:
                        import calllog
                        calllog.gateway_request(ROOT, line)
                    except (OSError, ValueError, KeyError, TypeError, AttributeError):
                        pass
                    line = redact_log(store, line)
                    for value in sorted(known, key=len, reverse=True):
                        line = line.replace(value, '[REDACTED]')
                    log.write(line)
                except (OSError, ValueError, KeyError, TypeError):
                    log.write('[gateway diagnostic suppressed]\n')
                log.flush()

        def output(child):
            for raw in child.stdout:
                try:
                    sync(settings(runtime), store)
                    diagnostic(raw.decode(errors='replace'))
                except (OSError, ValueError, KeyError, TypeError):
                    diagnostic('[gateway diagnostic suppressed]\n')

        def launch():
            from verify import gateway_fingerprints
            source_root = Path(json.loads(ordinary_path(runtime / 'source.json').read_text(encoding='utf-8'))['path'])
            fingerprints = gateway_fingerprints(ROOT, settings(runtime)) if source_root == ROOT else {}
            env = child_environment(settings(runtime), store, runtime, port, package, dashboard)
            remember()
            child = subprocess.Popen(['node', str(bootstrap)], cwd=runtime, env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            reader = threading.Thread(target=output, args=(child,), daemon=True)
            reader.start()
            save(runtime / 'pid.json', {'verification_hashes': fingerprints, 'pid': os.getpid(), 'identity': identity(os.getpid()),
                                       'child': child.pid, 'port': port, 'auto_refresh': auto_refresh,
                                       'dashboard': dashboard, 'require_token': require_token})
            return child, reader

        def reap(child, reader):
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
            child.wait()
            reader.join(timeout=5)
            child.stdout.close()

        refresher = None
        if auto_refresh:
            source = Path(json.loads(ordinary_path(runtime / 'source.json').read_text(encoding='utf-8'))['path'])
            refresher = BindingRefresher(source, store, REFRESH_INTERVAL, REFRESH_LEAD, diagnostic, stopped.is_set)
        process, reader = launch()
        try:
            while not stopped.wait(REFRESH_INTERVAL if auto_refresh else .2):
                if process.poll() is not None:
                    break
                try:
                    remember()
                    if refresher and refresher.check(settings(runtime)):
                        remember()
                        diagnostic('[auto-refresh] header changed; restarting gateway on same port\n')
                        reap(process, reader)
                        if stopped.is_set():
                            break
                        process, reader = launch()
                except (OSError, ValueError, KeyError, TypeError):
                    diagnostic('[auto-refresh] check failed; retry on next interval\n')
            reap(process, reader)
            sync(settings(runtime), store)
        finally:
            reap(process, reader)
            (runtime / 'pid.json').unlink(missing_ok=True)


def stop(runtime):
    current = state(runtime)
    if not current:
        return
    os.kill(current['pid'], signal.SIGTERM)
    for _ in range(200):
        if not state(runtime):
            if listeners(current['port']):
                raise SetupError(f"gateway stopped but port {current['port']} is still listening")
            return
        time.sleep(.1)
    raise ValueError('stop timed out')


def require_free(port):
    with socket.socket() as probe:
        if os.name != 'nt':
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((LOOPBACK, port))
        except OSError:
            raise SetupError(f'Gateway port unavailable: {port} is busy. Leave its listener alone and use another port '
                             f'with: {python_command()} gateway.py start --port <port>') from None


def start(runtime, port, auto_refresh=False, dashboard=False, require_token=False):
    require_supported()
    with installation_lock(exclusive=False):
        return start_runtime(runtime, port, auto_refresh, dashboard, require_token)


def catalog_source(path):
    return Path(json.loads(path.read_text(encoding='utf-8'))['path'])


def add_catalog(runtime_settings, source):
    catalog = {'type': 'stdio', 'command': sys.executable,
               'args': ['-B', str(ROOT / 'mcp_endpoint.py'), '--root', str(source), '--no-call-log']}
    servers = runtime_settings['mcpServers']
    name = 'coldlocker-catalog'
    suffix = 1
    while name in servers and servers[name].get('args') != catalog['args']:
        suffix += 1
        name = f'coldlocker-catalog-copy{suffix}'
    servers.setdefault(name, {}).update(catalog)


def start_runtime(runtime, port, auto_refresh=False, dashboard=False, require_token=False):
    if state(runtime):
        raise ValueError('already running')
    if not (runtime / 'mcp_settings.json').exists():
        raise SetupError(f'run {python_command()} gateway.py prepare first')
    check_node()
    prerequisite('ss' if sys.platform == 'linux' else 'lsof')
    check_runtime()
    require_free(port)
    source_path = ordinary_path(runtime / 'source.json')
    if source_path.exists():
        source = catalog_source(source_path)
        store = FileStore(ROOT)
        updated = drop_unset(ui_bindings(settings(runtime), source, store), store)
        updated = launch_settings(updated, source)
        add_catalog(updated, source)
        save(runtime / 'mcp_settings.json', updated)
    arguments = [sys.executable, str(ROOT / 'gateway.py'), '_run', '--port', str(port)]
    if auto_refresh:
        arguments.append('--auto-refresh')
    if dashboard:
        arguments.append('--dashboard')
    if require_token:
        arguments.append('--require-token')
    process = subprocess.Popen(arguments,
                               cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, start_new_session=True)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        deadline = time.monotonic() + STARTUP_SECONDS
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise ValueError('startup failed')
            if state(runtime):
                try:
                    with opener.open(f'http://{LOOPBACK}:{port}/health', timeout=.3) as response:
                        if response.status == 200:
                            current = state(runtime)
                            found = listeners(port)
                            if current and found and all(
                                    address == f'{LOOPBACK}:{port}' and current['child'] in pids
                                    for address, pids in found):
                                return
                except OSError:
                    pass
            time.sleep(.1)
        raise ValueError('startup timed out')
    except BaseException:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=12)
        raise


def endpoint(port):
    return f'http://{LOOPBACK}:{port}/mcp'


def health_json(port, route, *, child=None, body=None, headers=None, limit=8192, statuses=(200,)):
    connection = http.client.HTTPConnection(LOOPBACK, port, timeout=5)
    try:
        if child is not None:
            connection.connect()
            found = listeners(port)
            if not found or any(address != f'{LOOPBACK}:{port}' or child not in pids for address, pids in found):
                raise ValueError('unverified gateway listener')
        connection.request('GET' if body is None else 'POST', route, body=body, headers=headers or {})
        response = connection.getresponse()
        data = response.read(limit + 1)
        if response.status not in statuses or len(data) > limit:
            raise ValueError('unexpected gateway response')
        return response.status, json.loads(data)
    finally:
        connection.close()


def upstream_reason(server):
    status = server['status']
    error = server.get('error') or ''
    if status == 'oauth_required' or re.search(r'\bstatus[:= ]+40[13]\b', error, re.I):
        return 'upstream authentication failed'
    if status == 'connecting':
        return 'connection in progress'
    if re.search(r'\b(?:ETIMEDOUT|timed out)\b', error, re.I):
        return 'connection timed out'
    return 'upstream connection failed'


def expired_oauth(root, runtime):
    from mcp_endpoint import entries, read_server
    from ui_auth import Authentication, entry_prefix

    source_path = ordinary_path(runtime / 'source.json')
    source = catalog_source(source_path) if source_path.exists() else root
    store = FileStore(root)
    authentication = Authentication(source, store)
    now = time.time()
    expired = []
    unavailable = False
    for entry in entries(source, 'mcp_servers'):
        name = entry['name']
        try:
            binding = authentication.metadata(name)
            if binding.get('method') != 'oauth' or 'expires_at' not in binding:
                continue
            expiry = binding['expires_at']
            if type(expiry) not in (int, float) or not 0 < expiry < float('inf'):
                raise ValueError('invalid OAuth expiry')
            server = read_server(source, entry)
            if server['transport'] == 'http' and binding['resource'] == server['url'] and expiry <= now:
                remediation = 'Reconnect this upstream on the Sign in screen.'
                if store.has(entry_prefix(name) + '-refresh'):
                    remediation = 'Run python3 gateway.py refresh, or reconnect on the Sign in screen if refresh fails.'
                expired.append({'name': name if store.redact(name) == name else '[redacted]',
                                'status': 'expired',
                                'remediation': remediation})
        except HEALTH_ERRORS:
            unavailable = True
    return sorted(expired, key=lambda item: item['name']), unavailable


def disconnected_upstreams(root, port, child, total, disconnected):
    if type(child) is not int or child <= 1:
        raise ValueError('invalid gateway listener identity')
    store = FileStore(root)
    _, login = health_json(port, '/api/auth/login', child=child,
                           body=json.dumps({'username': 'admin', 'password': store.resolve('secret://gateway-admin')}),
                           headers={'Content-Type': 'application/json'})
    token = login['token']
    if login['success'] is not True or not isinstance(token, str) or not re.fullmatch(r'[\w.-]+', token, re.ASCII):
        raise ValueError('invalid gateway dashboard token')
    _, response = health_json(port, '/api/servers', child=child,
                              headers={'x-auth-token': token}, limit=4 * 1024 * 1024)
    if response['success'] is not True or not isinstance(response['data'], list):
        raise ValueError('invalid gateway dashboard response')
    names = set()
    down = []
    for server in response['data']:
        config = server.get('config') or {}
        if (server['enabled'] is False or 'credentialTemplate' in config
                or (config.get('oauth') or {}).get('identityMode') == 'personal'):
            continue
        name = server['name']
        if not NAME.fullmatch(name) or name in names or server['enabled'] is not True:
            raise ValueError('invalid upstream record')
        names.add(name)
        if not isinstance(server['status'], str):
            raise ValueError('invalid upstream status')
        if server['status'] != 'connected':
            if store.redact(name) != name:
                name = '[redacted]'
            down.append(f'gateway upstream {name} disconnected ({upstream_reason(server)})')
    if len(names) != total or len(down) != disconnected:
        raise ValueError('upstream status changed during health check')
    return sorted(down)


def started_before_head(root, pid):
    start = ps(pid, 'lstart', utc=True)
    if not start:
        raise ValueError('gateway start time unavailable')
    result = subprocess.run(['git', 'show', '-s', '--format=%ct', 'HEAD'], cwd=root,
                            capture_output=True, text=True, timeout=5)
    commit = result.stdout.strip()
    if result.returncode or not re.fullmatch(r'[0-9]+', commit):
        raise ValueError('checkout HEAD time unavailable')
    started = datetime.strptime(start, '%a %b %d %H:%M:%S %Y').replace(tzinfo=timezone.utc)
    return started.timestamp() < int(commit)


HEALTH_ERRORS = (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError,
                 http.client.HTTPException, subprocess.SubprocessError)


def health_report(root, port, current):
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError('invalid gateway port')
    code, payload = health_json(port, '/health', statuses=(200, 503))
    status = payload['status']
    if status not in ('healthy', 'degraded', 'unhealthy'):
        raise ValueError('unknown health status')
    total, connected, disconnected = (payload['servers'][key] for key in ('total', 'connected', 'disconnected'))
    if any(type(count) is not int or count < 0 for count in (total, connected, disconnected)):
        raise ValueError('invalid gateway connection count')
    if connected + disconnected != total:
        raise ValueError('inconsistent gateway connection counts')
    summary = f'gateway {status} ({connected}/{total} connected, {disconnected} disconnected)'
    warnings = []
    current = current if current and current.get('port') == port else {}
    if disconnected:
        try:
            warnings.extend(disconnected_upstreams(root, port, current['child'], total, disconnected))
        except HEALTH_ERRORS:
            warnings.append('gateway disconnected upstream details unavailable')
    try:
        if started_before_head(root, current['pid']):
            warnings.append('gateway service started before checkout HEAD')
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        warnings.append('gateway service start time unavailable')
    return summary, code == 200 and status == 'healthy' and disconnected == 0, warnings


def servers_line():
    import hub_ui
    result = hub_ui.gateway_connection(ROOT)
    if 'total' not in result:
        return 'servers: unknown'
    line = f"servers: {result['served']}/{result['total']} connected"
    if result['down']:
        line += '; failed: ' + ', '.join(item['name'] for item in result['down'])
    if result.get('skipped'):
        line += f"; skipped: {len(result['skipped'])}"
    return line


def skipped_lines():
    try:
        found = skipped(ROOT, FileStore(ROOT))
    except (OSError, ValueError, KeyError, TypeError):
        return []
    return [f"skipped: {name} needs {', '.join('secret://' + ref for ref in refs)}; {SKIPPED_NEXT_STEP}"
            for name, refs in found.items()]


def startable():
    try:
        check_runtime()
    except SetupError:
        return False
    return True


@contextmanager
def lifecycle():
    runtime = private_directory(private_directory(ROOT / '.hub-store') / 'gateway')
    with open(ordinary_path(runtime / 'lifecycle.lock'), 'a', encoding='utf-8') as lock:
        os.chmod(runtime / 'lifecycle.lock', 0o600)
        acquire(lock)
        yield runtime


def launch():
    require_supported()
    check_runtime()
    with lifecycle() as runtime:
        if state(runtime):
            return
        require_free(PORT)
        prepare(ROOT, ROOT)
        start(runtime, PORT)


def enabled(source):
    get = source.get if isinstance(source, dict) else lambda name: getattr(source, name)
    return {name: True for name in ('dashboard', 'require_token') if get(name)}


def main():
    parser = QuietParser(description='Select a local gateway; Executor lifecycle stays user-managed',
                         epilog='Local clients can read the catalog. Use --require-token on shared machines.')
    parser.add_argument('command', choices=('install', 'prepare', 'start', 'stop', 'status', 'sync', 'refresh', 'url', 'register-executor', 'token', '_run'))
    parser.add_argument('--server')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--source', type=Path, default=ROOT)
    parser.add_argument('--port', type=int, default=PORT)
    parser.add_argument('--auto-refresh', action='store_true')
    parser.add_argument('--dashboard', action='store_true')
    parser.add_argument('--require-token', action='store_true')
    parser.add_argument('--env-file', type=Path)
    args = parser.parse_args()
    try:
        if not 1024 <= args.port <= 65535:
            raise ValueError('invalid port')
        if args.command == 'register-executor':
            from executor_sources import registration
            return registration(ROOT, args.server, args.apply)
        backend = executor_connector.configuration(ROOT)['backend']
        if backend == 'executor':
            return executor_connector.command(ROOT, args.command)
        require_supported()
        if args.command == 'url':
            current = state(ROOT / '.hub-store/gateway')
            if not current:
                raise ValueError('gateway is stopped')
            print(endpoint(current['port']))
            return 0
        if args.command == 'token':
            if not args.env_file:
                raise ValueError('--env-file is required')
            try:
                write_token_file(ROOT, args.env_file.resolve())
            except FileExistsError:
                raise SetupError('env file already exists; choose a new path') from None
            print('token written to ' + str(args.env_file.resolve()))
            return 0
        if args.command == '_run':
            supervise(args.port, args.auto_refresh, **enabled(args))
            return 0
        if args.command == 'install':
            install()
            print('gateway install complete; MCPHub ' + VERSION)
            return 0
        with lifecycle() as runtime:
            if args.command == 'prepare':
                if state(runtime):
                    raise ValueError('stop before prepare')
                prepare(ROOT, args.source.resolve())
            elif args.command == 'start':
                start(runtime, args.port, args.auto_refresh, **enabled(args))
            elif args.command == 'stop':
                stop(runtime)
            elif args.command == 'refresh':
                current = state(runtime)
                source = Path(json.loads(ordinary_path(runtime / 'source.json').read_text(encoding='utf-8'))['path'])
                if current:
                    stop(runtime)
                store = FileStore(ROOT)
                save(runtime / 'mcp_settings.json', drop_unset(ui_bindings(settings(runtime), source, store), store))
                if current:
                    start(runtime, current['port'], current.get('auto_refresh', False), **enabled(current))
            elif args.command == 'sync':
                print('OAuth servers synchronized:', sync(settings(runtime), FileStore(ROOT)))
            else:
                current = state(runtime)
                print('running' if current else 'stopped')
                if current:
                    print('url: ' + endpoint(current['port']))
        if args.command == 'status':
            if current:
                print(servers_line())
                for line in skipped_lines():
                    print(line)
            return 0 if current else 1
        print('gateway ' + args.command + ' complete')
        if args.command == 'start':
            print(servers_line())
            for line in skipped_lines():
                print(line)
        return 0
    except SetupError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        print('gateway operation failed; inspect protected diagnostics', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
