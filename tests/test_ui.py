"""The web UI (bin/web-ui), its installer (bin/install-ui) and the read-only status helper (bin/server-status).

The UI runs for real on 127.0.0.1 over a temporary worker tree. sudo is a stub that records every argv and
stdin and runs the real status helper behind it (as this user, with docker, systemctl and journalctl
stubbed); Bitbucket is a fake API on 127.0.0.1. No root, no network.
"""

import base64
import getpass
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import subprocess
import threading
import time
from urllib.parse import urlencode

from test_metrics import WorkerTree, entry
from test_scripts import DIALOG, ROOT, ScriptTest

PASSWORD = "correct-horse-battery-staple"
PROJECT_TOKEN = "project-token-ABCdef0123456789"
GENERAL_TOKEN = "general-token-XYZxyz9876543210"
SECRET = "ATOAfake_secret-VALUE_9"  # the OAuthClientSecret of test_scripts.DIALOG
ROOT_COMMANDS = ("add-project", "remove-project", "install-backups", "install-metrics", "install-runner",
                 "add-backup-key", "prepare-host", "server-status", "update-setup")


def load_web_ui():
    loader = importlib.machinery.SourceFileLoader("web_ui", str(ROOT / "bin/web-ui"))
    spec = importlib.util.spec_from_loader("web_ui", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


# A cheap hash for the tests (the installer uses 600 000 iterations).
PASSWORD_HASH = load_web_ui().password_hash(PASSWORD, iterations=10000)

# sudo: records argv and stdin, then drops its own options. The status helper runs for real; every other
# command answers with "stub ran: <argv>", echoes its stdin (the worst a command could do) and exits with
# the code in $STUB_DIR/exit; with $STUB_DIR/sleep a run (not a --check) takes that long.
SUDO_STUB = r'''
echo "$*" >> "$STUB_DIR/sudo.calls"
while [ $# -gt 0 ]; do case "$1" in -u) shift 2;; -*) shift;; *) break;; esac; done
case "$1" in
  */server-status) shift; exec "$SERVER_STATUS" "$@";;
esac
input=$(cat)
printf '%s\n----\n' "$input" >> "$STUB_DIR/sudo.stdin"
echo "stub ran: $*"
printf '%s\n' "$input"
case " $* " in *" --check "*) ;; *) [ ! -f "$STUB_DIR/sleep" ] || sleep "$(cat "$STUB_DIR/sleep")";; esac
exit "$(cat "$STUB_DIR/exit" 2>/dev/null || echo 0)"
'''
# Docker: the project containers of the default context (compose label, name, state, status).
DOCKER_STUB = r'''
echo "$*" >> "$STUB_DIR/docker.calls"
printf 'asana-prod\tasana-prod-secrets\trunning\tUp 3 hours\n'
printf 'asana-prod\tasana-prod-php-fpm\trunning\tUp 3 hours (healthy)\n'
printf 'asana-prod\tasana-prod-cron\texited\tExited (1) 2 minutes ago\n'
printf 'other-prod\tother-prod-web\trunning\tUp 1 day\n'
'''
SYSTEMCTL_STUB = r'''
echo "$*" >> "$STUB_DIR/systemctl.calls"
for unit; do
  case "$unit" in
    setup-server-runner.service) printf 'Id=%s\nLoadState=loaded\nActiveState=active\nSubState=running\nActiveEnterTimestamp=Sun 2026-10-11 10:00:00 UTC\n\n' "$unit";;
    setup-server-backup.timer) printf 'Id=%s\nLoadState=loaded\nActiveState=active\nLastTriggerUSec=Mon 2026-10-12 00:30:00 UTC\nNextElapseUSecRealtime=Tue 2026-10-13 00:30:00 UTC\n\n' "$unit";;
    setup-server-backup.service) printf 'Id=%s\nLoadState=loaded\nActiveState=inactive\nResult=exit-code\n\n' "$unit";;
    setup-server-github-runner@*) printf 'Id=%s\nLoadState=loaded\nActiveState=failed\n\n' "$unit";;
    -*|show) ;;
    *) printf 'Id=%s\nLoadState=not-found\nActiveState=inactive\n\n' "$unit";;
  esac
done
'''
JOURNALCTL_STUB = 'echo "$*" >> "$STUB_DIR/journalctl.calls"\nfor unit; do :; done\necho "2026-10-12T00:30:00+0000 host journal of ${unit#--unit=}"\n'


class Worker(ScriptTest):
    """A worker tree with three build roots and the stubbed system commands."""

    def setUp(self):
        super().setUp()
        self.stubs = self.root / "stubs"
        self.stubs.mkdir()
        os.environ["STUB_DIR"] = str(self.stubs)
        self.addCleanup(os.environ.pop, "STUB_DIR")
        self.stub("docker", DOCKER_STUB)
        self.stub("systemctl", SYSTEMCTL_STUB)
        self.stub("journalctl", JOURNALCTL_STUB)
        self.tree = WorkerTree(self.root / "srv")
        self.library = self.root / "lib"
        hour = 3600
        asana = self.tree.project("asana_tracker", "prod")
        self.origin(asana.parent / "app", "git@bitbucket.org:rsmaizys/asana_tracker.git")
        (asana / "env/common.env").write_text("PROJECT_NAME=asana\n")
        (asana / "env/prod.env").write_text("DOMAIN=asana.example\nDATABASE_ROOT_PASSWORD=never-shown-password\n")
        (self.tree.projects / "asana_tracker/target.env").write_text(
            f"DEPLOY_CONTEXT=default\nDEPLOY_SSH=\nDEPLOY_PATH={self.tree.projects}/asana_tracker\nAPP_UID=1001\nAPP_GID=1001\n")
        for age, size in ((2 * hour, 4096), (26 * hour, 3000), (50 * hour, 2000)):
            stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(time.time() - age))
            self.tree.backup(asana, f"worker-prod-{stamp}Z.sql.gz", age, size)
        self.tree.deployments(asana, "prod", [entry("v1.1.0", "b" * 40, "2026-10-05T10:00:00+00:00"),
                                              entry("v1.0.0", "a" * 40, "2026-10-06T08:30:00+00:00", rollback=True)])
        forsena = self.tree.project("forsena_docker", "stage")
        self.origin(forsena.parent / "app", "git@github.com:BigSant/forsena_docker.git")
        (self.tree.projects / "forsena_docker/target.stage.env").write_text(
            "DEPLOY_CONTEXT=setup-forsena_docker-stage\nDEPLOY_SSH=deploy@shop.example\nDEPLOY_PATH=/home/deploy/shop\n")
        shop = self.tree.project("shop-two", "prod")
        self.origin(shop.parent / "app", "git@bitbucket.org:rsmaizys/redirect_me.git")
        runner = self.tree.root / "github-runner/bigsant"
        runner.mkdir(parents=True)
        (runner / ".runner").write_text("{}\n")

    @staticmethod
    def origin(checkout, url):
        (checkout / ".git/refs/remotes/origin").mkdir(parents=True, exist_ok=True)
        (checkout / ".git/config").write_text(f'[core]\n\tbare = false\n[remote "origin"]\n\turl = {url}\n'
                                              '\tfetch = +refs/heads/*:refs/remotes/origin/*\n')
        (checkout / ".git/refs/remotes/origin/HEAD").write_text("ref: refs/remotes/origin/main\n")

    def calls(self, name):
        path = self.stubs / f"{name}.calls"
        return path.read_text().splitlines() if path.exists() else []


