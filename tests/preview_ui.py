"""Preview bin/web-ui in a browser: the test worker tree (tests/test_ui.py) behind a proxy on 127.0.0.1:9178
that adds the test login, so no password is typed. sudo is the tests' stub: "Vykdyti" runs nothing real.

Usage: python3 tests/preview_ui.py [PORT]     (or the launch configuration "web-ui")
Stopping it (Ctrl-C, SIGTERM) stops the UI and removes the temporary tree.
"""

import base64
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import signal
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_ui  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9178


def fixture():
    """The tests' worker tree and running UI, with a few more backups and the setup account's public key."""
    case = test_ui.WebUiTest('test_every_command_line_action_has_its_form')
    case.setUp()
    ssh = case.tree.root / '.ssh'
    ssh.mkdir(exist_ok=True)
    (ssh / 'id_ed25519.pub').write_text('ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPreviewOnlyKey0123456789abcdefABCDEF setup@worker1\n')
    app = case.tree.projects / 'asana_tracker/app'
    stamp = lambda age: time.strftime('%Y%m%dT%H%M%S', time.gmtime(time.time() - age))
    case.tree.backup(app, f'full-prod-{stamp(3 * 86400)}Z.sql.gz', 3 * 86400, 7_340_032)
    case.tree.backup(app, f'asana-prod-{stamp(5 * 3600)}123.sql.gz', 5 * 3600, 1_200_000)
    return case


def main():
    case = fixture()
    upstream = case.port
    login = 'Basic ' + base64.b64encode(f'admin:{test_ui.PASSWORD}'.encode()).decode()

    class Proxy(BaseHTTPRequestHandler):
        def forward(self):
            length = int(self.headers.get('Content-Length') or 0)
            body = self.rfile.read(length) if length else None
            headers = {key: value for key, value in self.headers.items() if key.lower() not in ('host', 'authorization')}
            headers.update(Host=f'127.0.0.1:{upstream}', Authorization=login)
            connection = http.client.HTTPConnection('127.0.0.1', upstream, timeout=120)
            connection.request(self.command, self.path, body=body, headers=headers)
            response = connection.getresponse()
            data = response.read()
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() not in ('content-length', 'transfer-encoding', 'connection'):
                    self.send_header(key, value)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = forward

        def log_message(self, *arguments):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', PORT), Proxy)
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))
    print(f'web-ui preview on http://127.0.0.1:{PORT}/ (UI on {upstream}, worker tree {case.tree.root})', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        case.doCleanups()  # stops the UI and the fake Bitbucket, removes the tree


if __name__ == '__main__':
    main()
