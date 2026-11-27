"""The whole test-session cycle against this machine's Docker, as the account running it (docker group):

    python3 tests/fixtures/test_session_e2e.py SETUP [--lock-file FILE] [--keep]

SETUP is a setup checkout with test-session-v2. In a temporary $ROOT: the fixture project (test_session_project.py) as
a build root with origin/HEAD, a branch whose Makefile would run on the host (it must not), then: a session from the
data set dev; a run with a failing test; the patch that fixes it; a cancel; snapshots (one leaving the site is refused);
the session's isolation (no published port, an internal network, Playwright bounded and not root); a stage run against
a local site on Docker's bridge; expiry and reconcile, after which nothing of the session is left. Prints each check
and exits 1 on the first that fails.
"""

import argparse
import http.server
import threading
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
PROGRAM = HERE.parents[1] / 'bin/test-session'
sys.path.insert(0, str(HERE))
import test_session_project as fixture  # noqa: E402


def check(condition, text, detail=''):
    print(('ok   ' if condition else 'FAIL ') + text + (f' — {detail}' if detail and not condition else ''), flush=True)
    if not condition:
        sys.exit(1)


def confined(name):
    """Every container of the session's Compose project as setup's session overlay leaves it."""
    ids = subprocess.run(['docker', 'ps', '-q', '--filter', f'label=com.docker.compose.project={name}'], capture_output=True,
                         text=True).stdout.split()
    check(bool(ids), 'the session has containers')
    details = json.loads(subprocess.run(['docker', 'inspect', *ids], capture_output=True, text=True).stdout)
    services = sorted(item['Config']['Labels'].get('com.docker.compose.service', '?') for item in details)
    check('metrics' not in services and 'cron' in services and 'backup' in services,
          f'services {services}: no metrics; cron and backup kept')
    networks = set()
    for item in details:
        host = item['HostConfig']
        label = item['Config']['Labels'].get('com.docker.compose.service')
        check('ALL' in (host.get('CapDrop') or []) and 'no-new-privileges:true' in (host.get('SecurityOpt') or [])
              and (host.get('PidsLimit') or 0) > 0 and host.get('Memory', 0) > 0 and host.get('MemorySwap') == host.get('Memory')
              and not host.get('PortBindings') and not host.get('Privileged')
              and not any('host-gateway' in entry or 'host.docker.internal' in entry for entry in host.get('ExtraHosts') or [])
              and not any('docker.sock' in mount.get('Source', '') for mount in item.get('Mounts', [])),
              f'{label}: no capabilities beyond the few, no-new-privileges, pids/memory/swap limits, no ports or socket')
        networks |= set(item['NetworkSettings']['Networks'])
    for network in sorted(networks):
        internal = subprocess.run(['docker', 'network', 'inspect', network, '--format', '{{.Internal}}'],
                                  capture_output=True, text=True).stdout.strip()
        check(internal == 'true', f'network {network} is internal', internal)


def image_of(name):
    return subprocess.run(['docker', 'image', 'ls', '--filter', 'label=setup.service=playwright', '--format',
                           '{{.Repository}}:{{.Tag}}'], capture_output=True, text=True).stdout.split()[0]