class ServerStatusTest(Worker):
    def status(self, *arguments):
        result = subprocess.run([str(ROOT / "bin/server-status"), *arguments], capture_output=True, text=True, timeout=60,
                                env={**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}",
                                     "SETUP_SERVER_ROOT": str(self.tree.root), "SETUP_SERVER_LIBRARY": str(self.library)})
        return result

    def test_the_overview_comes_from_the_build_roots_docker_and_systemd(self):
        self.library.mkdir()
        (self.library / "install.env").write_text(f"VERSION=v0.4.0\nCHECKOUT={ROOT}\nADMIN={getpass.getuser()}\n")
        result = self.status()
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        projects = {item["name"]: item for item in data["projects"]}
        self.assertEqual(sorted(projects), ["asana_tracker", "forsena_docker", "shop-two"])
        asana = projects["asana_tracker"]
        self.assertEqual((asana["repository"], asana["branch"], asana["project_name"]),
                         ("git@bitbucket.org:rsmaizys/asana_tracker.git", "main", "asana"))
        self.assertEqual(asana["targets"]["all"]["DEPLOY_CONTEXT"], "default")
        prod = asana["environments"]["prod"]
        self.assertEqual(prod["deploys"]["last"], {"ref": "v1.0.0", "commit": "a" * 12, "at": 1791275400,
                                                   "rollback": True, "failed": None})
        self.assertEqual(prod["deploys"]["count"], 2)
        self.assertEqual((prod["backups"]["count"], prod["backups"]["newest"]["size"]), (3, 4096))
        self.assertEqual(prod["secrets"], "running")
        self.assertEqual([item["name"] for item in prod["containers"]],
                         ["asana-prod-secrets", "asana-prod-php-fpm", "asana-prod-cron"])
        self.assertEqual(prod["containers"][1]["health"], "healthy")
        stage = projects["forsena_docker"]["environments"]["stage"]
        self.assertEqual((stage["context"], stage["secrets"], stage["containers"]), ("setup-forsena_docker-stage", "missing", []))
        self.assertIn("--context setup-forsena_docker-stage ps", " ".join(self.calls("docker")))
        self.assertEqual(data["units"]["setup-server-runner.service"]["ActiveState"], "active")
        self.assertEqual(data["units"]["setup-server-backup.service"]["Result"], "exit-code")
        self.assertEqual(data["units"]["setup-server-github-runner@bigsant.service"]["ActiveState"], "failed")
        self.assertEqual(data["github_runners"], ["bigsant"])
        self.assertEqual(data["versions"]["installed"], "v0.4.0")
        self.assertTrue(data["versions"]["checkout"], "git describe of the admin clone")
        self.assertEqual(data["disk"]["path"], str(self.tree.root))
        self.assertTrue(all(call.startswith("show ") for call in self.calls("systemctl")), "systemd is only read")

    def test_only_listed_values_leave_and_a_planted_symlink_is_not_followed(self):
        asana = self.tree.projects / "asana_tracker"
        (self.root / "root-only.env").write_text("DEPLOY_SSH=from-a-root-file\nBITBUCKET_ACCESS_TOKEN=leaked\n")
        (asana / "target.prod.env").symlink_to(self.root / "root-only.env")
        result = self.status()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("never-shown-password", result.stdout)
        self.assertNotIn("from-a-root-file", result.stdout)
        self.assertNotIn("leaked", result.stdout)
        self.assertIn("target.prod.env: a symlink; not read", result.stdout)
        data = json.loads(result.stdout)
        self.assertEqual(len(data["projects"]), 3, "the other files of the project are still read")

    def test_logs_are_bounded_and_only_of_the_setup_server_units(self):
        (self.tree.root / "backup-pulls.log").write_text("".join(f"line {n}\n" for n in range(100)))
        result = self.status("--logs", "5")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["backup_pulls"], [f"line {n}" for n in range(95, 100)])
        self.assertEqual(sorted(data["journal"]), sorted(["setup-server-runner.service", "setup-server-github-runner@*.service",
                                                          "setup-server-backup.service", "setup-server-metrics.service",
                                                          "setup-server-ui.service"]))
        self.assertTrue(all("--lines=5" in call for call in self.calls("journalctl")))
        for arguments in (["--logs", "9999"], ["--logs", "0"], ["--logs", "5;ls"], ["--unit", "ssh"], ["--logs", "5", "x"]):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.status(*arguments).returncode, 64)


