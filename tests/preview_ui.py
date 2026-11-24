"""Preview bin/web-ui in a browser: the test worker tree (tests/test_ui.py) behind a proxy on 127.0.0.1:9178
that adds the test login, so no password is typed. sudo is the tests' stub: "Run" runs nothing real.

Usage: python3 tests/preview_ui.py [PORT]     (or the launch configuration "web-ui")
Stopping it (Ctrl-C, SIGTERM) stops the UI and removes the temporary tree.
"""

import base64
import getpass
import hashlib
import http.client
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import signal
import subprocess
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
    app = case.tree.projects / 'alpha_shop/app'
    stamp = lambda age: time.strftime('%Y%m%dT%H%M%S', time.gmtime(time.time() - age))
    case.tree.backup(app, f'full-prod-{stamp(3 * 86400)}Z.sql.gz', 3 * 86400, 7_340_032)
    case.tree.backup(app, f'alpha-prod-{stamp(5 * 3600)}123.sql.gz', 5 * 3600, 1_200_000)
    # Files backups (BACKUP_FILE_PATHS): two nights, one translation changed, one added, one gone.
    (app / 'env/common.env').write_text('PROJECT_NAME=alpha\nBACKUP_FILE_PATHS="modules/*/translations/ themes/*/lang/"\n')
    lt = "<?php\nglobal $_MODULE;\n$_MODULE = array();\n$_MODULE['<{ps_shoppingcart}prestashop>cart_1'] = 'Krepšelis';\n"
    test_ui.files_archive(app, 'prod', 26 * 3600, {'modules/ps_shoppingcart/translations/lt.php': lt.encode(),
                                                    'modules/ps_contact/translations/lt.php': b'<?php\n$_MODULE = array();\n',
                                                    'themes/classic/lang/old.php': b'<?php\n'})
    test_ui.files_archive(app, 'prod', 2 * 3600, {'modules/ps_shoppingcart/translations/lt.php': (lt.replace('Krepšelis', 'Pirkinių krepšelis') + "$_MODULE['<{ps_shoppingcart}prestashop>cart_2'] = 'Tuščias';\n").encode(),
                                                   'modules/ps_contact/translations/lt.php': b'<?php\n$_MODULE = array();\n',
                                                   'modules/ps_banner/translations/lt.php': b'<?php\n$_MODULE = array();\n',
                                                   'themes/classic/lang/flag.png': b'\x89PNG\r\n\x1a\n\0\0'})
    # More projects, most with prod and stage, as a worker with many shops has them.
    # Deployed here, each with a nightly DB short and a weekly DB full backup, but flowers (none yet) and books (no target).
    for shop in ('wellness', 'amber', 'coffee', 'supplements', 'beta', 'pharmacy', 'pet-shop', 'furniture', 'books', 'flowers'):
        directory = case.tree.project(shop, 'prod', 'stage')
        if shop != 'books':
            (directory.parent / 'target.env').write_text(f'DEPLOY_CONTEXT=default\nDEPLOY_SSH=\nDEPLOY_PATH={directory.parent}\n')
        for environment in ('prod', 'stage') if shop not in ('books', 'flowers') else ():
            # coffee prod is backed up weekly (below): its newest DB short one is 4 days old.
            age = 4 * 86400 if (shop, environment) == ('coffee', 'prod') else 3 * 3600
            case.tree.backup(directory, f'worker-{environment}-{stamp(age)}Z.sql.gz', age, 240_000)
            case.tree.backup(directory, f'full-{environment}-{stamp(2 * 86400)}Z.sql.gz', 2 * 86400, 980_000)
    # Backup policies (bin/backup-projects --set): DB full only, daily; nothing at all; weekly and monthly; a late DB-full-only one.
    case.tree.backup(case.tree.projects / 'wellness/app', f'full-prod-{stamp(5 * 3600)}Z.sql.gz', 5 * 3600, 1_010_000)
    (case.root / 'config').mkdir(exist_ok=True)
    (case.root / 'config/backup-policy.conf').write_text(
        '# bin/backup-projects --set: how often each series of a project environment is backed up (README: DB backups).\n'
        'wellness prod db-short=off db-full=daily\nbeta stage db-short=off db-full=off files=off\n'
        'coffee prod db-short=weekly db-full=monthly\npharmacy stage db-short=off db-full=daily\nalpha_shop prod files=weekly\n')
    # Developer keys with grants (bin/add-backup-key), their requests, and a setup checkout past its release tag.
    def blob(seed):  # a well-formed ssh-ed25519 public key of its own for each developer
        return base64.b64encode(b'\0\0\0\x0bssh-ed25519\0\0\0\x20' + hashlib.sha256(seed.encode()).digest()).decode()
    access = case.sbin / 'setup-server-access'  # the UI's SETUP_SERVER_SBIN: bin/add-backup-key knows its entries by it
    (ssh / 'authorized_keys').write_text(
        f'from="10.8.0.0/24",command="{access} --key tomas --pull-all db --pull wellness:prod=fresh --grant alpha_shop:prod=full '
        f'--grant beta_docker:stage=docker --grant shop-two:prod=phpfpm --grant wellness:prod=phpfpm-readonly '
        f'--grant wellness:stage=docker --grant amber:prod=phpfpm-readonly --grant amber:stage=docker '
        f'--grant coffee:prod=phpfpm --grant coffee:stage=docker",restrict,pty ssh-ed25519 {blob("tomas")} tomas\n'
        f'from="10.8.0.0/24",command="{access} --key ona --pull-all none --pull alpha_shop:prod=db '
        f'--pull amber:stage=fresh --grant alpha_shop:prod=phpfpm-readonly '
        f'--grant beta_docker:stage=phpfpm",restrict,pty ssh-ed25519 {blob("ona")} ona\n'
        f'from="10.8.0.0/24,10.9.0.5/32",command="{case.sbin / "setup-server-backup-pull"} --key qa-ci",restrict '
        f'ssh-ed25519 {blob("qa-ci")} qa-ci\n')
    now = lambda age: time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() - age))
    (case.tree.root / 'access.log').write_text(
        f"{now(7200)} key=tomas from=10.8.0.20 'shell alpha_shop prod' -> started (full): docker exec …\n"
        f"{now(7100)} key=tomas from=10.8.0.20 'shell alpha_shop prod' -> exit 0\n"
        f"{now(90000)} key=ona from=10.8.0.33 'docker beta_docker stage ps' -> refused: needs docker\n")
    with (case.tree.root / 'access.log').open('a') as handle:
        handle.write(f"{now(3000)} key=tomas from=10.8.0.20 'shell wellness prod' -> started (phpfpm-readonly): docker exec -it "
                     f"wellness-prod-php-fpm-1 sh session=20261023T080000Z-4242.log\n"
                     f"{now(2400)} key=tomas from=10.8.0.20 'shell wellness prod' -> exit 0\n"
                     f"{now(1800)} key=tomas from=10.8.0.20 \"exec coffee prod -- php bin/console cache:clear\" -> started (phpfpm): docker exec\n"
                     f"{now(1790)} key=tomas from=10.8.0.20 \"exec coffee prod -- php bin/console cache:clear\" -> exit 1\n"
                     f"{now(600)} key=tomas from=10.8.0.20 'docker amber stage logs --tail 200 php-fpm' -> started (docker): docker logs\n"
                     f"{now(590)} key=tomas from=10.8.0.20 'docker amber stage logs --tail 200 php-fpm' -> exit 0\n")
    sessions = case.tree.root / 'access-sessions/tomas'
    sessions.mkdir(parents=True)
    (sessions / '20261023T080000Z-4242.log').write_bytes(
        b'cd var/logs\rls -la\rtail -n 50 prod-2026-10-23.log | grep -i errr\x7for\r\x1b[Aphp bin/console about\rexit\r')
    (case.tree.root / 'backup-pulls.log').write_text(f'{now(40000)} key=qa-ci from=10.9.0.5 info alpha_shop prod -> ok\n')
    setup = case.tree.root / 'setup'
    git = lambda *arguments: subprocess.run(['git', '-C', str(setup), *arguments], check=True, capture_output=True,
                                            env={**os.environ, 'GIT_AUTHOR_NAME': 'p', 'GIT_AUTHOR_EMAIL': 'p@p',
                                                 'GIT_COMMITTER_NAME': 'p', 'GIT_COMMITTER_EMAIL': 'p@p'})
    setup.mkdir()
    git('init', '-q', '-b', 'master')
    for version in ('2.14.0', '2.14.1', '2.15.0'):
        (setup / 'VERSION').write_text(version + '\n')
        git('add', '-A')
        git('commit', '-q', '-m', version)
        if version != '2.15.0':
            git('tag', f'v{version}')
    git('update-ref', 'refs/remotes/origin/master', 'HEAD')
    git('checkout', '-q', '--detach', 'HEAD')
    # "Run" of a key change writes the tree's authorized_keys for real (the test stub only records): the real
    # bin/add-backup-key, as this user, with its root checks answered.
    helper = case.root / 'preview-add-backup-key'
    helper.write_text(f"""#!{sys.executable}
import os, sys
from unittest.mock import patch
sys.path.insert(0, {str(Path(__file__).resolve().parent)!r})
os.environ['SETUP_SERVER_USER'] = {getpass.getuser()!r}
import test_backup_pull
module = test_backup_pull.load('add_backup_key', {str(test_ui.ROOT / 'bin/add-backup-key')!r})
with patch.object(module.os, 'geteuid', return_value=0), patch.object(module.os, 'chown'):
    sys.exit(module.main(sys.argv[1:]))
""")
    helper.chmod(0o755)
    stub = case.bin / 'sudo'
    stub.write_text(stub.read_text().replace('  */server-status) shift; exec "$SERVER_STATUS" "$@";;',
                                             '  */server-status) shift; exec "$SERVER_STATUS" "$@";;\n'
                                             f'  */add-backup-key) shift; exec {helper} "$@";;'))
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