def gateway_check(name, php, image):
    """T2: a listener on 0.0.0.0 of this host is reachable at a plain internal network's gateway (the control, with this
    host's firewall opened for its port when sudo allows), and not from the session (its networks have no gateway on
    the host at all)."""
    listener = http.server.ThreadingHTTPServer(('0.0.0.0', 0), http.server.SimpleHTTPRequestHandler)
    port = listener.server_address[1]
    threading.Thread(target=listener.serve_forever, daemon=True).start()
    opened = subprocess.run(['sudo', '-n', 'iptables', '-I', 'INPUT', '-p', 'tcp', '--dport', str(port), '-j', 'ACCEPT'],
                            capture_output=True).returncode == 0
    control = f'ts-e2e-control-{port}'
    try:
        subprocess.run(['docker', 'network', 'create', '--internal', control], check=True, capture_output=True)
        gateway = subprocess.run(['docker', 'network', 'inspect', control, '--format', '{{(index .IPAM.Config 0).Gateway}}'],
                                 capture_output=True, text=True).stdout.strip()
        probe = "fetch(process.argv[1], {signal: AbortSignal.timeout(5000)}).then(r => console.log('reached')).catch(e => console.log('blocked'))"
        reached = subprocess.run(['docker', 'run', '--rm', '--network', control, '--entrypoint', 'node', image, '-e', probe,
                                  f'http://{gateway}:{port}/'], capture_output=True, text=True, timeout=60).stdout.strip()
        if opened:
            check(reached == 'reached', f'control: a plain internal network reaches the host at its gateway {gateway}:{port}',
                  reached)
        else:
            print(f'note control not shown: no sudo to open this host\'s firewall for {port} (it answered {reached})')
        networks = subprocess.run(['docker', 'network', 'ls', '-q', '--filter', f'label=com.docker.compose.project={name}'],
                                  capture_output=True, text=True).stdout.split()
        for network in json.loads(subprocess.run(['docker', 'network', 'inspect', *networks], capture_output=True,
                                                 text=True).stdout):
            subnet = network['IPAM']['Config'][0]['Subnet']
            first = subnet.split('/')[0].rsplit('.', 1)[0] + '.1'
            check(not any(item.get('Gateway') for item in network['IPAM']['Config']),
                  f'session network {network["Name"]} has no gateway address on the host')
            for target in (first, gateway):
                answer = subprocess.run(['docker', 'exec', php, 'php', '-r',
                                         f'echo @file_get_contents("http://{target}:{port}/", false, stream_context_create(["http" '
                                         '=> ["timeout" => 5]])) === false ? "blocked" : "reached";'],
                                        capture_output=True, text=True, timeout=60).stdout.strip()
                check(answer == 'blocked', f'the shop cannot reach a host listener on 0.0.0.0 at {target}:{port}', answer)
    finally:
        subprocess.run(['docker', 'network', 'rm', control], capture_output=True)
        if opened:
            subprocess.run(['sudo', '-n', 'iptables', '-D', 'INPUT', '-p', 'tcp', '--dport', str(port), '-j', 'ACCEPT'],
                           capture_output=True)
        listener.shutdown()


def new_file_patch(path, text):
    """A unified diff that adds a file (relative to app/)."""
    lines = text.splitlines()
    return (f'diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n'
            + ''.join(f'+{line}\n' for line in lines))