class InstallUiTest(ScriptTest):
    def setUp(self):
        super().setUp()
        self.paths = {"SETUP_SERVER_SYSTEMD": self.root / "systemd", "SETUP_SERVER_SBIN": self.root / "sbin",
                      "SETUP_SERVER_CONFIG": self.root / "etc", "SETUP_SERVER_SUDOERS": self.root / "sudoers.d",
                      "SETUP_SERVER_LIBRARY": self.root / "lib"}
        self.account = "setup-ui-test-missing"

    def install(self, *arguments, **env):
        return self.run_script("install-ui", *arguments, SETUP_SERVER_UI_USER=self.account,
                               **{key: str(value) for key, value in self.paths.items()}, **env)

    def test_check_shows_the_account_unit_sudo_rule_and_settings_and_changes_nothing(self):
        result = self.install("--check")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        out = result.stdout
        self.assertIn(f"would create the system account {self.account} (no login shell, no home, not in the docker group)", out)
        self.assertIn(f"would add {self.account} to the group setup-test-account-missing", out)
        self.assertIn(f"would write {self.root}/systemd/setup-server-ui.service", out)
        for line in (f"User={self.account}", f"EnvironmentFile={self.root}/etc/ui.env", "KillMode=process",
                     "NoNewPrivileges=false", f"ExecStart={self.root}/sbin/setup-server-ui",
                     f"Environment=SETUP_SERVER_LIBRARY={self.root}/lib", "StateDirectory=setup-server-ui"):
            self.assertIn(f"    {line}\n", out)
        # systemd sets NoNewPrivileges for any of these (systemd.exec), and then sudo cannot run as root.
        unit = out.split(f"would write {self.root}/systemd/setup-server-ui.service", 1)[1].split("==>", 1)[0]
        for option in ("SystemCallFilter", "SystemCallArchitectures", "RestrictAddressFamilies", "RestrictNamespaces",
                       "PrivateDevices", "ProtectKernelTunables", "ProtectKernelModules", "ProtectKernelLogs",
                       "ProtectClock", "MemoryDenyWriteExecute", "RestrictRealtime", "RestrictSUIDSGID",
                       "DynamicUser", "LockPersonality", "CapabilityBoundingSet"):
            self.assertNotRegex(unit, rf"(?m)^\s*{option}=", f"{option} would turn NoNewPrivileges on")
        self.assertIn("UI_PASSWORD_HASH=<new, hidden>", out)
        self.assertIn("UI_PORT=9102", out)
        self.assertNotRegex(out, r"pbkdf2_sha256:\d+:", "the hash is not shown")
        self.assertIn(f"would write {self.root}/lib/bin (root-owned copies of bin/): ", out)
        self.assertIn("would listen on http://127.0.0.1:9102/ for admin; Bitbucket tokens: none", out)
        for name in ("systemd", "sbin", "etc", "sudoers.d", "lib", "srv"):
            self.assertFalse((self.root / name).exists(), "--check changes nothing")

    def sudoers(self, output):
        text = output.split(f"would write {self.root}/sudoers.d/setup-server-ui\n", 1)[1].split("==>", 1)[0]
        return "\n".join(line[4:] for line in text.splitlines()) + "\n"

    def test_the_sudo_rule_names_only_the_root_owned_copies_and_the_clones_update(self):
        rule = self.sudoers(self.install("--check").stdout)
        commands = {}
        for statement in re.sub(r"\\\n\s*", " ", rule).splitlines():
            if statement.startswith("#") or not statement.strip():
                continue
            match = re.fullmatch(r"(\S+) ALL=\((\S+)\) NOPASSWD: (.+)", statement)
            self.assertIsNotNone(match, statement)
            self.assertEqual(match[1], self.account)
            commands[match[2]] = [command.strip() for command in match[3].split(",")]
        self.assertEqual(commands, {"root": [f"{self.root}/lib/bin/{name}" for name in ROOT_COMMANDS],
                                    getpass.getuser(): [f"{ROOT.resolve()}/bin/update"]})
        if shutil.which("visudo"):
            (self.root / "rule").write_text(rule)
            check = subprocess.run(["visudo", "-cqf", str(self.root / "rule")], capture_output=True, text=True)
            self.assertEqual(check.returncode, 0, check.stdout + check.stderr)

    def test_only_the_loopback_address_and_unprivileged_ports_are_accepted(self):
        for arguments in (["--address", "10.8.0.14"], ["--address", "0.0.0.0"], ["--address", "::1"]):
            with self.subTest(arguments=arguments):
                refused = self.install("--check", *arguments)
                self.assertEqual(refused.returncode, 64, refused.stdout + refused.stderr)
                self.assertIn("listens on 127.0.0.1 only", refused.stderr)
        for port in ("80", "http", "70000", "9102;id"):
            with self.subTest(port=port):
                self.assertEqual(self.install("--check", "--port", port).returncode, 64)
        accepted = self.install("--check", "--address", "127.0.0.1", "--port", "9200")
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        self.assertIn("would listen on http://127.0.0.1:9200/", accepted.stdout)

    def test_a_rerun_keeps_the_login_and_the_tokens_and_never_shows_them(self):
        (self.root / "etc").mkdir()
        (self.root / "etc/ui.env").write_text(f"UI_USER=ops\nUI_PASSWORD_HASH={PASSWORD_HASH}\nUI_PORT=9105\n"
                                              f"BITBUCKET_ACCESS_TOKEN_ASANA_TRACKER={PROJECT_TOKEN}\nBITBUCKET_ACCESS_TOKEN=\n")
        kept = self.install("--check")
        self.assertEqual(kept.returncode, 0, kept.stdout + kept.stderr)
        self.assertIn("UI_USER=ops\n", kept.stdout)
        self.assertIn("UI_PASSWORD_HASH=<kept, hidden>", kept.stdout)
        self.assertIn("BITBUCKET_ACCESS_TOKEN_ASANA_TRACKER=<hidden>", kept.stdout)
        self.assertIn("BITBUCKET_ACCESS_TOKEN=\n", kept.stdout)
        self.assertIn("for ops; Bitbucket tokens: BITBUCKET_ACCESS_TOKEN_ASANA_TRACKER", kept.stdout)
        self.assertNotIn(PROJECT_TOKEN, kept.stdout + kept.stderr)
        self.assertNotIn(PASSWORD_HASH, kept.stdout + kept.stderr)
        renewed = self.install("--check", "--new-password")
        self.assertIn("UI_PASSWORD_HASH=<new, hidden>", renewed.stdout)

    def test_refresh_rewrites_only_installed_copies_with_this_hosts_paths(self):
        nothing = self.install("--check", "--refresh")
        self.assertEqual(nothing.returncode, 0, nothing.stdout + nothing.stderr)
        self.assertIn("is not installed; nothing to refresh", nothing.stdout)
        root_bin = self.fake_root()
        self.stub("systemctl", f'echo "$*" >> {self.root}/systemctl.calls\n')
        (self.root / "sbin").mkdir()
        (self.root / "sbin/setup-server-ui").write_text("previous copy\n")
        (self.root / "lib/bin").mkdir(parents=True)
        (self.root / "lib/bin/retired-command").write_text("gone in this version\n")
        result = self.install("--refresh", PATH=f"{root_bin}:{self.bin}:{os.environ['PATH']}")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"os.environ.get('SETUP_SERVER_ROOT', '{self.root}/srv')", (self.root / "sbin/setup-server-ui").read_text())
        self.assertIn(f"ROOT=${{SETUP_SERVER_ROOT:-{self.root}/srv}}", (self.root / "lib/bin/add-project").read_text())
        self.assertIn(f"SBIN=${{SETUP_SERVER_SBIN:-{self.root}/sbin}}", (self.root / "lib/bin/install-backups").read_text())
        self.assertEqual(sorted(path.name for path in (self.root / "lib/bin").iterdir()),
                         sorted(path.name for path in (ROOT / "bin").iterdir() if path.is_file()))
        self.assertIn(f"CHECKOUT={ROOT.resolve()}\n", (self.root / "lib/install.env").read_text())
        self.assertIn("try-restart setup-server-ui.service", (self.root / "systemctl.calls").read_text())
        self.assertIn("differs from this version; apply it with: sudo bin/install-ui", result.stdout)
        self.assertFalse((self.root / "etc/ui.env").exists(), "--refresh touches no settings")
        again = self.install("--refresh", PATH=f"{root_bin}:{self.bin}:{os.environ['PATH']}")
        self.assertIn(f"{self.root}/lib/bin: unchanged", again.stdout)

    def test_installing_needs_root(self):
        if os.getuid() == 0:
            self.skipTest("runs as root")
        self.assertEqual(self.install().returncode, 77)


