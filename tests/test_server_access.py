"""Developer access to the deploy targets through this worker: the forced command bin/server-access."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
# Records its argv and answers `ps --format` and `inspect` from the test's files.
FAKE = r'''#!/usr/bin/env python3
import json, os, sys
with open(os.environ['FAKE_CALLS'], 'a') as handle:
    handle.write(json.dumps([os.path.basename(sys.argv[0])] + sys.argv[1:]) + '\n')
words = sys.argv[1:]
if words[:2] == ['--context', words[1] if len(words) > 1 else '']:
    words = words[2:]
if words[:1] == ['ps'] and '--format' in words:
    sys.stdout.write(open(os.environ['FAKE_CONTAINERS']).read())
elif words[:1] == ['inspect'] and '--format' in words:
    sys.stdout.write(open(os.environ['FAKE_MOUNTS']).read() if words[-1].endswith('metrics-docker-1') else '/srv/x\n')
else:
    sys.exit(int(os.environ.get('FAKE_EXIT', '0')))
'''
CONTAINERS = ("shop-prod-php-fpm-1\tphp-fpm\trunning\nshop-prod-database-1\tdatabase\trunning\n"
              "shop-prod-cron-1\tcron\texited\nshop-prod-metrics-docker-1\tmetrics-docker\trunning\n")


class ServerAccessTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.build_root = self.root / "projects/shop"
        app = self.build_root / "app"
        (app / "env").mkdir(parents=True)
        (app / "Makefile").write_text("include x\n")
        (app / "env/common.env").write_text("PROJECT_NAME=shop\n")
        (self.build_root / "target.env").write_text(
            "DEPLOY_CONTEXT=setup-shop\nDEPLOY_SSH=deploy@live.example\nDEPLOY_PATH=/home/deploy/shop\nAPP_UID=1001\nAPP_GID=1002\n")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in ("docker", "ssh"):
            (self.bin / name).write_text(FAKE)
            (self.bin / name).chmod(0o755)
        self.calls = self.root / "calls.jsonl"
        (self.root / "containers").write_text(CONTAINERS)
        (self.root / "mounts").write_text("/var/run/docker.sock\n/srv/data\n")
        pull = self.root / "backup-pull"
        pull.write_text('#!/bin/sh\necho "pull $* [$SSH_ORIGINAL_COMMAND]"\n')
        pull.chmod(0o755)

    def access(self, request, *grants, key="tomas", extra=()):
        environment = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "SETUP_SERVER_ROOT": str(self.root),
                       "SSH_ORIGINAL_COMMAND": request, "SSH_CONNECTION": "10.8.0.20 50000 10.8.0.14 22",
                       "SETUP_SERVER_BACKUP_PULL": str(self.root / "backup-pull"), "FAKE_CALLS": str(self.calls),
                       "FAKE_CONTAINERS": str(self.root / "containers"), "FAKE_MOUNTS": str(self.root / "mounts")}
        arguments = ["--key", key, *extra]
        for grant in grants:
            arguments += ["--grant", grant]
        result = subprocess.run([sys.executable, str(ROOT / "bin/server-access"), *arguments], env=environment,
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []
        self.calls.unlink(missing_ok=True)
        return result, calls

    def last(self, calls):
        """The call that does the work: the last one (lookups come first)."""
        return calls[-1] if calls else None

    def log(self):
        return (self.root / "access.log").read_text()

    def test_access_lists_the_grants_and_backup_requests_go_to_the_pull_command(self):
        result, _ = self.access("access", "shop:prod=phpfpm", "shop:stage=docker")
        self.assertEqual(result.stdout.splitlines(), ["shop prod phpfpm", "shop stage docker"])
        result, _ = self.access("", "shop:prod=phpfpm")
        self.assertIn("shell PROJECT ENV", result.stderr, "a login without a command says what to ask")
        result, _ = self.access("access")
        self.assertIn("reaches no project", result.stdout)
        result, _ = self.access("info shop prod full", "shop:prod=phpfpm", extra=["--allow-fresh"])
        self.assertEqual(result.stdout.strip(), "pull --key tomas --allow-fresh [info shop prod full]")

    def test_shell_runs_in_php_fpm_as_the_project_account_root_or_nobody(self):
        shell = ["sh", "-c", "if command -v bash >/dev/null 2>&1; then exec bash; else exec sh; fi"]
        result, calls = self.access("shell shop prod", "shop:prod=phpfpm")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.last(calls), ["docker", "--context", "setup-shop", "exec", "-i", "-u", "1001:1002",
                                            "shop-prod-php-fpm-1", *shell])
        _, calls = self.access("shell shop prod root", "shop:prod=phpfpm")
        self.assertEqual(self.last(calls)[4:7], ["-i", "-u", "0"])
        _, calls = self.access("exec shop prod readonly -- php bin/console about", "shop:prod=docker")
        self.assertEqual(self.last(calls)[5:], ["-u", "65534:65534", "shop-prod-php-fpm-1", "php", "bin/console", "about"])
        # A key with phpfpm-readonly only is read-only without asking, and never root.
        _, calls = self.access("exec shop prod -- id", "shop:prod=phpfpm-readonly")
        self.assertEqual(self.last(calls)[5:7], ["-u", "65534:65534"])
        result, calls = self.access("shell shop prod root", "shop:prod=phpfpm-readonly")
        self.assertEqual(result.returncode, 65)
        self.assertIn("needs phpfpm", result.stderr)
        self.assertFalse(any("exec" in call for call in calls))

    def test_a_project_or_environment_not_granted_is_refused_and_logged(self):
        for request, grant in (("shell shop prod", "shop:stage=full"), ("shell other prod", "shop:prod=full"),
                               ("host shop prod", "shop:prod=docker"), ("docker shop prod ps", "shop:prod=phpfpm"),
                               ("shell ../shop prod", "shop:prod=full"), ("exec shop prod id", "shop:prod=full"),
                               ("rm -rf /", "shop:prod=full")):
            with self.subTest(request=request):
                result, calls = self.access(request, grant)
                self.assertEqual(result.returncode, 65, result.stderr)
                self.assertTrue(result.stderr.startswith("setup-server-access: "))
                self.assertFalse(any(call[-1:] != ["--format"] and "exec" in call for call in calls))
        self.assertIn("key=tomas from=10.8.0.20 'shell shop prod' -> refused: this key may not reach shop prod", self.log())

    def test_host_is_the_deploy_users_shell_on_the_target_or_the_setup_account_here(self):
        result, calls = self.access("host shop prod", "shop:prod=full")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, [["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "deploy@live.example",
                                  'cd /home/deploy/shop && exec "${SHELL:-/bin/sh}" -l']])
        _, calls = self.access("host shop prod -- tail -n 5 'var/logs/a b.log'", "shop:prod=full")
        self.assertEqual(calls[0][-1], "cd /home/deploy/shop && tail -n 5 'var/logs/a b.log'")
        # A local target: no ssh, a shell of this account in the project root.
        (self.build_root / "target.env").write_text("DEPLOY_CONTEXT=default\nDEPLOY_SSH=\nDEPLOY_PATH=\nAPP_UID=999\nAPP_GID=999\n")
        result, calls = self.access("host shop prod -- pwd", "shop:prod=full")
        self.assertEqual(calls, [])
        self.assertEqual(result.stdout.strip(), str(self.build_root))
        self.assertIn("started (full): sh -c", self.log())
        self.assertIn("-> exit 0", self.log())

    def test_docker_verbs_reach_only_the_projects_containers_with_the_allowed_options(self):
        _, calls = self.access("docker shop prod ps -a", "shop:prod=docker")
        self.assertEqual(self.last(calls), ["docker", "--context", "setup-shop", "ps", "-a", "--filter",
                                            "label=com.docker.compose.project=shop-prod"])
        _, calls = self.access("docker shop prod logs --tail 100 -f cron", "shop:prod=docker")
        self.assertEqual(self.last(calls)[3:], ["logs", "--tail", "100", "-f", "shop-prod-cron-1"])
        _, calls = self.access("docker shop prod restart php-fpm shop-prod-database-1", "shop:prod=docker")
        self.assertEqual(self.last(calls)[3:], ["restart", "shop-prod-php-fpm-1", "shop-prod-database-1"])
        _, calls = self.access("docker shop prod exec --root -w /var/www/html database mysql -e 'show tables'", "shop:prod=full")
        self.assertEqual(self.last(calls)[3:], ["exec", "-i", "-u", "0", "-w", "/var/www/html", "shop-prod-database-1",
                                                "mysql", "-e", "show tables"])
        _, calls = self.access("docker shop prod stats", "shop:prod=docker")
        self.assertEqual(self.last(calls)[3:], ["stats", "--no-stream", "shop-prod-php-fpm-1", "shop-prod-database-1",
                                                "shop-prod-metrics-docker-1"])
        for request, message in (("docker shop prod run alpine", "not allowed"),
                                 ("docker shop prod exec --privileged php-fpm sh", "--privileged is not allowed"),
                                 ("docker shop prod exec metrics-docker sh", "holds the Docker socket"),
                                 ("docker shop prod exec cron sh", "exited"),
                                 ("docker shop prod logs other-prod-php-fpm-1", "no container or service of shop-prod"),
                                 ("docker shop prod logs --since '1h; rm' php-fpm", "not allowed"),
                                 ("docker shop prod stop -t 0 php-fpm", "containers only"),
                                 ("docker shop prod ps --format '{{json .}}'", "only -a")):
            with self.subTest(request=request):
                result, calls = self.access(request, "shop:prod=docker")
                self.assertEqual(result.returncode, 65)
                self.assertIn(message, result.stderr)
                self.assertFalse(any(call[3:4] in (["exec"], ["run"], ["stop"]) for call in calls))

    def test_the_log_keeps_one_line_per_answer_whatever_the_request_holds(self):
        self.access("shell shop prod\nforged line", "shop:prod=phpfpm")
        self.assertEqual(len(self.log().splitlines()), 1)
        self.assertIn("'shell shop prod\\nforged line'", self.log())

    def test_the_childs_exit_status_is_the_answer(self):
        os.environ["FAKE_EXIT"] = "3"
        self.addCleanup(os.environ.pop, "FAKE_EXIT", None)
        result, _ = self.access("exec shop prod -- false", "shop:prod=phpfpm")
        self.assertEqual(result.returncode, 3)
        self.assertTrue(self.log().rstrip().endswith("-> exit 3"))

if __name__ == "__main__":
    unittest.main()