ESCAPE_SPEC = """import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';

for (const url of ['http://1.1.1.1/', 'https://example.com/', 'http://GATEWAY:9102/', 'http://169.254.169.254/']) {
  test(`cannot reach ${url}`, async () => {
    await expect(fetch(url, { signal: AbortSignal.timeout(8000) })).rejects.toThrow();
  });
}

test('filling /tmp ends at its tmpfs', async () => {
  const block = Buffer.alloc(16 * 1024 * 1024);
  let error = null;
  try { for (let i = 0; i < 100; i += 1) fs.writeFileSync(`/tmp/fill-${i}`, block); } catch (caught) { error = caught; }
  expect(error && error.code).toBe('ENOSPC');
});

test('the root filesystem is read-only', async () => {
  expect(() => fs.writeFileSync('/e2e/x', 'x')).toThrow();
});
"""
FILL_SPEC = """import { test } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';

test('fills the results directory', async () => {
  test.setTimeout(120000);
  const block = Buffer.alloc(16 * 1024 * 1024);
  for (let i = 0; i < 40; i += 1) fs.writeFileSync(path.join(process.env.PLAYWRIGHT_DATA_DIR, `fill-${i}`), block);
  await new Promise((resolve) => setTimeout(resolve, 60000));
});
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('setup')
    parser.add_argument('--lock-file')
    parser.add_argument('--keep', action='store_true', help='keep the temporary tree')
    options = parser.parse_args()
    base = Path(tempfile.mkdtemp(prefix='ts-e2e-'))
    root, config = base / 'root', base / 'config'
    (root / 'projects').mkdir(parents=True)
    config.mkdir()
    (root / 'setup').symlink_to(Path(options.setup).resolve())
    environment = {**os.environ, 'SETUP_SERVER_ROOT': str(root), 'SETUP_SERVER_CONFIG': str(config)}

    def ts(*arguments, stdin=None, expect=None):
        result = subprocess.run([sys.executable, str(PROGRAM), *arguments], input=json.dumps(stdin) if stdin is not None else '',
                                capture_output=True, text=True, env=environment, timeout=900)
        try:
            return json.loads(result.stdout)
        except ValueError:
            check(False, f'test-session {arguments[0]} answers JSON', result.stdout[-500:] + result.stderr[-500:])

    def wait_run(identifier, limit=900):
        for _ in range(limit // 3):
            run = ts('run-show', f'--id={identifier}')['run']
            if run['status'] not in ('queued', 'running', 'cancelling'):
                return run
            time.sleep(3)
        check(False, f'run {identifier} ends')

    (base / 'work').mkdir()
    work = fixture.build(base / 'work', options.setup, lock_file=options.lock_file, services='playwright metrics cron backup')
    # This machine shares its host with the sessions knowingly (a production host gets its own filesystem and daemon).
    (config / 'test-sessions.env').write_text('TEST_RUN_DISK_MB=200\nTEST_SESSIONS_SHARE_PRODUCTION_HOST=accept-risk\n')
    origin = base / 'origin.git'
    subprocess.run(['git', 'init', '-q', '--bare', '-b', 'master', str(origin)], check=True)
    fixture.git(work, 'remote', 'add', 'origin', str(origin))
    fixture.git(work, 'push', '-q', 'origin', 'master')
    fixture.git(work, 'checkout', '-q', '-b', 'evil')
    (work / 'app/Makefile').write_text(f'test-init build test-prepare e2e-install e2e:\n\ttouch {base}/pwned\n')
    (work / 'app/tests/playwright/fixme.spec.ts').write_text(fixture.SPECS['fixme.spec.ts'].replace("'broken'", "'ready'"))
    fixture.git(work, 'commit', '-q', '-am', 'a branch that would run code on the host')
    fixture.git(work, 'push', '-q', 'origin', 'evil')
    build = root / 'projects' / fixture.NAME
    subprocess.run(['git', 'clone', '-q', str(origin), str(build)], check=True)
    started = time.time()

    # A session of the untrusted branch: its specs, the deploy branch's Makefile.
    made = ts('create', f'--project={fixture.NAME}', '--db=dataset:dev', '--env-ref=evil', '--by=e2e')
    check('session' in made, 'create answers a session', made)
    session = made['session']
    for _ in range(400):
        session = ts('show', f'--id={session["id"]}')['session']
        if session['status'] != 'creating':
            break
        time.sleep(3)
    log = root / 'test-sessions' / fixture.NAME / 'log'
    check(session['status'] == 'ready', f'the session is ready ({int(time.time() - started)} s)',
          f'{session} {log.read_text()[-3000:] if log.exists() else ""}')
    check(not (base / 'pwned').exists(), "the branch's Makefile never ran on the host")
    check(session['env_commit'] != session['tests_commit'], 'host tree from master, specs from the branch')
    name = session['compose_project'] if 'compose_project' in session else f'{fixture.NAME}-{session["id"][3:]}-test'
    network = subprocess.run(['docker', 'network', 'inspect', f'{name}-network', '--format', '{{.Internal}}'],
                             capture_output=True, text=True).stdout.strip()
    check(network == 'true', 'the session network is internal', network)
    ports = subprocess.run(['docker', 'ps', '--filter', f'label=com.docker.compose.project={name}', '--format', '{{.Ports}}'],
                           capture_output=True, text=True).stdout
    check('->' not in ports, 'nothing of the session is published on the host', ports)
    confined(name)
    gateway = subprocess.run(['docker', 'network', 'inspect', 'bridge', '--format', '{{(index .IPAM.Config 0).Gateway}}'],
                             capture_output=True, text=True).stdout.strip()
    php = next(line for line in subprocess.run(['docker', 'ps', '--format', '{{.Names}}', '--filter',
                                                f'label=com.docker.compose.project={name}'], capture_output=True,
                                               text=True).stdout.split() if line.endswith('-php-fpm'))
    gateway_check(name, php, image_of(name))
    for url in ('http://1.1.1.1/', f'http://{gateway}:9102/', 'http://169.254.169.254/latest/meta-data/'):
        reached = subprocess.run(['docker', 'exec', php, 'php', '-r',
                                  f'echo @file_get_contents("{url}", false, stream_context_create(["http" => ["timeout" => 5]])) '
                                  '=== false ? "blocked" : "reached";'], capture_output=True, text=True, timeout=60).stdout
        check(reached.strip() == 'blocked', f'the shop (php-fpm) cannot reach {url}', reached)

    # The branch fixed fixme.spec.ts: a run of the session passes it; the trusted tree's own specs would fail.
    run = ts('run', '--by=e2e', '--manage', stdin={'target': 'test', 'session': session['id'], 'files': ['fixme.spec.ts']})['run']
    run = wait_run(run['id'])
    check(run['status'] == 'passed' and run['executed'], "the branch's spec runs (passed)", run)

    # Back to the trusted specs: recreate from master, then a failing test, a patch, a cancel.
    ts('destroy', f'--id={session["id"]}', '--by=e2e')
    for _ in range(100):
        if ts('show', f'--id={session["id"]}')['session']['status'] in ('destroyed', 'failed'):
            break
        time.sleep(3)
    session = ts('create', f'--project={fixture.NAME}', '--db=dataset:dev', '--by=e2e')['session']
    for _ in range(400):
        session = ts('show', f'--id={session["id"]}')['session']
        if session['status'] != 'creating':
            break
        time.sleep(3)
    check(session['status'] == 'ready', 'a session of master is ready', session)
    name = f'{fixture.NAME}-{session["id"][3:]}-test'
    confined(name)
    run = wait_run(ts('run', '--by=e2e', '--manage', stdin={'target': 'test', 'session': session['id']})['run']['id'])
    titles = {test['title']: test['status'] for test in run.get('tests', [])}
    check(run['status'] == 'failed' and titles.get('status says ready') == 'failed', 'a failing test fails the run', run)
    check(run['summary']['passed'] >= 2 and run['artifacts'], 'the others pass; artifacts are kept', run.get('summary'))
    artifact = run['artifacts'][0]['name']
    raw = subprocess.run([sys.executable, str(PROGRAM), 'artifact', f'--id={run["id"]}', f'--name={artifact}'],
                         capture_output=True, env=environment).stdout
    head, _, body = raw.partition(b'\n')
    check(json.loads(head)['artifact']['size'] == len(body) > 0, 'an artifact streams after its header')
    fixed = ts('run', '--by=e2e', '--manage', stdin={'target': 'test', 'session': session['id'], 'patch': fixture.FIX_PATCH})
    fixed = wait_run(fixed['run']['id'])
    check(fixed['status'] == 'passed', 'the patch fixes it', fixed)
    bad = ts('run', '--by=e2e', '--manage', stdin={'target': 'test', 'session': session['id'], 'patch': fixture.OUTSIDE_PATCH})
    check(bad.get('status') == 400, 'a patch outside the specs is refused', bad)
    long = ts('run', '--by=e2e', '--manage', stdin={'target': 'test', 'session': session['id']})['run']
    for _ in range(100):
        if ts('run-show', f'--id={long["id"]}')['run'].get('step') == 'e2e':
            break
        time.sleep(1)
    time.sleep(4)
    stopped = ts('cancel', f'--id={long["id"]}', '--by=e2e')['run']
    check(stopped['status'] == 'cancelled', 'a cancel stops a running run', stopped)
    time.sleep(2)
    left = subprocess.run(['docker', 'ps', '-a', '--filter', f'name=^{long["id"]}$', '-q'], capture_output=True, text=True).stdout
    check(not left.strip(), 'its container is gone', left)
    check(ts('run-show', f'--id={long["id"]}')['run']['status'] == 'cancelled', 'and it stays cancelled')

    check(stopped['executed'] is True, 'a cancel during the suite says tests may have run (executed)', stopped)
    gateway_spec = ESCAPE_SPEC.replace('GATEWAY', gateway)
    escape = wait_run(ts('run', '--by=e2e', '--manage', stdin={'target': 'test', 'session': session['id'], 'files': ['escape.spec.ts'],
                                                                'patch': new_file_patch('tests/playwright/escape.spec.ts', gateway_spec)})['run']['id'])
    check(escape['status'] == 'passed' and escape['summary']['passed'] == 6,
          'a spec reaches neither the internet, the host gateway nor metadata; /tmp is bounded; the root is read-only', escape)
    filled = wait_run(ts('run', '--by=e2e', '--manage', stdin={'target': 'test', 'session': session['id'], 'files': ['fill.spec.ts'],
                                                                'patch': new_file_patch('tests/playwright/fill.spec.ts', FILL_SPEC)})['run']['id'])
    check(filled['status'] == 'error' and 'disk quota' in (filled.get('error') or ''), 'a run over its disk quota is stopped', filled)

    # Snapshots: the page, and a path that would leave the site.
    snap = ts('snapshot', '--by=e2e', '--manage', stdin={'target': 'test', 'session': session['id'], 'path': '/'})
    check(snap.get('run', {}).get('status') == 'done' and snap['run']['snapshot']['title'] == 'Fixture shop', 'a snapshot of /', snap)
    away = ts('snapshot', '--by=e2e', '--manage', stdin={'target': 'test', 'session': session['id'], 'path': '//172.17.0.1:9102/'})
    check(away.get('status') == 400, 'a scheme-relative path is refused', away)

    # stage: the trusted suite against a site in a container on Docker's bridge (a host firewall may keep containers
    # from the host itself), served by the Playwright image's node.
    image = subprocess.run(['docker', 'image', 'ls', '--filter', 'label=setup.service=playwright', '--format',
                            '{{.Repository}}:{{.Tag}}'], capture_output=True, text=True).stdout.split()[0]
    site, other = (f'ts-e2e-{kind}-{base.name[-8:].lower()}' for kind in ('site', 'other'))
    counter = """const http = require('http'); const seen = [];
