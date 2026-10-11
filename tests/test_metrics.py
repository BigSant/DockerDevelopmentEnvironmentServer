"""Worker /metrics: backups, deploys, runner and disk from the build roots, behind a token and an allowlist.

The endpoint runs for real on 127.0.0.1 over a temporary projects tree; systemctl is a stub.
Format check (opt-in where promtool is missing): SETUP_PROMTOOL_IMAGE=prom/prometheus:latest.
B level (opt-in, SETUP_SERVER_UBUNTU_IMAGE=ubuntu:22.04): install-metrics for real inside an Ubuntu
container, systemd-analyze verify of its unit, and a scrape of the installed command as the setup account.
"""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request

from test_scripts import DOCKER_RUN, ROOT, ScriptTest, UBUNTU

TOKEN = "worker-metrics-test-token-0123456789"
PROMTOOL = os.environ.get("SETUP_PROMTOOL_IMAGE")
SYSTEMCTL_ACTIVE = 'printf "LoadState=loaded\\nActiveState=active\\nSubState=running\\nNRestarts=3\\n"\n'
SAMPLE = re.compile(r'([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})? (\S+)$')


def parse(body):
    """{(name, ((label, value), ...)): float} of an exposition; HELP/TYPE lines are skipped."""
    samples = {}
    for line in body.splitlines():
        if not line or line.startswith("#"):
            continue
        name, labels, value = SAMPLE.match(line).groups()
        pairs = tuple(sorted(re.findall(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"', labels or "")))
        samples[(name, pairs)] = float(value)
    return samples


def value(samples, name, **labels):
    return samples[(name, tuple(sorted(labels.items())))]


def fetch(base, path="/metrics", token=None):
    request = urllib.request.Request(base + path)
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, dict(response.headers), response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read().decode()


class WorkerTree:
    """A fake /srv/setup-server: build roots with backups and deploy histories."""

    def __init__(self, root):
        self.root = root
        self.projects = root / "projects"
        self.projects.mkdir(parents=True)

    def project(self, name, *environments, layout="app"):
        directory = self.projects / name / layout
        (directory / "env").mkdir(parents=True)
        (directory / "Makefile").write_text("include $(SETUP_DIRECTORY)/docker/project.mk\n")
        for environment in environments:
            (directory / f"env/{environment}.env").write_text("DOMAIN=example.test\n")
        return directory

    def backup(self, directory, name, age, size):
        path = directory / ".generated/backups" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        moment = time.time() - age
        os.utime(path, (moment, moment))
        return path

    def deployments(self, directory, environment, entries):
        path = directory / f".generated/release/{environment}/deployments.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entries, indent=2) + "\n")


def entry(ref, commit, at, rollback=False, **extra):
    return {"ref": ref, "commit": commit, "at": at, "rollback": rollback, "backup": None, **extra}


class SharedHelpersTest(unittest.TestCase):
    """bin/backup-projects and bin/worker-metrics each carry the build-root lookup and the backup name
    patterns (the installed copies are single files); they must not drift apart."""

    SHARED = ("environment_directory", "NAME", "SERIES")

    def definitions(self, name):
        import ast
        source = (ROOT / "bin" / name).read_text()
        found = {}
        for node in ast.parse(source).body:
            if isinstance(node, ast.FunctionDef) and node.name in self.SHARED:
                found[node.name] = ast.get_source_segment(source, node)
            elif isinstance(node, ast.Assign) and len(node.targets) == 1 and getattr(node.targets[0], "id", None) in self.SHARED:
                found[node.targets[0].id] = ast.get_source_segment(source, node)
        return found

    def test_the_duplicated_helpers_are_identical(self):
        backups, metrics = self.definitions("backup-projects"), self.definitions("worker-metrics")
        self.assertEqual(sorted(backups), sorted(self.SHARED))
        for name in self.SHARED:
            with self.subTest(name=name):
                self.assertEqual(metrics.get(name), backups[name])


class EndpointTest(ScriptTest):
    def setUp(self):
        super().setUp()
        self.tree = WorkerTree(self.root / "srv")
        self.stub("systemctl", 'echo "$*" >> "$SYSTEMCTL_LOG"\n' + SYSTEMCTL_ACTIVE)
        self.systemctl_log = self.root / "systemctl.log"

    def start(self, **settings):
        env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "SETUP_SERVER_ROOT": str(self.tree.root),
               "SYSTEMCTL_LOG": str(self.systemctl_log), "METRICS_TOKEN": TOKEN, "METRICS_ADDRESS": "127.0.0.1",
               "METRICS_PORT": "0", "METRICS_ALLOWED_IPS": "127.0.0.1", **settings}
        process = subprocess.Popen([str(ROOT / "bin/worker-metrics")], env=env, stderr=subprocess.PIPE, text=True)
        self.addCleanup(process.stderr.close)
        self.addCleanup(process.wait, 10)
        self.addCleanup(process.terminate)
        line = process.stderr.readline()
        match = re.search(r"listening on (\S+):(\d+)", line)
        self.assertIsNotNone(match, line + (process.stderr.read() if process.poll() is not None else ""))
        return f"http://127.0.0.1:{match[2]}"

    def scrape(self, **settings):
        status, headers, body = fetch(self.start(**settings), token=TOKEN)
        self.assertEqual(status, 200, body)
        return body

    def fixtures(self):
        hour = 3600
        asana = self.tree.project("asana_tracker", "prod")
        for age, size in ((2 * hour, 4096), (26 * hour, 3000), (50 * hour, 2000)):
            stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(time.time() - age))
            self.tree.backup(asana, f"worker-prod-{stamp}Z.sql.gz", age, size)
        # A pre-deploy backup is newer but not the nightly series: it never hides a stopped timer.
        self.tree.backup(asana, "asana-prod-20261009T120000000000.sql.gz", 60, 99)
        self.tree.deployments(asana, "prod", [
            entry("v1.0.0", "a" * 40, "2026-10-01T10:00:00+00:00"),
            entry("v1.1.0", "b" * 40, "2026-10-05T10:00:00+00:00"),
            entry("v1.0.0", "a" * 40, "2026-10-06T08:30:00+00:00", rollback=True),
        ])
        forsena = self.tree.project("forsena_docker", "stage", layout="app/docker")
        self.tree.deployments(forsena, "stage", [
            entry("v3.2.0", "c" * 40, "2026-10-07T09:00:00+00:00"),
            entry("v3.3.0", "d" * 40, "2026-10-08T09:00:00+00:00", failed="after-rsync"),
        ])
        self.tree.project("not_deployed")
        self.github_runner("bigsant")
        return asana, forsena

    def github_runner(self, scope, registered=True):
        directory = self.tree.root / "github-runner" / scope
        directory.mkdir(parents=True)
        if registered:
            (directory / ".runner").write_text('{"agentName": "worker1"}\n')

    # --- Access ----------------------------------------------------------------------------------
    def test_without_the_token_or_with_a_wrong_one_it_is_refused(self):
        base = self.start()
        for token in (None, "wrong-token", TOKEN + "x", ""):
            with self.subTest(token=token):
                status, headers, body = fetch(base, token=token)
                self.assertEqual(status, 401, body)
                self.assertEqual(headers.get("WWW-Authenticate"), "Bearer")
                self.assertNotIn("setup_worker", body)
        status, headers, body = fetch(base, token=TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/plain; version=0.0.4; charset=utf-8")
        self.assertEqual(fetch(base, "/other", token=TOKEN)[0], 404)
        self.assertEqual(fetch(base, "/other")[0], 401, "nothing is told without the token")

    def test_an_address_outside_the_allowlist_is_refused_even_with_the_token(self):
        base = self.start(METRICS_ALLOWED_IPS="10.8.0.20 192.168.1.0/24")
        status, _, body = fetch(base, token=TOKEN)
        self.assertEqual(status, 403, body)
        self.assertNotIn("setup_worker", body)
        allowed = self.start(METRICS_ALLOWED_IPS="10.8.0.20,127.0.0.0/8")
        self.assertEqual(fetch(allowed, token=TOKEN)[0], 200)

    def test_it_does_not_start_without_a_usable_token_or_allowlist(self):
        env = {**os.environ, "SETUP_SERVER_ROOT": str(self.tree.root), "METRICS_ADDRESS": "127.0.0.1", "METRICS_PORT": "0"}
        for settings, message in (({"METRICS_ALLOWED_IPS": "127.0.0.1"}, "METRICS_TOKEN (24+ characters)"),
                                  ({"METRICS_TOKEN": "short", "METRICS_ALLOWED_IPS": "127.0.0.1"}, "METRICS_TOKEN (24+"),
                                  ({"METRICS_TOKEN": TOKEN}, "METRICS_ALLOWED_IPS"),
                                  ({"METRICS_TOKEN": TOKEN, "METRICS_ALLOWED_IPS": "all"}, "Invalid IP/CIDR"),
                                  ({"METRICS_TOKEN": TOKEN, "METRICS_ALLOWED_IPS": "10.8.0.0/8; deny"}, "Invalid IP/CIDR"),
                                  ({"METRICS_TOKEN": TOKEN, "METRICS_ALLOWED_IPS": "127.0.0.1",
                                    "METRICS_ADDRESS": "vpn.example"}, "METRICS_ADDRESS"),
                                  ({"METRICS_TOKEN": TOKEN, "METRICS_ALLOWED_IPS": "127.0.0.1",
                                    "METRICS_PORT": "99999"}, "METRICS_PORT")):
            with self.subTest(settings=settings):
                result = subprocess.run([str(ROOT / "bin/worker-metrics")], env={**env, **settings},
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 78, result.stderr)
                self.assertIn(message, result.stderr)
                self.assertNotIn("short", result.stderr, "the token is never printed")

    # --- Values from the worker's files ----------------------------------------------------------
    def test_backup_age_size_and_count_come_from_the_newest_nightly_backup(self):
        self.fixtures()
        samples = parse(self.scrape())
        age = value(samples, "setup_worker_backup_newest_age_seconds", project="asana_tracker", environment="prod")
        self.assertAlmostEqual(age, 7200, delta=60)
        self.assertEqual(value(samples, "setup_worker_backup_newest_size_bytes", project="asana_tracker",
                               environment="prod"), 4096)
        self.assertEqual(value(samples, "setup_worker_backups", project="asana_tracker", environment="prod"), 3)
        # Deployed to stage from here but never backed up: no age, a zero count the alert sees.
        self.assertEqual(value(samples, "setup_worker_backups", project="forsena_docker", environment="stage"), 0)
        self.assertNotIn(("setup_worker_backup_newest_age_seconds",
                          (("environment", "stage"), ("project", "forsena_docker"))), samples)
        self.assertFalse([key for key in samples if ("project", "not_deployed") in key[1]])

    def test_the_last_deploy_tells_a_rollback_and_a_failed_deploy_apart(self):
        self.fixtures()
        samples = parse(self.scrape())
        asana = dict(project="asana_tracker", environment="prod")
        self.assertEqual(value(samples, "setup_worker_deploy_last_timestamp_seconds", **asana), 1791275400)
        self.assertEqual(value(samples, "setup_worker_deploy_last_success", **asana), 1)
        self.assertEqual(value(samples, "setup_worker_deploy_last_rollback", **asana), 1)
        self.assertEqual(value(samples, "setup_worker_deploy_last_success_timestamp_seconds", **asana), 1791275400)
        self.assertEqual(value(samples, "setup_worker_deploy_last_info", ref="v1.0.0", commit="a" * 12, **asana), 1)
        forsena = dict(project="forsena_docker", environment="stage")
        self.assertEqual(value(samples, "setup_worker_deploy_last_timestamp_seconds", **forsena), 1791450000)
        self.assertEqual(value(samples, "setup_worker_deploy_last_success", **forsena), 0)
        self.assertEqual(value(samples, "setup_worker_deploy_last_rollback", **forsena), 0)
        # The last deploy that completed is the one before the failure.
        self.assertEqual(value(samples, "setup_worker_deploy_last_success_timestamp_seconds", **forsena), 1791363600)
        self.assertEqual(value(samples, "setup_worker_deploy_last_info", ref="v3.3.0", commit="d" * 12, **forsena), 1)
        for group in ("backups", "deploys", "runner", "github_runner", "disk"):
            self.assertEqual(value(samples, "setup_metrics_group_up", group=group), 1, group)

    def test_a_broken_history_clears_only_its_group_and_keeps_the_other_projects(self):
        asana, forsena = self.fixtures()
        (forsena / ".generated/release/stage/deployments.json").write_text("{not json")
        samples = parse(self.scrape())
        self.assertEqual(value(samples, "setup_metrics_group_up", group="deploys"), 0)
        self.assertEqual(value(samples, "setup_metrics_group_up", group="backups"), 1)
        self.assertEqual(value(samples, "setup_worker_deploy_last_success", project="asana_tracker", environment="prod"), 1)
        self.assertFalse([key for key in samples if key[0].startswith("setup_worker_deploy")
                          and ("project", "forsena_docker") in key[1]])

    def test_long_refs_are_cut_and_label_values_escaped(self):
        directory = self.tree.project("odd", "prod")
        self.tree.deployments(directory, "prod", [entry('feature/"quoted"\\' + "x" * 100, "e" * 40, "2026-10-08T09:00:00Z")])
        body = self.scrape()
        self.assertIn('ref="feature/\\"quoted\\"\\\\' + "x" * 47 + '"', body)  # 64 characters before escaping
        self.assertEqual(value(parse(body), "setup_worker_deploy_last_timestamp_seconds", project="odd",
                               environment="prod"), 1791450000)

    # --- Runner and disk -------------------------------------------------------------------------
    def test_the_runner_state_is_read_from_systemd_without_changing_it(self):
        samples = parse(self.scrape())
        self.assertEqual(value(samples, "setup_worker_runner_up"), 1)
        self.assertEqual(value(samples, "setup_worker_runner_state", state="active"), 1)
        self.assertEqual(value(samples, "setup_worker_runner_restarts"), 3)
        calls = self.systemctl_log.read_text().splitlines()
        self.assertTrue(calls and all(call.startswith("show ") and call.endswith(" setup-server-runner.service")
                                      for call in calls), calls)

    def test_a_stopped_missing_or_unreachable_runner(self):
        for output, up, state in (('printf "LoadState=loaded\\nActiveState=failed\\nSubState=failed\\nNRestarts=0\\n"\n', 1, "failed"),
                                  ('printf "LoadState=not-found\\nActiveState=inactive\\nSubState=dead\\nNRestarts=0\\n"\n', 1, "not-found"),
                                  ('echo "Failed to connect to bus" >&2; exit 1\n', 0, None)):
            with self.subTest(state=state):
                self.stub("systemctl", output)
                samples = parse(self.scrape())
                self.assertEqual(value(samples, "setup_metrics_group_up", group="runner"), up)
                if state:
                    self.assertEqual(value(samples, "setup_worker_runner_up"), 0)
                    self.assertEqual(value(samples, "setup_worker_runner_state", state=state), 1)
                else:
                    self.assertNotIn(("setup_worker_runner_up", ()), samples)

    def test_each_registered_github_runner_is_read_from_systemd_beside_the_bitbucket_one(self):
        self.github_runner("bigsant")
        self.github_runner("bigsant.forsena_docker")
        self.github_runner("half_done", registered=False)  # unpacked, never registered: not a runner yet
        self.github_runner("Not-A-Scope")
        self.stub("systemctl", 'echo "$*" >> "$SYSTEMCTL_LOG"\nfor unit; do :; done\ncase "$unit" in\n'
                               '  *@bigsant.forsena_docker.service) printf "LoadState=not-found\\nActiveState=inactive\\nNRestarts=0\\n";;\n'
                               '  *@bigsant.service) printf "LoadState=loaded\\nActiveState=active\\nNRestarts=2\\n";;\n'
                               '  *) printf "LoadState=loaded\\nActiveState=failed\\nNRestarts=7\\n";;\nesac\n')
        samples = parse(self.scrape())
        self.assertEqual(value(samples, "setup_worker_github_runner_up", scope="bigsant"), 1)
        self.assertEqual(value(samples, "setup_worker_github_runner_state", scope="bigsant", state="active"), 1)
        self.assertEqual(value(samples, "setup_worker_github_runner_restarts", scope="bigsant"), 2)
        self.assertEqual(value(samples, "setup_worker_github_runner_up", scope="bigsant.forsena_docker"), 0)
        self.assertEqual(value(samples, "setup_worker_github_runner_state", scope="bigsant.forsena_docker",
                               state="not-found"), 1)
        self.assertEqual({dict(key[1]).get("scope") for key in samples if key[0].startswith("setup_worker_github")},
                         {"bigsant", "bigsant.forsena_docker"})
        # The Bitbucket runner keeps its own unlabelled series.
        self.assertEqual(value(samples, "setup_worker_runner_up"), 0)
        self.assertEqual(value(samples, "setup_worker_runner_state", state="failed"), 1)
        self.assertEqual(value(samples, "setup_metrics_group_up", group="github_runner"), 1)
        calls = self.systemctl_log.read_text().splitlines()
        self.assertTrue(all(call.startswith("show ") for call in calls), calls)
        self.assertIn("setup-server-github-runner@bigsant.service", " ".join(calls))

    def test_without_github_runners_there_are_no_github_series_and_an_unreadable_one_clears_its_group(self):
        samples = parse(self.scrape())
        self.assertFalse([key for key in samples if key[0].startswith("setup_worker_github")])
        self.assertEqual(value(samples, "setup_metrics_group_up", group="github_runner"), 1)
        self.github_runner("bigsant")
        self.stub("systemctl", 'case "$*" in *github*) echo "Failed to connect to bus" >&2; exit 1;; esac\n' + SYSTEMCTL_ACTIVE)
        samples = parse(self.scrape())
        self.assertEqual(value(samples, "setup_metrics_group_up", group="github_runner"), 0)
        self.assertEqual(value(samples, "setup_metrics_group_up", group="runner"), 1)
        self.assertEqual(value(samples, "setup_worker_runner_up"), 1)

    def test_disk_usage_of_the_filesystem_holding_the_worker_root(self):
        samples = parse(self.scrape())
        stats = os.statvfs(self.tree.root)
        path = str(self.tree.root)
        self.assertEqual(value(samples, "setup_worker_disk_size_bytes", path=path), stats.f_blocks * stats.f_frsize)
        available = value(samples, "setup_worker_disk_available_bytes", path=path)
        self.assertAlmostEqual(available, stats.f_bavail * stats.f_frsize, delta=64 * 1048576)
        used = value(samples, "setup_worker_disk_used_bytes", path=path)
        self.assertAlmostEqual(used, (stats.f_blocks - stats.f_bfree) * stats.f_frsize, delta=64 * 1048576)

    def test_once_prints_the_exposition_without_serving(self):
        self.fixtures()
        result = subprocess.run([str(ROOT / "bin/worker-metrics"), "--once"], capture_output=True, text=True, timeout=30,
                                env={**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}",
                                     "SETUP_SERVER_ROOT": str(self.tree.root), "SYSTEMCTL_LOG": str(self.systemctl_log)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('setup_worker_backups{project="asana_tracker",environment="prod"} 3', result.stdout)

    def test_promtool_accepts_the_exposition_with_its_naming_lint(self):
        if not (shutil.which("promtool") or PROMTOOL):
            self.skipTest("Install promtool or set SETUP_PROMTOOL_IMAGE (e.g. prom/prometheus:latest)")
        self.fixtures()
        body = self.scrape()
        command = ["promtool"] if shutil.which("promtool") else [*DOCKER_RUN, "-i", "--entrypoint", "promtool", PROMTOOL]
        result = subprocess.run(command + ["check", "metrics"], input=body, capture_output=True, text=True, timeout=120)
        # Exit 0: the exposition parses and the naming lint has nothing to report (it exits 3 otherwise).
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr + body)


class InstallMetricsTest(ScriptTest):
    def setUp(self):
        super().setUp()
        os.environ["SETUP_SERVER_SYSTEMD"] = str(self.root / "systemd")
        os.environ["SETUP_SERVER_SBIN"] = str(self.root / "sbin")
        os.environ["SETUP_SERVER_CONFIG"] = str(self.root / "etc")
        for key in ("SETUP_SERVER_SYSTEMD", "SETUP_SERVER_SBIN", "SETUP_SERVER_CONFIG"):
            self.addCleanup(os.environ.pop, key)

    def test_check_shows_the_unit_and_settings_and_changes_nothing(self):
        result = self.run_script("install-metrics", "--check", "--address", "10.8.0.14", "--allow", "10.8.0.20")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"would write {self.root}/systemd/setup-server-metrics.service", result.stdout)
        self.assertIn("User=setup-test-account-missing", result.stdout)
        self.assertIn(f"EnvironmentFile={self.root}/etc/metrics.env", result.stdout)
        self.assertIn("ProtectSystem=strict", result.stdout)
        self.assertIn("METRICS_ADDRESS=10.8.0.14", result.stdout)
        self.assertIn('METRICS_ALLOWED_IPS="10.8.0.20"', result.stdout)
        self.assertIn("METRICS_TOKEN=<new, hidden>", result.stdout)
        self.assertIn("would listen on http://10.8.0.14:9101/metrics", result.stdout)
        for name in ("systemd", "sbin", "etc", "srv"):
            self.assertFalse((self.root / name).exists(), "--check changes nothing")

    def test_the_loopback_default_needs_no_allowlist_but_a_vpn_address_does(self):
        result = self.run_script("install-metrics", "--check")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("METRICS_ADDRESS=127.0.0.1", result.stdout)
        self.assertIn('METRICS_ALLOWED_IPS="127.0.0.1 ::1"', result.stdout)
        refused = self.run_script("install-metrics", "--check", "--address", "10.8.0.14")
        self.assertEqual(refused.returncode, 64)
        self.assertIn("--allow", refused.stderr)

    def test_bad_settings_are_refused(self):
        for arguments, code, message in ((["--allow", "all"], 78, "Invalid IP/CIDR"),
                                         (["--address", "vpn.example", "--allow", "10.8.0.20"], 78, "METRICS_ADDRESS"),
                                         (["--port", "http"], 78, "METRICS_PORT"),
                                         (["--bogus"], 64, "Usage")):
            with self.subTest(arguments=arguments):
                result = self.run_script("install-metrics", "--check", *arguments)
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                self.assertIn(message, result.stderr)

    def test_installing_needs_root(self):
        if os.getuid() == 0:
            self.skipTest("runs as root")
        self.assertEqual(self.run_script("install-metrics").returncode, 77)


@unittest.skipUnless(UBUNTU, "Set SETUP_SERVER_UBUNTU_IMAGE (e.g. ubuntu:22.04) to check on a real Ubuntu")
class RealUbuntuMetricsTest(unittest.TestCase):
    def test_installed_unit_verifies_and_the_setup_account_serves_the_metrics(self):
        script = r"""set -eu
mkdir -p /stubs
printf '#!/bin/sh\ncase "$1" in show) printf "LoadState=loaded\\nActiveState=active\\nSubState=running\\nNRestarts=0\\n";; esac\nexit 0\n' > /stubs/systemctl
chmod +x /stubs/systemctl
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null && apt-get install -y -qq systemd python3 curl >/dev/null
useradd --system --create-home --home-dir /srv/setup-server --shell /bin/bash setup
app=/srv/setup-server/projects/asana_tracker/app
mkdir -p $app/env $app/.generated/backups $app/.generated/release/prod
touch $app/Makefile $app/env/prod.env
printf 'dump' > $app/.generated/backups/worker-prod-20261009T013000Z.sql.gz
printf '[{"ref":"v1","commit":"%040d","at":"2026-10-09T10:00:00+00:00","rollback":false,"backup":null}]' 0 \
    > $app/.generated/release/prod/deployments.json
chown -R setup:setup /srv/setup-server/projects && chmod -R go-rwx /srv/setup-server/projects
# systemctl is a stub (no systemd in the container); the unit is verified below.
PATH=/stubs:$PATH /setup-server/bin/install-metrics --address 127.0.0.1
PATH=/stubs:$PATH /setup-server/bin/install-metrics | grep -c unchanged
# verify only warns (exit 0) about a bad setting: any output is a finding.
systemd-analyze verify /etc/systemd/system/setup-server-metrics.service >/tmp/verify.log 2>&1 || true
cat /tmp/verify.log; [ -s /tmp/verify.log ] || echo UNIT_VERIFIED
stat -c '%U %a %n' /etc/setup-server/metrics.env /usr/local/sbin/setup-server-metrics
# What the unit runs: the setup account with the root file's settings.
set -a; . /etc/setup-server/metrics.env; set +a
PATH=/stubs:$PATH runuser -u setup -- env SETUP_SERVER_ROOT=/srv/setup-server METRICS_TOKEN="$METRICS_TOKEN" \
    METRICS_ADDRESS="$METRICS_ADDRESS" METRICS_PORT="$METRICS_PORT" METRICS_ALLOWED_IPS="$METRICS_ALLOWED_IPS" \
    /usr/local/sbin/setup-server-metrics 2>/tmp/server.log &
for i in 1 2 3 4 5 6 7 8 9 10; do grep -q listening /tmp/server.log && break; sleep 0.5; done
echo "without: $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:9101/metrics)"
curl -sf -H "Authorization: Bearer $METRICS_TOKEN" http://127.0.0.1:9101/metrics
"""
        result = subprocess.run([*DOCKER_RUN, "-v", f"{ROOT}:/setup-server:ro", UBUNTU, "bash", "-c", script],
                                capture_output=True, text=True, timeout=900)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("\n3\n", result.stdout, "a second install changes nothing")
        self.assertIn("UNIT_VERIFIED", output)
        self.assertIn("root 600 /etc/setup-server/metrics.env", result.stdout)
        self.assertIn("root 755 /usr/local/sbin/setup-server-metrics", result.stdout)
        self.assertIn("without: 401", result.stdout)
        self.assertIn('setup_worker_backups{project="asana_tracker",environment="prod"} 1', result.stdout)
        self.assertIn('setup_worker_deploy_last_success{project="asana_tracker",environment="prod"} 1', result.stdout)
        self.assertIn('setup_metrics_group_up{group="backups"} 1', result.stdout)


if __name__ == "__main__":
    unittest.main()