class FakeBitbucket(ThreadingHTTPServer):
    """The pipelines API of one workspace: records every request; redirect_me answers with a redirect."""
    daemon_threads = True

    def __init__(self):
        self.requests = []

        class Handler(BaseHTTPRequestHandler):
            def answer(handler, status, body=None, headers=()):
                data = json.dumps(body).encode() if body is not None else b""
                handler.send_response(status)
                for header in headers:
                    handler.send_header(*header)
                handler.send_header("Content-Type", "application/json")
                handler.send_header("Content-Length", str(len(data)))
                handler.end_headers()
                handler.wfile.write(data)

            def do_GET(handler):
                self.requests.append(("GET", handler.path, dict(handler.headers), None))
                if "/redirect_me/" in handler.path or handler.path.startswith("/elsewhere"):
                    return handler.answer(302, headers=[("Location", f"http://127.0.0.1:{self.server_port}/elsewhere")])
                handler.answer(200, {"values": [{"build_number": 41, "state": {"name": "COMPLETED", "result": {"name": "SUCCESSFUL"}},
                                                 "target": {"ref_name": "main", "selector": {"type": "custom", "pattern": "deploy-production"}},
                                                 "created_on": "2026-10-11T09:00:00.000000+00:00",
                                                 "creator": {"display_name": "Tomas"}}]})

            def do_POST(handler):
                body = json.loads(handler.rfile.read(int(handler.headers["Content-Length"])))
                self.requests.append(("POST", handler.path, dict(handler.headers), body))
                if "/redirect_me/" in handler.path:
                    return handler.answer(302, headers=[("Location", f"http://127.0.0.1:{self.server_port}/elsewhere")])
                handler.answer(201, {"build_number": 42, "uuid": "{run}"})

            def log_message(handler, *arguments):
                pass

        super().__init__(("127.0.0.1", 0), Handler)