http.createServer((q, s) => {
  if (q.url === '/seen') { s.setHeader('content-type', 'application/json'); return s.end(JSON.stringify(seen)); }
  seen.push(q.method + ' ' + q.url);
  if (q.url === '/go') { s.writeHead(302, { location: process.env.OTHER + '/stolen' }); return s.end(); }
  s.setHeader('content-type', 'text/html');
  if (q.url === '/form') return s.end('<!doctype html><title>Form</title><form id="f" method="post" action="/order">'
                                      + '<input name="x" value="1"></form><script>document.getElementById("f").submit()</script>');
  s.end('<!doctype html><title>Fixture</title><h1>Fixture shop</h1><img src="' + (process.env.OTHER || '') + '/pixel">'
        + '<script>fetch("/order", { method: "POST", body: "x=1" }).catch(() => {})</script>');
}).listen(8080);"""
    subprocess.run(['docker', 'run', '-d', '--rm', '--name', other, '--network', 'bridge', '--user', '1000:1000',
                    '--entrypoint', 'node', image, '-e', counter], check=True, capture_output=True)
    other_address = subprocess.run(['docker', 'inspect', '--format', '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}', other],
                                   capture_output=True, text=True).stdout.strip()
    subprocess.run(['docker', 'run', '-d', '--rm', '--name', site, '--network', 'bridge', '--user', '1000:1000',
                    '--env', f'OTHER=http://{other_address}:8080', '--entrypoint', 'node', image, '-e', counter],
                   check=True, capture_output=True)

    def seen(container):
        return json.loads(subprocess.run(['docker', 'exec', container, 'node', '-e',
                                          "fetch('http://127.0.0.1:8080/seen').then(r => r.text()).then(console.log)"],
                                         capture_output=True, text=True).stdout or '[]')
    try:
        address = subprocess.run(['docker', 'inspect', '--format', '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}', site],
                                 capture_output=True, text=True).stdout.strip()
        check(bool(address), 'the site container has an address', address)
        with open(config / 'test-sessions.env', 'a') as handle:
            handle.write(f'TEST_URL_{fixture.NAME.upper().replace("-", "_")}_STAGE=http://{address}:8080\n')
        stage = ts('run', '--by=e2e', '--manage', stdin={'target': 'stage', 'project': fixture.NAME})
        check('run' in stage, 'a stage run starts', stage)
        stage = wait_run(stage['run']['id'])
        check(stage['summary']['passed'] + stage['summary']['failed'] == 2, 'stage runs its 2 @stage tests (not @stage-write)',
              stage)
        check(not (root / 'test-runs' / stage['id'] / 'data').exists(), "a stage run's traces and report are not kept")
        again = wait_run(ts('run', '--by=e2e', '--manage', stdin={'target': 'stage', 'project': fixture.NAME})['run']['id'])
        install = (root / 'test-runs' / again['id'] / 'log').read_text()
        check('is up to date' in install, 'the next stage run reuses the dependency install')
        before, other_before = len(seen(site)), len(seen(other))  # the runs before were free to load the pixel
        snap = ts('snapshot', '--by=e2e', '--manage', stdin={'target': 'stage', 'project': fixture.NAME, 'path': '/'})
        check(snap.get('run', {}).get('status') == 'done' and snap['run']['snapshot']['blocked'] >= 2,
              'a snapshot of stage: the page, its POST and its image from another host blocked', snap)
        form = ts('snapshot', '--by=e2e', '--manage', stdin={'target': 'stage', 'project': fixture.NAME, 'path': '/form'})
        check(form.get('run', {}).get('status') == 'error', 'a page that submits a form gives an error, not the result', form)
        away = ts('snapshot', '--by=e2e', '--manage', stdin={'target': 'stage', 'project': fixture.NAME, 'path': '/go'})
        check(away.get('run', {}).get('status') == 'error', 'a redirect to another host gives an error, no page', away)
        site_seen, other_seen = seen(site)[before:], seen(other)[other_before:]
        check(not any(item.startswith('POST') for item in site_seen), f'the site received no POST from a snapshot: {site_seen}')
        check(other_seen == [], f'the other server received nothing from the snapshots (pixel, redirect hop): {other_seen}')
        patched = ts('run', '--by=e2e', '--manage', stdin={'target': 'stage', 'project': fixture.NAME, 'patch': fixture.FIX_PATCH})
        check(patched.get('status') == 400, 'stage takes no patch', patched)
    finally:
        subprocess.run(['docker', 'rm', '-f', site, other], capture_output=True)

    # Expiry: reconcile tears it down; nothing of it is left.
    record = root / 'test-sessions' / fixture.NAME / 'session.json'
    data = json.loads(record.read_text())
    data['expires_at'] = int(time.time()) - 1
    record.write_text(json.dumps(data))
    done = ts('reconcile')
    check(f'expired {session["id"]}' in done['reconciled'], 'reconcile expires the session', done)
    check(ts('show', f'--id={session["id"]}')['session']['status'] == 'expired', 'its status is expired')
    rest = subprocess.run(['docker', 'ps', '-a', '--filter', f'label=com.docker.compose.project={name}', '-q'],
                          capture_output=True, text=True).stdout
    check(not rest.strip(), 'no container of the session is left', rest)
    print(f'all checks passed in {int(time.time() - started)} s')
    if not options.keep:
        subprocess.run([sys.executable, str(PROGRAM), 'reconcile'], env=environment, capture_output=True)
        shutil.rmtree(base, ignore_errors=True)


if __name__ == '__main__':
    main()