class WebUiTest(Worker):
    def setUp(self):
        super().setUp()
        self.stub("sudo", SUDO_STUB)
        self.library.mkdir()
        (self.library / "install.env").write_text("VERSION=v0.4.0\nCHECKOUT=/home/admin/setup-server\nADMIN=admin\n")
        self.api = FakeBitbucket()
        threading.Thread(target=self.api.serve_forever, daemon=True).start()
        self.addCleanup(self.api.server_close)
        self.addCleanup(self.api.shutdown)
        self.state = self.root / "state"
        self.log = self.tree.root / "ui-actions.log"
        self.port = self.start()

    def environment(self, **settings):
        environment = {key: value for key, value in os.environ.items() if "proxy" not in key.lower()}
        return {**environment, "PATH": f"{self.bin}:{os.environ['PATH']}", "SETUP_SERVER_ROOT": str(self.tree.root),
                "SETUP_SERVER_LIBRARY": str(self.library), "STATE_DIRECTORY": str(self.state),
                "SERVER_STATUS": str(ROOT / "bin/server-status"), "UI_USER": "admin", "UI_PASSWORD_HASH": PASSWORD_HASH,
                "UI_PORT": "0", "BITBUCKET_API": f"http://127.0.0.1:{self.api.server_port}/2.0",
                "BITBUCKET_ACCESS_TOKEN_ASANA_TRACKER": PROJECT_TOKEN, "BITBUCKET_ACCESS_TOKEN": GENERAL_TOKEN, **settings}

    def start(self, **settings):
        self.stderr = self.root / "ui.stderr"
        output = open(self.stderr, "w")
        self.addCleanup(output.close)
        process = subprocess.Popen([str(ROOT / "bin/web-ui")], env=self.environment(**settings), stderr=output, text=True)
        self.addCleanup(process.wait, 10)
        self.addCleanup(process.terminate)
        for _ in range(100):
            match = re.search(r"listening on 127\.0\.0\.1:(\d+)", self.stderr.read_text())
            if match or process.poll() is not None:
                break
            time.sleep(0.05)
        self.assertIsNotNone(match, self.stderr.read_text())
        return int(match[1])

    def request(self, method, path, form=None, login=("admin", PASSWORD), headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        sent = {"Host": f"127.0.0.1:{self.port}", **(headers or {})}
        if login:
            sent["Authorization"] = "Basic " + base64.b64encode(f"{login[0]}:{login[1]}".encode()).decode()
        body = urlencode(form) if form is not None else None
        if body is not None:
            sent["Content-Type"] = "application/x-www-form-urlencoded"
        connection.request(method, path, body=body, headers=sent)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read().decode()
        connection.close()
        return result

    def csrf(self):
        status, _, body = self.request("GET", "/actions")
        self.assertEqual(status, 200, body)
        return re.search(r'name="csrf" value="([0-9a-f]+)"', body)[1]

    def preview(self, action, **fields):
        return self.request("POST", "/preview", {"csrf": self.csrf(), "action": action, **fields})

    def confirm(self, body):
        """POST the preview's "Vykdyti" form; the job's page when it is done (or the reply)."""
        pending = re.search(r'name="pending" value="([^"]+)"', body)
        self.assertIsNotNone(pending, body)
        status, headers, reply = self.request("POST", "/run", {"csrf": self.csrf(), "pending": pending[1]})
        if status != 303:
            return status, reply
        for _ in range(200):
            status, _, page = self.request("GET", headers["Location"])
            if "vykdoma" not in page:
                return status, page
            time.sleep(0.05)
        self.fail("the job did not finish")

    def sudo_calls(self, pattern=None):
        return [call for call in self.calls("sudo") if pattern is None or pattern in call]

    def everything_written(self):
        """Every file the UI wrote and everything it printed: where a secret must never be."""
        texts = [self.stderr.read_text(), self.log.read_text() if self.log.exists() else ""]
        for path in self.state.rglob("*") if self.state.exists() else []:
            if path.is_file():
                texts.append(path.read_text(errors="replace"))
        return "\n".join(texts)

    # --- Access ----------------------------------------------------------------------------------
    def test_every_request_needs_the_login(self):
        for login in (None, ("admin", "wrong-password"), ("root", PASSWORD), ("admin", "")):
            with self.subTest(login=login):
                status, headers, body = self.request("GET", "/", login=login)
                self.assertEqual(status, 401)
                self.assertEqual(headers.get("WWW-Authenticate"), 'Basic realm="setup-server", charset="UTF-8"')
                self.assertNotIn("asana_tracker", body)
        self.assertEqual(self.request("GET", "/ui.css", login=None)[0], 401)
        self.assertEqual(self.request("POST", "/preview", {"action": "add-project"}, login=None)[0], 401)
        self.assertEqual(self.request("GET", "/")[0], 200)
        # A name rebound to 127.0.0.1 is not this UI's: refused before the login is even looked at.
        self.assertEqual(self.request("GET", "/", headers={"Host": "attacker.example"})[0], 400)
        self.assertEqual(self.request("GET", "/", headers={"Host": f"localhost:{self.port}"})[0], 200)
        self.assertNotIn(PASSWORD, self.everything_written())
        self.assertIn("wrong login (401)", self.stderr.read_text())

    def test_every_post_needs_the_csrf_token_of_its_form(self):
        form = {"action": "add-project", "env_repo": "git@bitbucket.org:ws/shop.git"}
        for token in (None, "", "0" * 64, self.csrf()[:-1] + "x"):
            with self.subTest(token=token):
                fields = form if token is None else {**form, "csrf": token}
                status, _, body = self.request("POST", "/preview", fields)
                self.assertEqual(status, 403, body)
        foreign = self.request("POST", "/preview", {**form, "csrf": self.csrf()}, headers={"Origin": "https://attacker.example"})
        self.assertEqual(foreign[0], 403)
        self.assertEqual(self.request("POST", "/run", {"csrf": "x", "pending": "x"})[0], 403)
        self.assertEqual(self.sudo_calls("add-project"), [], "nothing ran")
        allowed = self.request("POST", "/preview", {**form, "csrf": self.csrf()}, headers={"Origin": f"http://localhost:{self.port}"})
        self.assertEqual(allowed[0], 200, allowed[2])
        # A browser sends the Origin of its form only when the page's Referrer-Policy allows it to the same
        # origin; with no-referrer every form posts Origin: null and is refused.
        status, headers, _ = self.request("GET", "/actions")
        self.assertEqual((status, headers["Referrer-Policy"]), (200, "same-origin"))
        self.assertEqual(self.request("POST", "/preview", {**form, "csrf": self.csrf()}, headers={"Origin": "null"})[0], 403)
        tunnel = self.request("POST", "/preview", {**form, "csrf": self.csrf()}, headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(tunnel[0], 200, tunnel[2])

    def test_only_the_loopback_address_and_complete_settings_are_accepted(self):
        def run(*arguments, **settings):
            return subprocess.run([str(ROOT / "bin/web-ui"), *arguments], env=self.environment(**settings),
                                  capture_output=True, text=True, timeout=30)
        for settings, message in (({"UI_ADDRESS": "0.0.0.0"}, "UI_ADDRESS must be 127.0.0.1"),
                                  ({"UI_ADDRESS": "10.8.0.14"}, "UI_ADDRESS must be 127.0.0.1"),
                                  ({"UI_ADDRESS": "::1"}, "UI_ADDRESS must be 127.0.0.1"),
                                  ({"UI_PORT": "80"}, "UI_PORT"), ({"UI_PASSWORD_HASH": ""}, "UI_PASSWORD_HASH"),
                                  ({"UI_PASSWORD_HASH": PASSWORD}, "UI_PASSWORD_HASH"), ({"UI_USER": "a b"}, "UI_USER"),
                                  ({"BITBUCKET_API": "http://api.example/2.0"}, "BITBUCKET_API")):
            with self.subTest(settings=settings):
                result = run(**settings)
                self.assertEqual(result.returncode, 78, result.stderr)
                self.assertIn(message, result.stderr)
        check = run("--check", UI_PORT="9102")
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertIn("would listen on http://127.0.0.1:9102/ for admin "
                      "(Bitbucket tokens: BITBUCKET_ACCESS_TOKEN, BITBUCKET_ACCESS_TOKEN_ASANA_TRACKER)", check.stdout)
        self.assertNotIn(PROJECT_TOKEN, check.stdout + check.stderr)

    # --- Status ----------------------------------------------------------------------------------
    def test_the_pages_show_the_status_helpers_view_of_the_worker(self):
        status, _, body = self.request("GET", "/")
        self.assertEqual(status, 200, body)
        for text in ("asana_tracker", "git@bitbucket.org:rsmaizys/asana_tracker.git", "v1.0.0", "rollback",
                     "laikoma 3", "2/3 veikia", "šis serveris", "deploy@shop.example:/home/deploy/shop",
                     "GitHub: dar nepalaikoma", "v0.4.0", "nepavyko (exit-code)", "Tue 2026-10-13 00:30:00 UTC",
                     "GitHub runner&#x27;is bigsant", "#41"):
            self.assertIn(text, body)
        self.assertNotIn("never-shown-password", body)
        self.assertIn(f"-n {self.library}/bin/server-status", self.sudo_calls())
        status, _, project = self.request("GET", "/project/asana_tracker")
        self.assertEqual(status, 200)
        for text in ("asana-prod-php-fpm", "healthy", "asana-prod-cron", "asana-prod-secrets", "Deploy į prod", "Backup dabar"):
            self.assertIn(text, project)
        self.assertIn("GitHub dar nepalaikomas", self.request("GET", "/project/forsena_docker")[2])
        self.assertEqual(self.request("GET", "/project/missing")[0], 404)
        self.assertEqual(self.request("GET", "/project/..%2Fetc")[0], 404)
        logs = self.request("GET", "/logs")[2]
        self.assertIn("journal of setup-server-runner.service", logs)
        self.assertIn(f"-n {self.library}/bin/server-status --logs 50", self.sudo_calls())

    # --- Actions ---------------------------------------------------------------------------------
    def test_an_action_runs_its_check_first_and_then_exactly_that_command(self):
        command = f"-n {self.library}/bin/add-project"
        status, _, body = self.preview("add-project", env_repo="git@bitbucket.org:rsmaizys/forsena_docker.git",
                                       code_repo="git@bitbucket.org:rsmaizys/forsena.git",
                                       targets="prod=ssh://deploy@shop.example/home/deploy/shop\nlocal")
        self.assertEqual(status, 200, body)
        arguments = ("--target prod=ssh://deploy@shop.example/home/deploy/shop --target local "
                     "git@bitbucket.org:rsmaizys/forsena_docker.git git@bitbucket.org:rsmaizys/forsena.git")
        self.assertEqual(self.sudo_calls("add-project"), [f"{command} --check {arguments}"])
        self.assertIn("Vykdyti", body)
        status, page = self.confirm(body)
        self.assertEqual(status, 200, page)
        self.assertIn("baigta, kodas 0", page)
        self.assertIn(f"stub ran: {self.library}/bin/add-project {arguments}", page)
        self.assertEqual(self.sudo_calls("add-project"), [f"{command} --check {arguments}", f"{command} {arguments}"])
        log = self.log.read_text()
        self.assertRegex(log, rf"user=admin client=127\.0\.0\.1 check add-project: sudo {re.escape(command)} --check .* -> exit 0\n")
        self.assertRegex(log, rf"user=admin client=127\.0\.0\.1 run job=\S+ add-project: sudo {re.escape(command)} --target")
        self.assertRegex(log, r"finished job=\S+ add-project -> exit 0\n")
        # The preview is used once.
        pending = re.search(r'name="pending" value="([^"]+)"', body)[1]
        self.assertEqual(self.request("POST", "/run", {"csrf": self.csrf(), "pending": pending})[0], 400)
        self.assertEqual(len(self.sudo_calls("add-project")), 2)

    def test_setup_updates_to_a_release_tag_without_a_password(self):
        command = f"-n {self.library}/bin/update-setup"
        status, _, body = self.preview("update-setup", setup_tag="v2.7.0")
        self.assertEqual(status, 200, body)
        self.assertEqual(self.sudo_calls("update-setup"), [f"{command} --check v2.7.0"])
        newest = self.preview("update-setup", setup_tag="")
        self.assertEqual(newest[0], 200, newest[2])
        self.assertEqual(self.sudo_calls("update-setup")[-1], f"{command} --check", "empty: the newest release")
        for tag in ("master", "v2.7", "v2.7.0; rm -rf /"):
            with self.subTest(tag=tag):
                self.assertEqual(self.preview("update-setup", setup_tag=tag)[0], 400)

    def test_a_failed_check_offers_no_run_and_prepare_host_may_need_attention(self):
        (self.stubs / "exit").write_text("65\n")
        status, _, body = self.preview("remove-project", name="asana_tracker")
        self.assertEqual(status, 200)
        self.assertIn(f"stub ran: {self.library}/bin/remove-project --check asana_tracker", body)
        self.assertNotIn('name="pending"', body)
        (self.stubs / "exit").write_text("1\n")
        self.assertIn('name="pending"', self.preview("prepare-host", setup_ref="v2.6.0")[2], '"Needs attention" may run')
        self.assertIn("-> exit 65", self.log.read_text())

    def test_bad_values_are_refused_before_anything_runs(self):
        cases = (("add-project", {"env_repo": "--upload-pack=touch /tmp/x"}, "Aplinkos repo"),
                 ("add-project", {"env_repo": "git@bitbucket.org:ws/shop.git; rm -rf /"}, "Aplinkos repo"),
                 ("add-project", {"env_repo": "file:///etc"}, "Aplinkos repo"),
                 ("add-project", {"env_repo": "git@bitbucket.org:ws/shop.git", "name": "Bad Name"}, "Vardas"),
                 ("add-project", {"env_repo": "git@bitbucket.org:ws/shop.git", "name": "-rf"}, "Vardas"),
                 ("add-project", {"env_repo": "git@bitbucket.org:ws/shop.git", "targets": "ftp://x/y"}, "Netinkamas taikinys"),
                 ("add-project", {"env_repo": "git@bitbucket.org:ws/shop.git", "targets": "ssh://u@h/p -oProxyCommand=x"}, "Netinkamas taikinys"),
                 ("add-project", {"env_repo": "git@bitbucket.org:ws/shop.git", "targets": "dev=local"}, "Netinkamas taikinys"),
                 ("remove-project", {"name": "../etc"}, "Projektas"),
                 ("install-backups", {"at": "25:00"}, "Laikas"),
                 ("install-metrics", {"address": "vpn.example"}, "Adresas"),
                 ("install-metrics", {"allow": "0.0.0.0/0"}, "ne visus adresus"),
                 ("add-backup-key", {"public_key": "-----BEGIN OPENSSH PRIVATE KEY-----", "key_name": "tomas",
                                     "sources": "10.8.0.0/24"}, "viešąjį raktą"),
                 ("update", {"server_ref": "--upload-pack=x"}, "setup-server ref"),
                 ("update", {"setup_ref": "v1..v2"}, "setup ref"),
                 ("update", {}, "bent vieną"),
                 ("install-runner", {"provider": "gitlab", "dialog": "x"}, "bitbucket arba github"),
                 ("pipeline", {"project": "asana_tracker", "pipeline": "deploy-production", "ref": "$(id)"}, "REF"),
                 ("pipeline", {"project": "asana_tracker", "pipeline": "custom-anything"}, "Nežinomas pipeline"),
                 ("no-such-action", {}, "Nežinomas veiksmas"))
        for action, fields, message in cases:
            with self.subTest(action=action, fields=fields):
                status, _, body = self.preview(action, **fields)
                self.assertEqual(status, 400, body)
                self.assertIn(message, body)
        self.assertEqual([call for call in self.sudo_calls() if "server-status" not in call], [])
        self.assertEqual(self.api.requests, [])

    def test_the_runner_dialog_goes_to_stdin_and_is_never_shown_or_logged(self):
        status, _, body = self.preview("install-runner", provider="bitbucket", dialog=DIALOG)
        self.assertEqual(status, 200, body)
        self.assertEqual(self.sudo_calls("install-runner"), [f"-n {self.library}/bin/install-runner --check --provider bitbucket"])
        self.assertNotIn(SECRET, body, "the stub echoes its stdin: the UI masks it")
        self.assertIn("--OAuthClientSecret &lt;hidden&gt;", body)
        status, page = self.confirm(body)
        self.assertIn("baigta, kodas 0", page)
        self.assertNotIn(SECRET, page)
        given = (self.stubs / "sudo.stdin").read_text().split("\n----\n")
        self.assertEqual(given[:2], [DIALOG.rstrip("\n")] * 2, "the check and the run both got the whole dialog")
        self.assertNotIn(SECRET, self.everything_written())
        self.assertNotIn(SECRET, self.request("GET", "/logs")[2])

    def test_update_runs_as_the_owner_of_the_admin_clone(self):
        status, _, body = self.preview("update", server_ref="v0.5.0", setup_ref="origin/master")
        self.assertEqual(status, 200, body)
        self.assertEqual(self.sudo_calls("update"),
                         ["-n -H -u admin /home/admin/setup-server/bin/update --check --server-ref v0.5.0 --setup-ref origin/master"])
        (self.library / "install.env").write_text("VERSION=v0.4.0\n")
        self.assertIn("Nežinomas administratoriaus klonas", self.preview("update", server_ref="v0.5.0")[2])

    def test_one_action_at_a_time(self):
        (self.stubs / "sleep").write_text("3\n")
        first = self.preview("install-backups", at="02:15")[2]
        second = self.preview("install-metrics", token="new")[2]
        pending = re.search(r'name="pending" value="([^"]+)"', first)[1]
        self.assertEqual(self.request("POST", "/run", {"csrf": self.csrf(), "pending": pending})[0], 303)
        status, _, body = self.request("POST", "/run", {"csrf": self.csrf(),
                                                        "pending": re.search(r'name="pending" value="([^"]+)"', second)[1]})
        self.assertEqual(status, 409, body)
        self.assertIn("Dar vykdomas kitas veiksmas", body)
        self.assertEqual(self.sudo_calls("install-metrics"), [f"-n {self.library}/bin/install-metrics --check --new-token"])
        for _ in range(100):  # the first one finishes on its own
            if "finished" in self.log.read_text():
                break
            time.sleep(0.1)
        self.assertIn("finished", self.log.read_text())

    def test_nothing_runs_without_the_action_log(self):
        self.log.mkdir()  # a directory where the log should be: it cannot be appended to
        body = self.preview("install-backups")[2]
        self.assertIn("vykdyti negalima", body)
        self.assertNotIn('name="pending"', body)

    # --- Bitbucket -------------------------------------------------------------------------------
    def test_a_deploy_starts_the_custom_pipeline_with_its_variables_and_the_projects_token(self):
        status, _, body = self.preview("pipeline", project="asana_tracker", pipeline="deploy-production",
                                       ref="v1.2.0", images="yes", backup="yes")
        self.assertEqual(status, 200, body)
        self.assertIn("BITBUCKET_ACCESS_TOKEN_ASANA_TRACKER", body)
        posted = [request for request in self.api.requests if request[0] == "POST"]
        self.assertEqual(posted, [], "the preview sends nothing")
        status, page = self.confirm(body)
        self.assertEqual(status, 200, page)
        method, path, headers, sent = [request for request in self.api.requests if request[0] == "POST"][0]
        self.assertEqual(path, "/2.0/repositories/rsmaizys/asana_tracker/pipelines/")
        self.assertEqual(headers["Authorization"], f"Bearer {PROJECT_TOKEN}")
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(sent, {"target": {"type": "pipeline_ref_target", "ref_type": "branch", "ref_name": "main",
                                           "selector": {"type": "custom", "pattern": "deploy-production"}},
                                "variables": [{"key": "REF", "value": "v1.2.0"}, {"key": "IMAGES", "value": "yes"},
                                              {"key": "ROLLBACK", "value": "no"}, {"key": "BACKUP", "value": "yes"}]})
        self.assertIn("https://bitbucket.org/rsmaizys/asana_tracker/pipelines/results/42", page)
        self.assertRegex(self.log.read_text(), r"pipeline asana_tracker: POST /repositories/rsmaizys/asana_tracker/pipelines/ "
                                               r"deploy-production branch=main REF=v1\.2\.0 IMAGES=yes ROLLBACK=no BACKUP=yes "
                                               r"\(token BITBUCKET_ACCESS_TOKEN_ASANA_TRACKER\) -> HTTP 201 #42")
        written = self.everything_written() + body + page
        self.assertNotIn(PROJECT_TOKEN, written)
        self.assertNotIn(GENERAL_TOKEN, written)

    def test_backup_now_uses_the_general_token_and_a_redirect_is_not_followed(self):
        status, _, body = self.preview("pipeline", project="shop-two", pipeline="backup-now", branch="master")
        self.assertEqual(status, 200, body)
        self.assertIn("token'as: BITBUCKET_ACCESS_TOKEN</p>", body)
        status, page = self.confirm(body)
        self.assertEqual(status, 502, page)
        self.assertIn("HTTP 302", page)
        posted = [request for request in self.api.requests if request[0] == "POST"]
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0][2]["Authorization"], f"Bearer {GENERAL_TOKEN}")
        self.assertEqual(posted[0][3]["variables"], [])
        self.assertEqual(posted[0][3]["target"]["selector"]["pattern"], "backup-now")
        self.assertFalse([request for request in self.api.requests if request[1].startswith("/elsewhere")],
                         "the token never follows a redirect")
        self.assertNotIn(GENERAL_TOKEN, self.everything_written() + page)

    def test_the_last_runs_are_read_with_the_token_and_github_projects_have_no_triggers(self):
        status, _, page = self.request("GET", "/project/asana_tracker")
        self.assertIn("pipelines/results/41", page)
        listed = [request for request in self.api.requests if request[0] == "GET"]
        self.assertIn(("/2.0/repositories/rsmaizys/asana_tracker/pipelines/?sort=-created_on&pagelen=10",
                       f"Bearer {PROJECT_TOKEN}"), [(request[1], request[2]["Authorization"]) for request in listed])
        status, _, body = self.preview("pipeline", project="forsena_docker", pipeline="deploy-stage")
        self.assertEqual(status, 400)
        self.assertIn("GitHub dar nepalaikomas", body)


if __name__ == "__main__":
    import unittest
    unittest.main()
