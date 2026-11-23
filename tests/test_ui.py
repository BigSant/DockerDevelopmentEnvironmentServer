"""The web UI (bin/web-ui), its installer (bin/install-ui) and the read-only status helper (bin/server-status).

The UI runs for real on 127.0.0.1 over a temporary worker tree. sudo is a stub that records every argv and
stdin and runs the real status helper behind it (as this user, with docker, systemctl and journalctl
stubbed); Bitbucket is a fake API on 127.0.0.1. No root, no network.
"""

import base64
from urllib.parse import quote
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
ROOT_COMMANDS = ("add-project", "remove-project", "install-backups", "install-metrics", "install-runner", "install-ssh",
                 "add-backup-key", "backup-projects", "move-bitbucket-ssh", "prepare-host", "server-status", "server-unit",
                 "tool-access", "update-setup")


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
case " $* " in *" logs "*) for last; do :; done; echo "2026-10-12T00:00:01Z first line of $last"
  echo "2026-10-12T00:00:02Z second line of $last" >&2; exit 0;; esac
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
# The installed setup-server-metrics: --once prints an exposition.
METRICS_STUB = ('[ "$*" = --once ] || exit 64\n'
                'printf "# HELP setup_metrics_group_up Whether every part\\nsetup_metrics_group_up{group=\\"backups\\"} 1\\n'
                'setup_metrics_group_up{group=\\"runner\\"} 0\\nsetup_worker_backup_size_bytes{project=\\"asana\\"} 4096\\n"\n')
JOURNALCTL_STUB = 'echo "$*" >> "$STUB_DIR/journalctl.calls"\nfor unit; do :; done\necho "2026-10-12T00:30:00+0000 host journal of ${unit#--unit=}"\n'


def files_archive(directory, environment, age, members):
    """A files backup as setup's make files-backup writes it: a tar.gz of code-tree paths, members without ./;
    members maps a path to bytes (a file), None (a directory) or a str (a symlink to it)."""
    import io
    import tarfile
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(time.time() - age))
    path = directory / f".generated/backups/files-{environment}-{stamp}Z.tar.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    moment = time.time() - age
    with tarfile.open(path, "w:gz") as archive:
        for name, content in sorted(members.items()):
            info = tarfile.TarInfo(name)
            info.mtime = int(moment)
            if content is None:
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            elif isinstance(content, str):
                info.type, info.linkname = tarfile.SYMTYPE, content
                archive.addfile(info)
            else:
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
    os.utime(path, (moment, moment))
    return path.name


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

    def test_registered_monorepo_is_visible_as_local_without_server_access(self):
        checkout = self.tree.root / "development-checkout"
        app = checkout / "app"
        (app / "env").mkdir(parents=True)
        (app / "Makefile").write_text("include $(SETUP_DIRECTORY)/docker/project.mk\n")
        (app / "env/common.env").write_text("PROJECT_NAME=ai-os-dev\n")
        (app / "env/local.env").write_text("DOMAIN=localhost\nPASSWORD=never-export-this\n")
        (checkout / ".git").mkdir()
        (checkout / ".git/config").write_text('[remote "origin"]\n url = git@github.com:BigSant/ai-os.git\n')
        build = self.tree.projects / "ai-os"
        build.mkdir()
        (build / "app").symlink_to(app, target_is_directory=True)
        (build / "development.json").write_text(json.dumps({"version": 1, "checkout": str(checkout)}))
        result = self.status()
        self.assertNotIn("never-export-this", result.stdout)
        project = next(p for p in json.loads(result.stdout)["projects"] if p["name"] == "ai-os")
        self.assertEqual(project["repository"], "git@github.com:BigSant/ai-os.git")
        self.assertEqual(list(project["environments"]), ["local"])
        self.assertTrue(project["environments"]["local"]["development"])
        self.assertEqual(project["environments"]["local"]["compose_project"], "ai-os-dev-local")
        view = load_web_ui()
        self.assertEqual(view.Page.target(project, 'local'), 'šis serveris · kūrimo aplinka')
        self.assertEqual(view.github_actions_link(project), 'https://github.com/BigSant/ai-os/actions')
        self.assertIsNone(view.github_actions_link({'repository': 'https://example.invalid/steal'}))
        self.assertNotIn(('ai-os', 'local'), view.access_columns({'projects': [project]}))
        # An invalid descriptor must not turn an unrelated checkout into a local project.
        (build / "development.json").write_text(json.dumps({"version": 1, "checkout": str(checkout.parent)}))
        project = next(p for p in json.loads(self.status().stdout)["projects"] if p["name"] == "ai-os")
        self.assertNotIn("local", project["environments"])

    def test_a_monorepo_build_root_names_its_own_repository(self):
        # bin/add-project --monorepo: the build root is the clone, app/ its environment, app/public/ no clone of its own.
        app = self.tree.project("zarskis", "stage", "prod")
        self.origin(app.parent, "git@github.com-bigsant.zarskis.lt:BigSant/zarskis.lt.git")
        (app / "public").mkdir()
        for environment in ("stage", "prod"):
            (app.parent / f"target.{environment}.env").write_text(
                f"DEPLOY_CONTEXT=default\nDEPLOY_SSH=\nDEPLOY_PATH=/home/tomas/Projects/runtime/zarskis/{environment}\n")
        result = self.status()
        self.assertEqual(result.returncode, 0, result.stderr)
        project = next(p for p in json.loads(result.stdout)["projects"] if p["name"] == "zarskis")
        self.assertEqual((project["repository"], project["branch"], project["code_repository"], project["layout"]),
                         ("git@github.com-bigsant.zarskis.lt:BigSant/zarskis.lt.git", "main", None, "app"))
        self.assertEqual(project["targets"]["prod"]["DEPLOY_PATH"], "/home/tomas/Projects/runtime/zarskis/prod")
        self.assertEqual(project["targets"]["stage"]["DEPLOY_PATH"], "/home/tomas/Projects/runtime/zarskis/stage")
        self.assertNotIn("all", project["targets"])

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

    def test_developer_keys_are_listed_with_their_grants_fingerprint_and_last_use(self):
        ssh = self.tree.root / ".ssh"
        ssh.mkdir()
        blob = "AAAAC3NzaC1lZDI1NTE5AAAAIGq0dB1i0xJm6yJYGQq3o2Wl3wq8l6q1u2f7m8c9d0e1"
        (ssh / "authorized_keys").write_text(
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOther admin@laptop\n"
            f'from="10.8.0.0/24",command="/usr/local/sbin/setup-server-backup-pull --key ona",restrict ssh-ed25519 {blob} ona\n'
            f'from="10.8.0.0/24,10.9.0.5/32",command="/usr/local/sbin/setup-server-access --key tomas --pull-all none '
            f'--pull asana_tracker:prod=fresh --grant asana_tracker:prod=phpfpm --grant shop-two:stage=full",restrict,pty '
            f'ssh-ed25519 {blob} tomas\n')
        (self.tree.root / "backup-pulls.log").write_text("2026-10-20T08:00:00Z key=tomas from=10.8.0.20 info x prod -> ok\n")
        (self.tree.root / "access.log").write_text("2026-10-21T09:30:00Z key=tomas from=10.8.0.21 'shell asana_tracker prod' -> exit 0\n"
                                                   "garbage line\n")
        result = self.status("--projects")
        self.assertEqual(result.returncode, 0, result.stderr)
        keys = {item["name"]: item for item in json.loads(result.stdout)["keys"]}
        self.assertEqual(sorted(keys), ["ona", "tomas"], "an administrator's own key is not listed")
        self.assertEqual(keys["ona"], {"name": "ona", "sources": ["10.8.0.0/24"], "type": "ssh-ed25519",
                                       "fingerprint": keys["tomas"]["fingerprint"], "command": "backup-pull", "grants": [],
                                       "pulls": [], "pull_all": "db", "legacy": True, "last": None},
                         "written before the backup grants: every project, as before")
        self.assertTrue(keys["tomas"]["fingerprint"].startswith("SHA256:"))
        self.assertEqual(keys["tomas"]["grants"], [{"project": "asana_tracker", "environment": "prod", "level": "phpfpm"},
                                                   {"project": "shop-two", "environment": "stage", "level": "full"}])
        self.assertEqual(keys["tomas"]["last"], {"at": 1792575000, "from": "10.8.0.21", "log": "access"})
        self.assertEqual((keys["tomas"]["pull_all"], keys["tomas"]["legacy"], keys["tomas"]["pulls"]),
                         ("none", False, [{"project": "asana_tracker", "environment": "prod", "level": "fresh"}]))

    def test_the_versions_tell_a_release_tag_from_a_branch_and_count_what_master_has_beyond(self):
        git = lambda *arguments: subprocess.run(["git", *arguments], env={**os.environ, "GIT_AUTHOR_NAME": "t",
                                                "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
                                                check=True, capture_output=True, text=True)
        setup = self.tree.root / "setup"
        git("init", "-q", "-b", "master", str(setup))
        for version in ("2.14.0", "2.14.1"):
            (setup / "VERSION").write_text(version)
            git("-C", str(setup), "add", "-A")
            git("-C", str(setup), "commit", "-q", "-m", version)
        git("-C", str(setup), "tag", "v2.14.1")
        (setup / "VERSION").write_text("2.15.0")
        git("-C", str(setup), "commit", "-q", "-am", "next")
        git("-C", str(setup), "update-ref", "refs/remotes/origin/master", "HEAD")
        git("-C", str(setup), "checkout", "-q", "--detach", "v2.14.1")
        os.environ["SETUP_SERVER_USER"] = getpass.getuser()
        self.addCleanup(os.environ.pop, "SETUP_SERVER_USER")
        data = json.loads(self.status().stdout)["versions"]["setup_git"]
        self.assertEqual((data["tag"], data["behind"], len(data["commit"])), ("v2.14.1", 1, 12))
        git("-C", str(setup), "checkout", "-q", "--detach", "refs/remotes/origin/master")
        data = json.loads(self.status().stdout)["versions"]["setup_git"]
        self.assertEqual((data["tag"], data["behind"]), (None, 0))

    def test_activity_joins_a_shells_start_and_exit_and_a_session_reads_as_typed_lines(self):
        (self.tree.root / "backup-pulls.log").write_text(
            "2026-10-20T08:00:00Z key=ona from=10.8.0.33 get asana_tracker prod worker-prod-1.sql.gz -> sent 5 bytes\n")
        (self.tree.root / "access.log").write_text(
            "2026-10-21T09:30:00Z key=tomas from=10.8.0.21 'shell asana_tracker prod' -> started (phpfpm): docker exec -it x sh "
            "session=20261021T093000Z-77.log\n"
            "2026-10-21T09:31:00Z key=ona from=10.8.0.33 'docker asana_tracker prod ps' -> refused: needs docker\n"
            "2026-10-21T09:32:00Z key=tomas from=10.8.0.21 \"exec asana_tracker prod -- echo 'a -> b'\" -> started (phpfpm): docker exec\n"
            "2026-10-21T09:32:01Z key=tomas from=10.8.0.21 \"exec asana_tracker prod -- echo 'a -> b'\" -> exit 3\n"
            "2026-10-21T09:40:00Z key=tomas from=10.8.0.21 'shell asana_tracker prod' -> exit 0\n")
        result = self.status("--activity", "tomas")
        self.assertEqual(result.returncode, 0, result.stderr)
        events = json.loads(result.stdout)["events"]
        self.assertEqual([(item["request"], item["state"], item.get("exit")) for item in events],
                         [("exec asana_tracker prod -- echo 'a -> b'", "failed", 3), ("shell asana_tracker prod", "ok", 0)])
        self.assertEqual((events[1]["session"], events[1]["level"], events[1]["ended"] - events[1]["at"]),
                         ("20261021T093000Z-77.log", "phpfpm", 600))
        everyone = json.loads(self.status("--activity").stdout)["events"]
        self.assertEqual([item["verb"] for item in everyone], ["exec", "docker", "shell", "get"])
        self.assertEqual(everyone[1]["state"], "refused")
        sessions = self.tree.root / "access-sessions/tomas"
        sessions.mkdir(parents=True)
        (sessions / "20261021T093000Z-77.log").write_bytes(b"lss\x7f\r\x1b[Acat var/log\tx\rpasswd\x15exit\r\x04")
        lines = json.loads(self.status("--session", "tomas", "20261021T093000Z-77.log").stdout)["lines"]
        self.assertEqual(lines, ["ls", "cat var/log\tx", "exit", "^D"])
        self.assertEqual(self.status("--session", "tomas", "../../etc/passwd").returncode, 64)

    def test_a_stage_class_environment_is_one_more_environment_of_the_project(self):
        asana = self.tree.projects / "asana_tracker/app"
        (asana / "env/common.env").write_text('PROJECT_NAME=asana\nSTAGE_ENVIRONMENTS="mdv"\n')
        (asana / "env/mdv.env").write_text("DOMAIN=mdv.asana.example\n")
        (self.tree.projects / "asana_tracker/target.mdv.env").write_text("DEPLOY_CONTEXT=setup-asana-mdv\nDEPLOY_SSH=d@mdv\n")
        result = self.status("--projects")
        self.assertEqual(result.returncode, 0, result.stderr)
        asana_entry = next(item for item in json.loads(result.stdout)["projects"] if item["name"] == "asana_tracker")
        mdv = asana_entry["environments"]["mdv"]
        self.assertEqual((mdv["declared"], mdv["compose_project"], mdv["context"]), (True, "asana-mdv", "setup-asana-mdv"))
        self.assertEqual(asana_entry["targets"]["mdv"]["DEPLOY_SSH"], "d@mdv")

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

    def test_the_project_lists_every_backup_of_an_environment_newest_first(self):
        asana = self.tree.projects / "asana_tracker/app"
        stamp = lambda age: time.strftime("%Y%m%dT%H%M%S", time.gmtime(time.time() - age))
        self.tree.backup(asana, f"full-prod-{stamp(3600)}Z.sql.gz", 3600, 9000)
        self.tree.backup(asana, f"asana-prod-{stamp(7200)}123.sql.gz", 30 * 60, 500)
        self.tree.backup(asana, f"worker-stage-{stamp(60)}Z.sql.gz", 60, 10)  # another environment
        self.tree.backup(asana, ".lock-prod", 10, 0)
        self.tree.backup(asana, "notes.txt", 10, 5)
        data = json.loads(self.status().stdout)
        prod = next(item for item in data["projects"] if item["name"] == "asana_tracker")["environments"]["prod"]
        self.assertEqual([(item["kind"], item["size"]) for item in prod["backup_files"]],
                         [("predeploy", 500), ("full", 9000), ("worker", 4096), ("worker", 3000), ("worker", 2000)])

    def test_files_backups_are_listed_and_read_one_file_at_a_time(self):
        asana = self.tree.projects / "asana_tracker/app"
        (asana / "env/common.env").write_text('PROJECT_NAME=asana\nBACKUP_FILE_PATHS="modules/*/translations/ $(bad) config/local.php"\n')
        older = files_archive(asana, "prod", 26 * 3600, {"modules/a/translations/lt.php": b"<?php $a = 1;\n"})
        newest = files_archive(asana, "prod", 3600, {"modules": None, "modules/a/translations/lt.php": b"<?php $a = 2;\n",
                                                    "modules/b/translations/en.php": b"\0binary", "config/local.php": "../x"})
        data = json.loads(self.status().stdout)
        prod = next(item for item in data["projects"] if item["name"] == "asana_tracker")["environments"]["prod"]
        self.assertEqual(prod["file_paths"], ["modules/*/translations/", "config/local.php"], "only plain code-tree paths leave")
        (asana / "env/prod.env").write_text("PROFILE=prestashop\n")
        data = json.loads(self.status().stdout)
        prod = next(item for item in data["projects"] if item["name"] == "asana_tracker")["environments"]["prod"]
        self.assertEqual(prod["file_paths"], ["modules/*/translations/", "themes/*/modules/*/translations/", "mails/*/lang.php",
                                              "themes/*/mails/*/lang.php", "translations/*-*/", "config/local.php"],
                         "the PrestaShop profile's first, each path once")
        self.assertEqual(prod["files_backups"]["count"], 2)
        self.assertEqual(prod["files_backups"]["newest"]["name"], newest)
        self.assertEqual([item["name"] for item in prod["backup_files"] if item["kind"] == "files"], [newest, older])
        listing = json.loads(self.status("--files", "asana_tracker", "prod", newest).stdout)
        self.assertEqual({item["path"]: (item["type"], item["size"], item["link"]) for item in listing["entries"]},
                         {"modules": ("dir", 0, None), "modules/a/translations/lt.php": ("file", 14, None),
                          "modules/b/translations/en.php": ("file", 7, None), "config/local.php": ("link", 0, "../x")})
        one = json.loads(self.status("--file", "asana_tracker", "prod", newest, "modules/a/translations/lt.php").stdout)
        self.assertEqual(base64.b64decode(one["content"]), b"<?php $a = 2;\n")
        self.assertFalse(one["truncated"])
        for arguments, error in (([newest, "config/local.php"], "not a regular file"), ([newest, "missing.php"], "has no missing.php"),
                                 ([older.replace("files-", "worker-").replace(".tar.gz", ".sql.gz"), "x"], "not a files backup"),
                                 ([older.replace("prod", "stage"), "x"], "not a files backup of prod")):
            with self.subTest(arguments=arguments):
                answer = json.loads(self.status("--file", "asana_tracker", "prod", *arguments).stdout)
                self.assertIn(error, answer["error"])
        planted = asana / ".generated/backups/files-prod-20200101T000000Z.tar.gz"
        planted.symlink_to("/etc/passwd")
        self.assertIn("has no", json.loads(self.status("--files", "asana_tracker", "prod", planted.name).stdout)["error"])
        for arguments in (["--files", "asana_tracker", "prod", "../../x.tar.gz"], ["--files", "Bad", "prod", newest],
                          ["--file", "asana_tracker", "prod", newest, "a\x01b"], ["--files", "asana_tracker", "prod"]):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.status(*arguments).returncode, 64)

    def test_container_logs_only_of_a_container_of_that_project_environment(self):
        result = self.status("--container-logs", "asana_tracker", "prod", "asana-prod-cron", "20")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["lines"], ["2026-10-12T00:00:01Z first line of asana-prod-cron",
                                         "2026-10-12T00:00:02Z second line of asana-prod-cron"])
        self.assertIn("logs --timestamps --tail 20 asana-prod-cron", self.calls("docker"))
        foreign = json.loads(self.status("--container-logs", "asana_tracker", "prod", "other-prod-web").stdout)
        self.assertIn("is not a container of asana-prod", foreign["error"])
        missing = json.loads(self.status("--container-logs", "forsena_docker", "prod", "x").stdout)
        self.assertIn("has no prod environment", missing["error"])
        self.assertEqual([call for call in self.calls("docker") if " logs " in f" {call} "],
                         ["logs --timestamps --tail 20 asana-prod-cron"], "nothing else was asked for its logs")
        unlisted = json.loads(self.status("--container-logs", "asana_tracker", "dev", "c").stdout)
        self.assertIn("has no dev environment", unlisted["error"], "an environment name the project does not list")
        for arguments in (["--container-logs", "asana_tracker", "Dev", "c"], ["--container-logs", "../x", "prod", "c"],
                          ["--container-logs", "asana_tracker", "prod", "-f"], ["--container-logs", "asana_tracker", "prod", "c", "9999"],
                          ["--container-logs", "asana_tracker", "prod"]):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.status(*arguments).returncode, 64)

    def test_the_overview_names_only_the_setup_accounts_public_key(self):
        self.assertIsNone(json.loads(self.status().stdout)["deploy_key"])
        ssh = self.tree.root / ".ssh"
        ssh.mkdir()
        (ssh / "id_ed25519").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nnever-shown\n")
        (ssh / "id_ed25519.pub").write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKey setup@worker1\n")
        result = self.status()
        self.assertEqual(json.loads(result.stdout)["deploy_key"], "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKey setup@worker1")
        self.assertNotIn("never-shown", result.stdout)
        (ssh / "id_ed25519.pub").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n")
        self.assertIsNone(json.loads(self.status().stdout)["deploy_key"], "only a public key line leaves")

    def test_metrics_are_the_installed_commands_exposition(self):
        sbin = self.root / "sbin"
        missing = json.loads(self.status("--metrics").stdout)
        self.assertIn("is not installed", missing["error"])
        sbin.mkdir()
        (sbin / "setup-server-metrics").write_text("#!/bin/sh\n" + METRICS_STUB)
        (sbin / "setup-server-metrics").chmod(0o755)
        result = subprocess.run([str(ROOT / "bin/server-status"), "--metrics"], capture_output=True, text=True, timeout=60,
                                env={**os.environ, "SETUP_SERVER_ROOT": str(self.tree.root), "SETUP_SERVER_SBIN": str(sbin)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('setup_metrics_group_up{group="runner"} 0', json.loads(result.stdout)["text"])

    def test_logs_are_bounded_and_only_of_the_setup_server_units(self):
        (self.tree.root / "backup-pulls.log").write_text("".join(f"line {n}\n" for n in range(100)))
        (self.tree.root / "access.log").write_text("".join(f"access {n}\n" for n in range(10)))
        result = self.status("--logs", "5")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["backup_pulls"], [f"line {n}" for n in range(95, 100)])
        self.assertEqual(data["access"], [f"access {n}" for n in range(5, 10)])
        self.assertEqual(sorted(data["journal"]), sorted(["setup-server-runner.service", "setup-server-github-runner@*.service",
                                                          "setup-server-backup.service", "setup-server-backup-full.service",
                                                          "setup-server-backup-files.service", "setup-server-metrics.service",
                                                          "setup-server-ui.service"]))
        self.assertTrue(all("--lines=5" in call for call in self.calls("journalctl")))
        for arguments in (["--logs", "9999"], ["--logs", "0"], ["--logs", "5;ls"], ["--unit", "ssh"], ["--logs", "5", "x"]):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.status(*arguments).returncode, 64)


# systemctl for bin/server-unit: every unit is loaded but the metrics one; records every call.
UNIT_SYSTEMCTL_STUB = r'''
echo "$*" >> "$STUB_DIR/systemctl.calls"
case "$1" in show) for unit; do :; done
  case "$*" in *--property=LoadState\ *|*--property=LoadState) [ "$unit" = setup-server-metrics.service ] && echo not-found || echo loaded;;
    *) printf 'loaded\nactive\nrunning\n\n\n';; esac;; esac
'''


class ServerUnitTest(Worker):
    def setUp(self):
        super().setUp()
        self.stub("systemctl", UNIT_SYSTEMCTL_STUB)
        self.stub("pgrep", 'echo ${FAKE_MAKE:-}\n')

    def unit(self, *arguments, root=False, **env):
        path = f"{self.fake_root()}:{self.bin}" if root else str(self.bin)
        return subprocess.run([str(ROOT / "bin/server-unit"), *arguments], capture_output=True, text=True, timeout=60,
                              env={**os.environ, "PATH": f"{path}:{os.environ['PATH']}", "SETUP_SERVER_ROOT": str(self.tree.root),
                                   "SETUP_SERVER_USER": getpass.getuser(), **env})

    def changes(self):
        return [call for call in self.calls("systemctl") if not call.startswith("show ")]

    def test_check_shows_the_unit_and_changes_nothing(self):
        result = self.unit("--check", "restart", "runner", FAKE_MAKE="777")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("==> setup-server-runner.service: loaded active running", result.stdout)
        self.assertIn("a pipeline step of", result.stdout)
        self.assertIn("would: systemctl restart setup-server-runner.service", result.stdout)
        self.assertEqual(self.changes(), [])

    def test_only_the_listed_units_and_what_may_be_done_to_each(self):
        for arguments in (["start", "ui"], ["stop", "ui"], ["restart", "backup"], ["stop", "backup-full"], ["stop", "backup-files"],
                          ["restart", "ssh"],
                          ["restart", "setup-server-runner.service"], ["restart", "github-runner@other"],
                          ["restart", "github-runner@../bigsant"], ["kill", "runner"], ["restart", "metrics"]):
            with self.subTest(arguments=arguments):
                result = self.unit("--check", *arguments)
                self.assertEqual(result.returncode, 65, result.stdout + result.stderr)
        self.assertIn("is not installed", self.unit("--check", "restart", "metrics").stderr)
        for arguments in ([], ["restart"], ["restart", "runner", "now"], ["--now", "restart", "runner"]):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.unit(*arguments).returncode, 64)
        self.assertEqual(self.unit("restart", "runner").returncode, 77, "without --check it needs root")
        self.assertEqual(self.changes(), [])

    def test_a_run_does_exactly_that_to_that_unit(self):
        result = self.unit("stop", "github-runner@bigsant", root=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.changes(), ["stop setup-server-github-runner@bigsant.service"])
        self.assertTrue(any("--unit setup-server-github-runner@bigsant.service" in call for call in self.calls("journalctl")))
        self.unit("start", "backup-full", root=True)
        self.unit("restart", "ui", root=True)
        self.assertEqual(self.changes()[1:], ["start setup-server-backup-full.service", "restart setup-server-ui.service"])


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
        self.sbin = self.root / "sbin"
        self.sbin.mkdir()
        (self.sbin / "setup-server-metrics").write_text("#!/bin/sh\n" + METRICS_STUB)
        (self.sbin / "setup-server-metrics").chmod(0o755)
        self.port = self.start()

    def environment(self, **settings):
        environment = {key: value for key, value in os.environ.items() if "proxy" not in key.lower()}
        return {**environment, "PATH": f"{self.bin}:{os.environ['PATH']}", "SETUP_SERVER_ROOT": str(self.tree.root),
                "SETUP_SERVER_LIBRARY": str(self.library), "STATE_DIRECTORY": str(self.state),
                "SERVER_STATUS": str(ROOT / "bin/server-status"), "UI_USER": "admin", "UI_PASSWORD_HASH": PASSWORD_HASH,
                "UI_PORT": "0", "BITBUCKET_API": f"http://127.0.0.1:{self.api.server_port}/2.0",
                "BITBUCKET_ACCESS_TOKEN_ASANA_TRACKER": PROJECT_TOKEN, "BITBUCKET_ACCESS_TOKEN": GENERAL_TOKEN,
                "SETUP_SERVER_SBIN": str(self.sbin), "SETUP_SERVER_CONFIG": str(self.root / "config"), **settings}

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
            if 'class="chip run"' not in page:  # the running job's state chip, not a word other text may contain
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
        # A browser without a session goes to the login page; no WWW-Authenticate, so no browser login dialog.
        status, headers, body = self.request("GET", "/access?q=x", login=None)
        self.assertEqual((status, headers.get("Location")), (303, "/login?next=/access%3Fq%3Dx"))
        self.assertNotIn("WWW-Authenticate", headers)
        for login in (("admin", "wrong-password"), ("root", PASSWORD), ("admin", "")):
            with self.subTest(login=login):
                status, headers, body = self.request("GET", "/", login=login)
                self.assertEqual(status, 401)
                self.assertNotIn("WWW-Authenticate", headers)
                self.assertNotIn("asana_tracker", body)
        self.assertEqual(self.request("GET", "/ui.css", login=None)[0], 200)  # the login page's look; nothing secret
        self.assertEqual(self.request("POST", "/preview", {"action": "add-project"}, login=None)[0], 401)
        self.assertEqual(self.request("GET", "/")[0], 200)
        # A name rebound to 127.0.0.1 is not this UI's: refused before the login is even looked at.
        self.assertEqual(self.request("GET", "/", headers={"Host": "attacker.example"})[0], 400)
        self.assertEqual(self.request("GET", "/", headers={"Host": f"localhost:{self.port}"})[0], 200)
        self.assertNotIn(PASSWORD, self.everything_written())
        self.assertIn("wrong login (401)", self.stderr.read_text())

    # --- Users, groups and sessions --------------------------------------------------------------
    def login(self, user, password, target="/"):
        """The session cookie of a login on the login page (the reply's headers too)."""
        status, headers, body = self.request("POST", "/login", {"user": user, "password": password, "next": target}, login=None)
        self.assertEqual(status, 303, body)
        return re.match(r"ops_session=([^;]+);", headers["Set-Cookie"])[1], headers

    def as_session(self, method, path, cookie, form=None):
        return self.request(method, path, form, login=None, headers={"Cookie": f"ops_session={cookie}"})

    def token_of(self, cookie):
        return re.search(r'name="csrf" value="([0-9a-f]+)"', self.as_session("GET", "/account", cookie)[2])[1]

    def add_user(self, name, password, groups, **fields):
        form = {"csrf": self.csrf(), "new": "1", "name": name, "password": password, "repeat": password,
                **{f"group:{group}": "1" for group in groups}, **fields}
        return self.request("POST", "/users/save", form)

    def test_the_login_page_opens_a_session_and_logout_ends_it(self):
        status, _, page = self.request("GET", "/login?next=/files", login=None)
        self.assertEqual(status, 200)
        self.assertIn('action="/login"', page)
        self.assertIn('name="next" value="/files"', page)
        status, _, page = self.request("POST", "/login", {"user": "admin", "password": "wrong-password"}, login=None)
        self.assertEqual(status, 401)
        self.assertIn("Neteisingas vardas arba slaptažodis", page)
        cookie, headers = self.login("admin", PASSWORD, "/files")
        self.assertEqual(headers["Location"], "/files")
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Lax", headers["Set-Cookie"])
        self.assertNotIn("Secure", headers["Set-Cookie"])  # plain http on 127.0.0.1 (an SSH tunnel)
        self.assertIn("Secure", self.request("POST", "/login", {"user": "admin", "password": PASSWORD}, login=None,
                                             headers={"X-Forwarded-Proto": "https"})[1]["Set-Cookie"])
        status, _, page = self.as_session("GET", "/", cookie)
        self.assertEqual(status, 200)
        self.assertIn('href="/account">admin</a>', page)
        self.assertIn('action="/logout"', page)
        # Only the cookie's sha256 is kept; the password never.
        self.assertNotIn(cookie, self.everything_written())
        self.assertNotIn(PASSWORD, self.everything_written())
        self.assertEqual(self.as_session("GET", "/login", cookie)[1].get("Location"), "/")
        # A login leads only to a page of this UI.
        for target in ("//attacker.example/", "https://attacker.example/", "/login", "/\\attacker.example"):
            with self.subTest(target=target):
                self.assertEqual(self.login("admin", PASSWORD, target)[1]["Location"], "/")
        status, headers, _ = self.as_session("POST", "/logout", cookie, {"csrf": self.token_of(cookie)})
        self.assertEqual((status, headers["Location"]), (303, "/login"))
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.assertEqual(self.as_session("GET", "/", cookie)[0], 303)
        self.assertIn("user=admin client=127.0.0.1 users login", self.log.read_text())
        self.assertIn("users logout", self.log.read_text())

    def test_wrong_logins_from_one_client_must_wait(self):
        for _ in range(10):
            self.assertEqual(self.request("POST", "/login", {"user": "admin", "password": "wrong-password"}, login=None)[0], 401)
        status, headers, _ = self.request("POST", "/login", {"user": "admin", "password": PASSWORD}, login=None)
        self.assertEqual(status, 429)
        self.assertIn("Retry-After", headers)
        self.assertEqual(self.request("GET", "/", login=("admin", "wrong-password"))[0], 429)
        self.assertIn("too many wrong logins (429)", self.stderr.read_text())
        # Behind the LAN proxy each device counts for itself: another one still logs in.
        lan = lambda address, password: self.request("POST", "/login", {"user": "admin", "password": password}, login=None,
                                                     headers={"X-Real-IP": address})[0]
        for _ in range(10):
            self.assertEqual(lan("192.168.10.50", "wrong-password"), 401)
        self.assertEqual(lan("192.168.10.50", PASSWORD), 429)
        self.assertEqual(lan("192.168.10.51", PASSWORD), 303)

    def test_the_first_start_makes_the_groups_and_the_ui_env_login_an_administrator(self):
        status, _, page = self.request("GET", "/users")
        self.assertEqual(status, 200)
        for title in ("Administratoriai", "Operatoriai", "Stebėtojai"):
            self.assertIn(title, page)
        self.assertIn('href="/users/admin"', page)
        stored = json.loads((self.state / "accounts.json").read_text())
        self.assertEqual(stored["users"]["admin"]["groups"], ["administratoriai"])
        self.assertEqual(stored["users"]["admin"]["password"], PASSWORD_HASH)
        self.assertEqual(oct((self.state / "accounts.json").stat().st_mode & 0o777), "0o600")

    def test_groups_decide_what_a_user_sees_and_runs(self):
        form = {"csrf": self.csrf(), "group": "", "title": "Žiūrovai", "level:overview": "view", "level:logs": "view"}
        status, headers, body = self.request("POST", "/groups/save", form)
        self.assertEqual((status, headers.get("Location")), (303, "/groups/ziurovai?saved=1"), body)
        self.assertEqual(self.add_user("jonas", "jono-slaptazodis-1", ["ziurovai"], full_name="Jonas Jonaitis")[0], 303)
        jonas = ("jonas", "jono-slaptazodis-1")
        status, _, page = self.request("GET", "/", login=jonas)
        self.assertEqual(status, 200)
        self.assertIn('href="/logs"', page)
        self.assertNotIn('href="/access"', page)
        self.assertNotIn('action="/preview"', page)  # view only: no action forms
        self.assertIn("Tik peržiūra", page)
        for path in ("/access", "/files", "/update", "/actions", "/users", "/users/admin", "/worker-metrics"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path, login=jonas)[0], 403)
        token = re.search(r'name="csrf" value="([0-9a-f]+)"', page)[1]
        status, _, body = self.request("POST", "/preview", {"csrf": token, "action": "unit", "unit": "runner",
                                                            "operation": "restart"}, login=jonas)
        self.assertEqual(status, 403, body)
        self.assertEqual(self.request("POST", "/users/save", {"csrf": token, "new": "1", "name": "x"}, login=jonas)[0], 403)
        self.assertEqual(self.sudo_calls("server-unit"), [])
        # "valdo" on the overview: its actions run; a group the user gains counts at once.
        form = {"csrf": self.csrf(), "group": "ziurovai", "title": "Žiūrovai", "level:overview": "manage", "level:logs": "view"}
        self.assertEqual(self.request("POST", "/groups/save", form)[0], 303)
        status, _, body = self.request("POST", "/preview", {"csrf": token, "action": "unit", "unit": "runner",
                                                            "operation": "restart"}, login=jonas)
        self.assertEqual(status, 200, body)
        self.assertIn("user=admin client=127.0.0.1 users group ziurovai: created overview=view logs=view", self.log.read_text())
        self.assertNotIn("jono-slaptazodis-1", self.everything_written())

    def test_an_administrator_always_remains(self):
        self.assertEqual(self.request("POST", "/users/save", {"csrf": self.csrf(), "name": "admin", "disabled": "1",
                                                              "group:administratoriai": "1"})[0], 400)
        status, _, body = self.request("POST", "/users/save", {"csrf": self.csrf(), "name": "admin", "group:operatoriai": "1"})
        self.assertEqual(status, 400)
        self.assertIn("bent vienas įjungtas", body)
        self.assertEqual(self.request("POST", "/users/delete", {"csrf": self.csrf(), "name": "admin"})[0], 400)
        self.assertEqual(self.request("POST", "/groups/delete", {"csrf": self.csrf(), "group": "administratoriai"})[0], 400)
        self.assertEqual(self.request("POST", "/groups/save", {"csrf": self.csrf(), "group": "administratoriai",
                                                               "title": "Visi"})[0], 400)
        self.assertEqual(self.request("GET", "/")[0], 200)
        self.assertIn("users user admin: refused (", self.log.read_text())
        # With a second administrator the first may go; a disabled user's sessions end at once.
        self.assertEqual(self.add_user("ona", "onos-slaptazodis-1", ["administratoriai"])[0], 303)
        cookie, _ = self.login("ona", "onos-slaptazodis-1")
        self.assertEqual(self.request("POST", "/users/save", {"csrf": self.csrf(), "name": "ona", "disabled": "1",
                                                              "group:administratoriai": "1"})[0], 303)
        self.assertEqual(self.as_session("GET", "/", cookie)[0], 303)
        self.assertEqual(self.request("GET", "/", login=("ona", "onos-slaptazodis-1"))[0], 401)

    def test_a_user_changes_their_own_password(self):
        self.assertEqual(self.add_user("petras", "petro-slaptazodis-1", ["stebetojai"])[0], 303)
        cookie, _ = self.login("petras", "petro-slaptazodis-1")
        other, _ = self.login("petras", "petro-slaptazodis-1")
        form = {"csrf": self.token_of(cookie), "current": "wrong-password", "password": "naujas-slaptazodis", "repeat": "naujas-slaptazodis"}
        self.assertEqual(self.as_session("POST", "/account/password", cookie, form)[0], 400)
        form["current"] = "petro-slaptazodis-1"
        self.assertEqual(self.as_session("POST", "/account/password", cookie, form)[1].get("Location"), "/account?saved=1")
        self.assertEqual(self.as_session("GET", "/account", cookie)[0], 200)
        self.assertEqual(self.as_session("GET", "/", other)[0], 303)  # the other session ended
        self.assertEqual(self.request("GET", "/", login=("petras", "naujas-slaptazodis"))[0], 200)
        self.assertEqual(self.request("GET", "/", login=("petras", "petro-slaptazodis-1"))[0], 401)

    def test_a_user_links_to_a_prieiga_person(self):
        self.write_keys()
        self.assertEqual(self.add_user("rimas", "rimo-slaptazodis-1", ["stebetojai"], person="no-such-key")[0], 400)
        status, _, page = self.request("GET", "/users/new")
        self.assertEqual(status, 200)
        self.assertEqual(re.findall(r'<option value="([^"]+)"', page), ["tomas"])
        self.assertEqual(self.add_user("rimas", "rimo-slaptazodis-1", ["stebetojai"], person="tomas")[0], 303)
        self.assertIn('href="/access/tomas"', self.request("GET", "/users")[2])

    def test_every_action_belongs_to_a_section(self):
        web_ui = load_web_ui()
        self.assertEqual(set(web_ui.ACTION_SECTIONS), set(web_ui.ACTIONS))
        for sections in web_ui.ACTION_SECTIONS.values():
            self.assertTrue(set(sections) <= set(web_ui.MANAGED_SECTIONS))

    def test_a_new_ui_env_password_gives_the_first_administrator_back(self):
        web_ui = load_web_ui()
        state = self.root / "accounts-state"
        accounts = web_ui.Accounts(state, "admin", PASSWORD_HASH)
        accounts.save_user("ona", True, "", ["administratoriai"], "", False, PASSWORD_HASH)
        accounts.save_user("admin", False, "", [], "", True, None)
        self.assertEqual(accounts.permissions("admin"), {})
        self.assertFalse(web_ui.Accounts(state, "admin", PASSWORD_HASH).recovered)  # the same ui.env: nothing changes
        renewed = web_ui.password_hash("a-new-ui-env-password", iterations=10000)
        accounts = web_ui.Accounts(state, "admin", renewed)
        self.assertTrue(accounts.recovered)
        self.assertTrue(accounts.authenticate("admin", "a-new-ui-env-password"))
        self.assertEqual(accounts.permissions("admin")["users"], "manage")
        broken = self.root / "broken-state"
        broken.mkdir()
        (broken / "accounts.json").write_text("{not json")
        with self.assertRaises(web_ui.Refused):
            web_ui.Accounts(broken, "admin", PASSWORD_HASH)

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
                     "GitHub Actions", "v0.4.0", "nepavyko (exit-code)", "Tue 2026-10-13 00:30:00 UTC",
                     "GitHub runner&#x27;is bigsant", "#41"):
            self.assertIn(text, body)
        self.assertNotIn("never-shown-password", body)
        self.assertIn(f"-n {self.library}/bin/server-status", self.sudo_calls())
        status, _, project = self.request("GET", "/project/asana_tracker")
        self.assertEqual(status, 200)
        for text in ("asana-prod-php-fpm", "healthy", "asana-prod-cron", "asana-prod-secrets", "Deploy į prod", "Backup dabar"):
            self.assertIn(text, project)
        self.assertIn("https://github.com/BigSant/forsena_docker/actions", self.request("GET", "/project/forsena_docker")[2])
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

    def test_setup_server_updates_to_the_newest_master_with_one_button(self):
        self.library.mkdir(exist_ok=True)
        (self.library / "install.env").write_text(f"VERSION=v0.4.0\nCHECKOUT={ROOT}\nADMIN={getpass.getuser()}\n")
        page = self.request("GET", "/update")[2]
        self.assertIn('name="latest" value="yes"', page)
        self.assertNotIn('value="master"', page, "setup updates to release tags only")
        self.preview("update", latest="yes")
        self.assertEqual(self.sudo_calls("update")[-1],
                         f"-n -H -u {getpass.getuser()} {ROOT}/bin/update --check --server-ref origin/master")

    def test_a_status_read_before_an_action_ended_is_read_again_after_it(self):
        module = load_web_ui()
        cache, calls = module.Cache(), []
        compute = lambda: calls.append(1) or len(calls)
        self.assertEqual(cache.get("status", 60, compute), 1)
        self.assertEqual(cache.get("status", 60, compute, since=time.time() - 3600), 1, "an action that ended before")
        self.assertEqual(cache.get("status", 60, compute, since=time.time() + 1), 2, "an action that ended after it")
        self.assertEqual(cache.peek("status"), 2)

    def write_keys(self):
        ssh = self.tree.root / ".ssh"
        ssh.mkdir(exist_ok=True)
        blob = "AAAAC3NzaC1lZDI1NTE5AAAAIGq0dB1i0xJm6yJYGQq3o2Wl3wq8l6q1u2f7m8c9d0e1"
        (ssh / "authorized_keys").write_text(
            f'from="10.8.0.0/24",command="/usr/local/sbin/setup-server-access --key tomas --pull-all none '
            f'--pull asana_tracker:prod=db --grant asana_tracker:prod=full --grant forsena_docker:stage=docker",restrict,pty '
            f'ssh-ed25519 {blob} tomas\n')
        (self.tree.root / "access.log").write_text(
            "2026-10-21T09:30:00Z key=tomas from=10.8.0.21 'shell asana_tracker prod' -> started (full): x session=20261021T093000Z-7.log\n"
            "2026-10-21T09:40:00Z key=tomas from=10.8.0.21 'shell asana_tracker prod' -> exit 0\n")
        sessions = self.tree.root / "access-sessions/tomas"
        sessions.mkdir(parents=True)
        (sessions / "20261021T093000Z-7.log").write_bytes(b"ls\rcat .env\r")

    def test_the_access_pages_list_the_keys_their_reach_activity_and_sessions(self):
        self.write_keys()
        status, _, page = self.request("GET", "/access?fresh=1")
        self.assertEqual(status, 200, page)
        for text in ("tomas", "lv-full", "lv-docker", "/access/new", "shell"):
            self.assertIn(text, page)
        self.assertIn("Nė vienas raktas neatitinka", self.request("GET", "/access?q=nothing")[2])
        status, _, key = self.request("GET", "/access/tomas")
        self.assertEqual(status, 200, key)
        self.assertRegex(key, r'name="grant:asana_tracker:prod" value="full" checked')
        self.assertIn('name="action" value="key-bulk"', key)
        self.assertIn("/access/tomas/session/20261021T093000Z-7.log", key)
        self.assertNotIn("grant:forsena_docker:stage", self.request("GET", "/access/tomas?q=asana")[2], "filtered out")
        session = self.request("GET", "/access/tomas/session/20261021T093000Z-7.log")[2]
        self.assertIn("<li>cat .env</li>", session)
        self.assertEqual(self.request("GET", "/access/nobody")[0], 404)
        self.assertIn('/access/tomas?q=asana_tracker#projects', self.request("GET", "/project/asana_tracker")[2])
        self.assertIn("Programuotojų veikla", self.request("GET", "/logs")[2])

    def test_a_key_page_changes_only_what_it_shows_and_bulk_sets_a_level_everywhere(self):
        self.write_keys()
        self.request("GET", "/access?fresh=1")
        library = f"-n {self.library}/bin/add-backup-key --check"
        # A filtered page sends asana_tracker only: forsena_docker's grant stays.
        status, _, body = self.preview("add-backup-key", key_name="tomas", merge="yes", **{"grant:asana_tracker:prod": "phpfpm"})
        self.assertEqual(status, 200, body)
        kept = "--pull-all none --pull asana_tracker:prod=db"
        self.assertEqual(self.sudo_calls("add-backup-key")[-1],
                         f"{library} --name tomas {kept} --grant asana_tracker:prod=phpfpm --grant forsena_docker:stage=docker")
        self.assertIn("~ asana_tracker prod: full → phpfpm", body)
        status, _, body = self.preview("key-bulk", key_name="tomas", scope="prod", level="phpfpm-readonly", only="all")
        self.assertEqual(status, 200, body)
        self.assertEqual(self.sudo_calls("add-backup-key")[-1],
                         f"{library} --name tomas {kept} --grant asana_tracker:prod=phpfpm-readonly "
                         "--grant forsena_docker:stage=docker --grant shop-two:prod=phpfpm-readonly")
        self.assertIn("+ shop-two prod: readonly", body)
        self.preview("key-bulk", key_name="tomas", scope="all", level="", only="existing")
        self.assertEqual(self.sudo_calls("add-backup-key")[-1], f"{library} --name tomas {kept} --no-grants")
        # Backups: the same bulk and cells, and every project's level.
        status, _, body = self.preview("key-bulk", key_name="tomas", kind="backups", scope="stage", level="fresh", only="all")
        self.assertEqual(status, 200, body)
        self.assertEqual(self.sudo_calls("add-backup-key")[-1],
                         f"{library} --name tomas --pull-all none --pull asana_tracker:prod=db --pull forsena_docker:stage=fresh "
                         "--grant asana_tracker:prod=full --grant forsena_docker:stage=docker")
        self.assertIn("+ kopijos: forsena_docker stage: fresh", body)
        status, _, body = self.preview("add-backup-key", key_name="tomas", merge="yes", pull_all="db",
                                       **{"pull:asana_tracker:prod": "", "pull:shop-two:prod": "fresh"})
        self.assertEqual(self.sudo_calls("add-backup-key")[-1],
                         f"{library} --name tomas --pull-all db --pull shop-two:prod=fresh "
                         "--grant asana_tracker:prod=full --grant forsena_docker:stage=docker")
        self.assertIn("kopijos, visi projektai (ir nauji): none → db", body)
        self.assertIn("− kopijos: asana_tracker prod: atimama db", body)
        for action, fields, message in (("key-bulk", {"key_name": "nobody", "scope": "prod", "level": "full"}, "nėra"),
                                        ("key-bulk", {"key_name": "tomas", "scope": "dev", "level": "full"}, "Pasirink"),
                                        ("add-backup-key", {"key_name": "tomas", "merge": "yes", "grant:x:local": "full"}, "netinkamas"),
                                        ("add-backup-key", {"key_name": "tomas", "grant:asana_tracker:prod": "root"}, "nežinomas"),
                                        ("add-backup-key", {"key_name": "tomas", "pull:asana_tracker:prod": "full"}, "nežinomas"),
                                        ("add-backup-key", {"key_name": "tomas", "pull_all": "all"}, "nežinomas"),
                                        ("key-bulk", {"key_name": "tomas", "kind": "backups", "scope": "prod", "level": "docker"}, "Pasirink"),
                                        ("add-backup-key", {"key_name": "ona", "sources": "10.8.0.0/24"}, "Įklijuok")):
            with self.subTest(action=action, fields=fields):
                status, _, body = self.preview(action, **fields)
                self.assertEqual(status, 400, body)
                self.assertIn(message, body)

    def test_the_tools_tab_grants_project_environments_and_sets_a_password_never_shown(self):
        self.write_keys()
        config = self.root / "config"
        config.mkdir()
        (config / "tool-access.json").write_text(json.dumps({
            "allowed_ips": ["198.51.100.7"], "managed": ["asana_tracker:prod", "forsena_docker:stage"],
            "applied": {"forsena_docker:stage": {"at": 1, "ok": False, "note": "ssh: connection refused"}},
            "users": {"tomas": {"hash": "$6$salt$never-shown-hash", "password_set": "2026-10-26T10:00:00Z",
                                "projects": ["asana_tracker:prod", "forsena_docker:stage"]}}}))
        status, _, page = self.request("GET", "/access?fresh=1")
        self.assertEqual(status, 200, page)
        self.assertIn('id="tools"', page)
        self.assertIn("198.51.100.7", page)
        self.assertIn("forsena_docker stage", page, "a failed sending is named")
        self.assertNotIn("never-shown-hash", page)
        status, _, key = self.request("GET", "/access/tomas?tab=tools")
        self.assertEqual(status, 200, key)
        self.assertRegex(key, r'name="tool:asana_tracker:prod" value="tools" checked')
        self.assertIn("nepritaikyta", key)
        self.assertIn('type="password" name="tool_password"', key)
        self.assertNotIn("never-shown-hash", key)
        library = f"-n {self.library}/bin/tool-access --check"
        # The cells shown change; the others stay.
        status, _, body = self.preview("tool-access", key_name="tomas", **{"tool:asana_tracker:prod": "",
                                                                          "tool:shop-two:prod": "tools"})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.sudo_calls("tool-access")[-1],
                         f"{library} --name tomas --tools forsena_docker:stage --tools shop-two:prod")
        self.assertIn("− asana_tracker prod: įrankiai nebeįleis", body)
        self.assertIn("+ shop-two prod: įrankiai įleis", body)
        # Every prod (or stage, the others, all) at once, as the other tabs; local and test never.
        self.assertIn('name="action" value="tool-bulk"', key)
        status, _, body = self.preview("tool-bulk", key_name="tomas", scope="prod", level="tools")
        self.assertEqual(status, 200, body)
        self.assertEqual(self.sudo_calls("tool-access")[-1],
                         f"{library} --name tomas --tools asana_tracker:prod --tools forsena_docker:stage "
                         "--tools shop-two:prod")
        self.assertIn("+ shop-two prod: įrankiai įleis", body)
        self.preview("tool-bulk", key_name="tomas", scope="stage", level="")
        self.assertEqual(self.sudo_calls("tool-access")[-1], f"{library} --name tomas --tools asana_tracker:prod")
        self.preview("tool-bulk", key_name="tomas", scope="all", level="")
        self.assertEqual(self.sudo_calls("tool-access")[-1], f"{library} --name tomas --no-tools")
        # The password: on stdin only, never in a page, a log or the job's files.
        password = "Typed-By-Admin-2026"
        status, _, body = self.preview("tool-password", key_name="tomas", tool_password=password)
        self.assertEqual(status, 200, body)
        self.assertEqual(self.sudo_calls("tool-access")[-1], f"{library} --name tomas --password -")
        self.assertNotIn(password, body, "the stub echoes its stdin: even then the page hides it")
        self.assertIn("&lt;hidden&gt;", body)
        self.assertIn("perduotas per stdin", body)
        self.assertEqual(self.stubs.joinpath("sudo.stdin").read_text().split("\n----\n")[-2], password)
        status, page = self.confirm(body)
        self.assertEqual(status, 200, page)
        self.assertNotIn(password, page)
        self.assertNotIn(password, self.everything_written())
        self.assertNotIn(password, "".join(self.sudo_calls()))
        status, _, body = self.preview("tool-ips", allowed_ips="198.51.100.7, 203.0.113.0/24")
        self.assertEqual(self.sudo_calls("tool-access")[-1], f"{library} --allowed-ips 198.51.100.7,203.0.113.0/24")
        self.assertEqual(self.sudo_calls("tool-access --check --no")[-1:], [])
        self.preview("tool-ips", allowed_ips="")
        self.assertEqual(self.sudo_calls("tool-access")[-1], f"{library} --no-allowed-ips")
        for action, fields, message in (("tool-password", {"key_name": "tomas", "tool_password": "short"}, "Slaptažodis"),
                                        ("tool-password", {"key_name": "nobody", "tool_password": password}, "nėra"),
                                        ("tool-access", {"key_name": "tomas", "tool:x:local": "tools"}, "netinkamas"),
                                        ("tool-access", {"key_name": "tomas", "tool:asana_tracker:prod": "full"}, "netinkamas"),
                                        ("tool-bulk", {"key_name": "tomas", "scope": "dev", "level": "tools"}, "Pasirink"),
                                        ("tool-bulk", {"key_name": "tomas", "scope": "prod", "level": "full"}, "Pasirink"),
                                        ("tool-bulk", {"key_name": "nobody", "scope": "prod", "level": "tools"}, "nėra"),
                                        ("tool-ips", {"allowed_ips": "0.0.0.0/0"}, "konkretų tinklą")):
            with self.subTest(action=action, fields=fields):
                status, _, body = self.preview(action, **fields)
                self.assertEqual(status, 400, body)
                self.assertIn(message, body)

    def test_a_keys_person_has_a_full_name_on_the_pages_and_a_login_of_their_own(self):
        self.write_keys()
        with (self.tree.root / ".ssh/authorized_keys").open("a") as keys:
            keys.write('from="10.8.0.0/24",command="/usr/local/sbin/setup-server-backup-pull --key ona --pull-all none",restrict '
                       'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGq0dB1i0xJm6yJYGQq3o2Wl3wq8l6q1u2f7m8c9d0e2 ona\n')
        config = self.root / "config"
        config.mkdir()
        (config / "tool-access.json").write_text(json.dumps({"users": {
            "tomas": {"full_name": "Tomas Žarskis", "login": "tomas@example.lt", "projects": ["asana_tracker:prod"]},
            "ona": {"login": "ona.k", "projects": []}}}))
        status, _, page = self.request("GET", "/access?fresh=1")
        self.assertEqual(status, 200, page)
        self.assertIn('<a href="/access/tomas">Tomas Žarskis</a><small>tomas · ', page)
        self.assertIn('<a href="/access/ona">ona</a>', page, "a key without a full name shows its own")
        for query in ("žarsk", "example.lt"):
            with self.subTest(query=query):
                found = self.request("GET", f"/access?q={quote(query)}")[2]
                self.assertIn('href="/access/tomas"', found)
                self.assertNotIn('href="/access/ona"', found)
        status, _, key = self.request("GET", "/access/tomas?tab=tools")
        self.assertIn('<p class="keytitle">Tomas Žarskis</p>', key)
        self.assertIn("vardo <code>tomas@example.lt</code>", key, "the tools ask for the login")
        self.assertIn('name="full_name" value="Tomas Žarskis"', key)
        self.assertIn('name="login" value="tomas@example.lt"', key)
        library = f"-n {self.library}/bin/tool-access --check"
        status, _, body = self.preview("tool-person", key_name="tomas", full_name="  Tomas   Žarskis ", login="Tomas.Z")
        self.assertEqual(status, 200, body)
        self.assertEqual(self.sudo_calls("tool-access")[-1],
                         f"{library} --name tomas --full-name Tomas Žarskis --login Tomas.Z")
        self.assertIn("įrankiai klaus Tomas.Z vietoj tomas@example.lt", body)
        self.assertNotIn("vardas:", body, "the full name stays")
        # Empty: no full name, the key's name again.
        status, _, body = self.preview("tool-person", key_name="ona", full_name="Ona K", login="")
        self.assertEqual(status, 200, body)
        self.assertEqual(self.sudo_calls("tool-access")[-1], f"{library} --name ona --full-name Ona K --login ")
        self.assertIn("įrankiai klaus ona vietoj ona.k", body)
        for fields, message in (({"key_name": "ona", "login": "Tomas@Example.lt"}, "užimtas (raktas tomas)"),
                                ({"key_name": "ona", "login": "TOMAS"}, "užimtas (raktas tomas)"),
                                ({"key_name": "ona", "login": "ona k"}, "Prisijungimo vardas"),
                                ({"key_name": "ona", "login": "ona:k"}, "Prisijungimo vardas"),
                                ({"key_name": "ona", "full_name": "x" * 101}, "iki 100"),
                                ({"key_name": "nobody", "full_name": "X"}, "nėra")):
            with self.subTest(fields=fields):
                status, _, body = self.preview("tool-person", **fields)
                self.assertEqual(status, 400, body)
                self.assertIn(message, body)

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
                 ("add-project", {"env_repo": "git@bitbucket.org:ws/shop.git", "targets": "test=local"}, "Netinkamas taikinys"),
                 ("add-project", {"env_repo": "git@bitbucket.org:ws/shop.git", "targets": "Dev=local"}, "Netinkamas taikinys"),
                 ("remove-project", {"name": "../etc"}, "Projektas"),
                 ("install-backups", {"at": "25:00"}, "Laikas"),
                 ("install-backups", {"full_at": "Sunday 04:30"}, "Pilnos kopijos diena"),
                 ("install-metrics", {"address": "vpn.example"}, "Adresas"),
                 ("install-metrics", {"allow": "0.0.0.0/0"}, "ne visus adresus"),
                 ("add-backup-key", {"public_key": "-----BEGIN OPENSSH PRIVATE KEY-----", "key_name": "tomas",
                                     "sources": "10.8.0.0/24"}, "viešąjį raktą"),
                 ("add-backup-key", {"public_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGq0dB1i0xJm6yJYGQq3o2Wl3wq8l6q1u2f7m8c9d0e1",
                                     "key_name": "tomas", "sources": "10.8.0.0/24", "grants": "shop:prod=root"}, "Prieiga"),
                 ("add-backup-key", {"public_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGq0dB1i0xJm6yJYGQq3o2Wl3wq8l6q1u2f7m8c9d0e1",
                                     "key_name": "tomas", "sources": "10.8.0.0/24", "grants": "shop=full --remove=x"}, "Prieiga"),
                 ("update", {"server_ref": "--upload-pack=x"}, "Server OPS ref"),
                 ("update", {"setup_ref": "v1..v2"}, "setup ref"),
                 ("update", {}, "bent vieną"),
                 ("install-runner", {"provider": "gitlab", "dialog": "x"}, "bitbucket arba github"),
                 ("pipeline", {"project": "asana_tracker", "pipeline": "deploy-production", "ref": "$(id)"}, "REF"),
                 ("pipeline", {"project": "asana_tracker", "pipeline": "custom-anything"}, "Nežinomas pipeline"),
                 ("backup-local", {"environment": "prod"}, "tik kartu su projektu"),
                 ("backup-local", {"project": "asana_tracker", "environment": "test"}, "STAGE_ENVIRONMENTS"),
                 ("backup-local", {"project": "--full"}, "Projektas"),
                 ("unit", {"unit": "ssh", "operation": "restart"}, "Nežinoma paslauga"),
                 ("unit", {"unit": "github-runner@../x", "operation": "stop"}, "Nežinoma paslauga"),
                 ("unit", {"unit": "ui", "operation": "stop"}, "galima tik"),
                 ("unit", {"unit": "backup", "operation": "restart"}, "galima tik"),
                 ("install-ssh", {"sources": "0.0.0.0/0"}, "ne visus adresus"),
                 ("install-ssh", {"sources": "10.8.0.0/24 -oX"}, "nėra IP adresas"),
                 ("install-runner", {"provider": "bitbucket", "tarball": "https://dl.example/r.tar.gz", "sha256": "a" * 64,
                                     "dialog": "x"}, "tik GitHub"),
                 ("install-runner", {"provider": "github", "sha256": "a" * 64, "dialog": "x"}, "tik kartu su archyvu"),
                 ("install-runner", {"provider": "github", "tarball": "ftp://dl.example/r.tar.gz", "dialog": "x"}, "Archyvas"),
                 ("install-runner", {"provider": "github", "tarball": "/tmp/../etc/shadow", "dialog": "x"}, "Archyvas"),
                 ("install-runner", {"provider": "github", "tarball": "/r.tgz", "sha256": "XYZ", "dialog": "x"}, "SHA-256"),
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

    def test_the_files_tab_shows_the_backups_their_files_changes_and_one_file_read_only(self):
        asana = self.tree.projects / "asana_tracker/app"
        (asana / "env/common.env").write_text('PROJECT_NAME=asana\nBACKUP_FILE_PATHS="modules/*/translations/"\n')
        older = files_archive(asana, "prod", 26 * 3600, {"modules/a/translations/lt.php": b"<?php\n$a = 1;\n$keep = 0;\n",
                                                         "modules/c/translations/gone.php": b"old\n"})
        newest = files_archive(asana, "prod", 3600, {"modules/a/translations/lt.php": b"<?php\n$a = 2;\n$keep = 0;\n",
                                                     "modules/b/translations/en.php": b"\0\1binary"})
        status, _, body = self.request("GET", "/files?fresh=1")
        self.assertEqual(status, 200, body)
        self.assertIn('href="/files">Failai', body)
        self.assertIn("<code>modules/*/translations/</code>", body)
        self.assertIn('href="/files/asana_tracker/prod"', body)
        self.assertNotIn("/files/shop-two/", body, "a project without BACKUP_FILE_PATHS is not listed")
        status, _, body = self.request("GET", "/files/asana_tracker/prod")
        self.assertEqual(status, 200, body)
        self.assertLess(body.index(newest), body.index(older), "newest first")
        self.assertIn('name="files" value="yes"', body)
        self.assertIn("setup-server-backup --files asana_tracker prod", body)
        status, _, body = self.request("GET", f"/files/asana_tracker/prod/{newest}")
        self.assertEqual(status, 200, body)
        self.assertRegex(body, r'lt\.php</a></td>.*?pakeista')
        self.assertRegex(body, r'en\.php</a></td>.*?nauja')
        self.assertIn("Dingo nuo ankstesnės kopijos (1)", body)
        self.assertIn(f"/files/asana_tracker/prod/{older}/view?path=modules/c/translations/gone.php", body)
        filtered = self.request("GET", f"/files/asana_tracker/prod/{newest}?q=EN.php")[2]
        self.assertIn("en.php", filtered)
        self.assertNotIn("lt.php</a>", filtered)
        status, _, body = self.request("GET", f"/files/asana_tracker/prod/{newest}/view?path=modules/a/translations/lt.php")
        self.assertEqual(status, 200, body)
        self.assertIn('<li class="del">-$a = 1;</li>', body)
        self.assertIn('<li class="add">+$a = 2;</li>', body)
        self.assertIn("<li>$keep = 0;</li>", body)
        self.assertIn("/download?path=modules/a/translations/lt.php", body)
        binary = self.request("GET", f"/files/asana_tracker/prod/{newest}/view?path=modules/b/translations/en.php")[2]
        self.assertIn("Dvejetainis failas", binary)
        self.assertIn("šio failo nėra", binary)
        status, headers, content = self.request("GET", f"/files/asana_tracker/prod/{newest}/download?path=modules/a/translations/lt.php")
        self.assertEqual((status, content), (200, "<?php\n$a = 2;\n$keep = 0;\n"))
        self.assertEqual(headers["Content-Type"], "application/octet-stream")
        self.assertIn('attachment; filename="lt.php"', headers["Content-Disposition"])
        for path in (f"/files/asana_tracker/prod/{newest}/view?path=", f"/files/asana_tracker/prod/{newest}/view?path=a%01b"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 400)
        self.assertEqual(self.request("GET", "/files/asana_tracker/mdv")[0], 404)
        self.assertEqual(self.request("GET", "/files/asana_tracker/prod/worker-prod-20261009T013000Z.sql.gz")[0], 404)
        self.preview("backup-local", project="asana_tracker", environment="prod", files="yes")
        self.assertEqual(self.sudo_calls("backup-projects")[-1],
                         f"-n {self.library}/bin/backup-projects --check --files asana_tracker prod")
        status, _, body = self.preview("backup-local", project="asana_tracker", full="yes", files="yes")
        self.assertIn("nėra DB kopija", body)
        self.preview("install-backups", files_at="02:40")
        self.assertEqual(self.sudo_calls("install-backups")[-1], f"-n {self.library}/bin/install-backups --check --files-at 02:40")

    def test_backup_forms_pass_the_weekly_time_the_fresh_permission_and_full(self):
        self.preview("install-backups", at="02:15", full_at="Sat 05:00")
        self.assertEqual(self.sudo_calls("install-backups"),
                         [f"-n {self.library}/bin/install-backups --check --at 02:15 --full-at Sat 05:00"])
        key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGq0dB1i0xJm6yJYGQq3o2Wl3wq8l6q1u2f7m8c9d0e1 tomas@laptop"
        self.preview("add-backup-key", public_key=key, key_name="tomas", sources="10.8.0.0/24", pull_all="fresh")
        self.assertEqual(self.sudo_calls("add-backup-key")[-1],
                         f"-n {self.library}/bin/add-backup-key --check - --name tomas --from 10.8.0.0/24 --pull-all fresh "
                         "--no-pulls --no-grants")
        self.preview("add-backup-key", public_key=key, key_name="tomas", sources="10.8.0.0/24",
                     grants="shop:prod=phpfpm-readonly, shop:stage=docker", pulls="shop=db", **{"start:pull:prod": "fresh"})
        self.assertEqual(self.sudo_calls("add-backup-key")[-1],
                         f"-n {self.library}/bin/add-backup-key --check - --name tomas --from 10.8.0.0/24 --pull-all none "
                         "--pull asana_tracker:prod=fresh --pull shop:prod=db --pull shop:stage=db --pull shop-two:prod=fresh "
                         "--grant shop:prod=phpfpm-readonly --grant shop:stage=docker")
        status, _, body = self.preview("pipeline", project="shop-two", pipeline="backup-now", branch="master", full="yes")
        self.assertEqual(status, 200, body)
        self.assertIn("&quot;key&quot;: &quot;FULL&quot;,\n      &quot;value&quot;: &quot;yes&quot;", body)
        # Not asked for: no variable, so a project whose backup-now has no FULL keeps working.
        self.assertIn("&quot;variables&quot;: []", self.preview("pipeline", project="shop-two", pipeline="backup-now",
                                                                branch="master")[2])

    def test_nothing_runs_without_the_action_log(self):
        self.log.mkdir()  # a directory where the log should be: it cannot be appended to
        body = self.preview("install-backups")[2]
        self.assertIn("vykdyti negalima", body)
        self.assertNotIn('name="pending"', body)

    def test_every_command_line_action_has_its_form(self):
        library = f"-n {self.library}/bin"
        self.preview("backup-local", project="asana_tracker", environment="prod", full="yes")
        self.preview("backup-local")
        self.assertEqual(self.sudo_calls("backup-projects"), [f"{library}/backup-projects --check --full asana_tracker prod",
                                                               f"{library}/backup-projects --check"])
        status, _, body = self.preview("unit", unit="runner", operation="restart")
        self.assertEqual(status, 200, body)
        self.assertIn("pipeline&#x27;o žingsnis nutrūktų", body, "what can go wrong is shown above Vykdyti")
        self.preview("unit", unit="github-runner@bigsant", operation="stop")
        self.preview("unit", unit="backup", operation="start")
        self.preview("unit", unit="backup-files", operation="start")
        self.assertEqual(self.sudo_calls("server-unit"), [f"{library}/server-unit --check restart runner",
                                                          f"{library}/server-unit --check stop github-runner@bigsant",
                                                          f"{library}/server-unit --check start backup",
                                                          f"{library}/server-unit --check start backup-files"])
        self.preview("install-ssh", sources="10.8.0.0/24, 10.9.0.0/24")
        self.preview("install-ssh")
        self.assertEqual(self.sudo_calls("install-ssh"), [f"{library}/install-ssh --check --from 10.8.0.0/24,10.9.0.0/24",
                                                          f"{library}/install-ssh --check"])
        sha = "0123456789abcdef" * 4
        self.preview("install-runner", provider="github", runner_name="worker1", tarball="https://dl.example/actions-runner.tar.gz",
                     sha256=sha, dialog="./config.sh --url https://github.com/BigSant --token ABCDEFGH")
        self.assertEqual(self.sudo_calls("install-runner"),
                         [f"{library}/install-runner --check --provider github --name worker1 "
                          f"--tarball https://dl.example/actions-runner.tar.gz --sha256 {sha}"])
        self.preview("move-bitbucket-ssh")
        self.assertEqual(self.sudo_calls("move-bitbucket-ssh"), [f"{library}/move-bitbucket-ssh --check"])
        status, page = self.confirm(self.preview("unit", unit="ui", operation="restart")[2])
        self.assertIn("baigta, kodas 0", page)
        self.assertIn(f"stub ran: {self.library}/bin/server-unit restart ui", page)

    def test_the_overview_and_the_project_offer_the_controls_and_read_only_views(self):
        overview = self.request("GET", "/")[2]
        for unit, operation in (("runner", "restart"), ("github-runner@bigsant", "stop"), ("backup", "start")):
            with self.subTest(unit=unit):
                self.assertRegex(overview, rf'name="unit" value="{re.escape(unit)}"><input type="hidden" name="operation" '
                                           rf'value="{operation}">')
        self.assertNotIn('name="unit" value="ui"', overview, "no controls for a unit that is not installed")
        self.assertNotIn('name="unit" value="metrics"', overview)
        self.assertIn('value="github-runner@bigsant"', self.request("GET", "/actions")[2])
        project = self.request("GET", "/project/asana_tracker")[2]
        self.assertIn("Visos kopijos (3)", project)
        self.assertIn("/project/asana_tracker/logs?env=prod&amp;container=asana-prod-cron", project)
        self.assertIn('name="action" value="backup-local"', project)
        status, _, logs = self.request("GET", "/project/asana_tracker/logs?env=prod&container=asana-prod-cron&lines=500")
        self.assertEqual(status, 200, logs)
        self.assertIn("second line of asana-prod-cron", logs)
        self.assertIn(f"-n {self.library}/bin/server-status --container-logs asana_tracker prod asana-prod-cron 500",
                      self.sudo_calls())
        self.assertIn("is not a container of", self.request("GET", "/project/asana_tracker/logs?env=prod&container=other-prod-web")[2])
        for query in ("env=Dev&container=x", "env=prod&container=-f", "env=prod&container=x&lines=10", "env=prod"):
            with self.subTest(query=query):
                self.assertEqual(self.request("GET", f"/project/asana_tracker/logs?{query}")[0], 400)
        status, _, metrics = self.request("GET", "/worker-metrics")
        self.assertEqual(status, 200, metrics)
        self.assertIn("setup_worker_backup_size_bytes{project=&quot;asana&quot;} 4096", metrics)
        self.assertIn("trūksta", metrics, "the runner group is not up")

    def test_a_remote_on_the_new_bitbucket_ssh_host_keeps_its_pipelines(self):
        self.origin(self.tree.projects / "asana_tracker/app", "git@ssh.bitbucket.org:rsmaizys/asana_tracker.git")
        page = self.request("GET", "/project/asana_tracker?fresh=1")[2]
        self.assertIn("Deploy į prod", page)
        self.assertIn("pipelines/results/41", page)
        web_ui = load_web_ui()
        for url in ("git@ssh.bitbucket.org:ws/shop.git", "ssh://git@ssh.bitbucket.org/ws/shop.git", "git@bitbucket.org:ws/shop.git"):
            self.assertEqual(web_ui.bitbucket_repository(url), ("ws", "shop"), url)

    def instructions(self, **choice):
        status, _, body = self.request("GET", "/instructions?" + urlencode(choice))
        self.assertEqual(status, 200, body)
        return body

    def test_the_instructions_follow_the_chosen_os_panel_environment_and_certificate(self):
        default = self.instructions()
        for text in ("Ubuntu 22.04 / 24.04", "apt-get install -y rsync", "download.docker.com/linux/ubuntu", "Domenas DirectAdmin",
                     "Custom HTTPD Configurations", "ProxyPass / http://127.0.0.1:8081/", "LOCALHOST_PORT_SSL=8082",
                     "PMA_ALLOWED_IPS=&quot;BIURO_IP&quot;", "Deployments → <code>Production</code>", " žingsni",
                     "Kokia OS ir architektūra", ". /etc/os-release &amp;&amp; echo", "uname -m",
                     "CSF ugniasienė (DirectAdmin)", "csf -a WORKER_IP &quot;Server OPS worker&quot;",
                     "systemctl is-active --quiet docker &amp;&amp; systemctl restart docker", "dd if=/dev/zero of=/swapfile",
                     "Prieiga prie repo", "sudo -u setup -H git ls-remote APLINKOS_REPO_URL HEAD",
                     "Workspace runners → Add runner", "sudo bin/install-runner --check",
                     "make deploy-variables ENV=prod names=1", "str_replace(&quot;%%&quot;, &quot;%&quot;",
                     "APLINKOS_REPO_URL [KODO_REPO_URL]", "ServerAlias *.DOMENAS"):
            with self.subTest(text=text):
                self.assertIn(text, default)
        self.assertNotRegex(default, r"\nALLOWED_IPS=", "prod: per-tool lists, not the site-wide one")
        for text in ("certbot", "firewall-cmd", "SELinux", "Tik VPN"):
            with self.subTest(absent=text):
                self.assertNotIn(text, default)
        self.assertIn("Worker'io rakto nepavyko perskaityti", default)
        centos = self.instructions(os="centos", panel="none", env="stage", ssl="letsencrypt", host="shop.example.lt",
                                   user="shop", path="/home/shop/site", domain="shop.example.lt", worker="10.8.0.14")
        for text in ("Ši OS setup&#x27;e netestuota", "dnf install -y rsync", "firewall-cmd --permanent --add-rich-rule",
                     'source address=&quot;10.8.0.14&quot;', "SELinux", "useradd -m -s /bin/bash shop",
                     "dnf install -y epel-release &amp;&amp; dnf install -y certbot",
                     "certbot certonly --standalone -d shop.example.lt",
                     "/etc/letsencrypt/live/shop.example.lt/privkey.pem /home/shop/site/data/stage/ssl/domain.key",
                     "LOCALHOST_PORT=80", "LOCALHOST_PORT_SSL=443", "\nALLOWED_IPS=&quot;BIURO_IP&quot;", "Deployments → <code>Staging</code>",
                     "DATABASE_BUFFER_POOL=512M\nDATABASE_CPUS=…", "shop.example.lt   A  TAIKINIO_IP",
                     "--target stage=ssh://shop@shop.example.lt/home/shop/site",
                     "/actions?target=stage%3Dssh%3A%2F%2Fshop%40shop.example.lt%2Fhome%2Fshop%2Fsite#add-project"):
            with self.subTest(text=text):
                self.assertIn(text, centos)
        self.assertNotIn("Custom HTTPD", centos)
        vpn = self.instructions(panel="none", ssl="none", vpn_address="10.8.0.14", public_address="192.0.2.1")
        for text in ("Tik VPN", "PUBLISH_ADDRESS=10.8.0.14", "HTTP_FORCE_HTTPS=off"):
            self.assertIn(text, vpn)
        self.assertNotIn("CSF ugniasienė", vpn, "CSF is a DirectAdmin server's firewall")
        self.assertNotIn("certbot", vpn)
        self.assertNotIn("192.0.2.1", vpn.split('class="steps"')[1], "a field this choice does not ask for is ignored")
        self.assertNotIn("Atidaryti formą", vpn, "no form link without the server, user and path")

    def test_the_form_asks_only_for_what_the_choice_needs(self):
        body = self.instructions(host="shop.example.lt", domain="shop.example.lt", da_user="shopda", address="ignored", worker="10.8.0.14")
        steps = body.split('class="steps"')[1]
        self.assertIn("sudo -u shopda -H mkdir -p /home/shopda/docker/prod", steps,
                      "with DirectAdmin the directory comes from its user and the environment")
        self.assertIn("--target prod=ssh://shopda@shop.example.lt/home/shopda/docker/prod", steps)
        self.assertIn("*.shop.example.lt   A  TAIKINIO_IP", steps, "DNS: the site and its tools by one wildcard")
        self.assertNotIn("DATABASE_BUFFER_POOL", steps, "prod keeps setup's buffer pool")
        self.assertIn("Atidaryti formą su šiuo taikiniu", steps)
        self.assertIn("PUBLISH_ADDRESS=127.0.0.1", steps)
        css = self.request("GET", "/ui.css")[2]
        self.assertIn('.guide-form:has(select[name="panel"] option[value="none"]:checked) .field-da_user', css)
        self.assertIn('.guide-form:has(select[name="panel"] option[value="directadmin"]:checked) .field-user', css)
        self.assertIn(':has(select[name="ssl"] option[value="letsencrypt"]:checked) .field-port', css)
        self.assertIn('<details class="more">', body, "the optional overrides stay folded while empty")
        self.assertIn('<details class="more" open>', self.instructions(path="/srv/shop"))
        public = self.instructions(panel="none", ssl="letsencrypt", port="8081").split('class="steps"')[1]
        self.assertIn("LOCALHOST_PORT=80", public, "a public site without a panel takes 80/443, whatever the port field says")
        self.assertIn("Netinkama reikšmė lauke „Projekto port&#x27;as“", self.instructions(port="70000"))

    def test_the_example_walks_through_adding_a_project(self):
        self.assertIn('href="/instructions/example"', self.instructions())
        status, _, body = self.request("GET", "/instructions/example")
        self.assertEqual(status, 200, body)
        self.assertIn('class="btn on" href="/instructions"', body, "the example belongs to the instructions section")
        for text in ("bin/update --check --server-ref origin/master --setup-ref vX.Y.Z", "df -h /home",
                     "usermod -aG docker shopuser", "sudo -u shopuser -H mkdir -p /home/shopuser/docker/stage /home/shopuser/docker/prod",
                     "Access keys", "Host key verification failed", "ssh-keyscan -t ed25519 server.example.lt",
                     "--target stage=ssh://shopuser@server.example.lt/home/shopuser/docker/stage",
                     "git@ssh.bitbucket.org:imone/shop_docker.git git@ssh.bitbucket.org:imone/shop.git",
                     "Alias /shop /var/www/html", "ProxyPass /shop/ http://127.0.0.1:3045/shop/",
                     "sudo bin/remove-project --check shop_docker", "Kokia OS ir architektūra",
                     "Swap ir CSF", "*.server.example.lt   A  SERVERIO_IP", "Workspace runners → Add runner",
                     "19 žingsnių"):
            with self.subTest(text=text):
                self.assertIn(text, body)
        self.assertIn(f"{self.tree.root}/.ssh/id_ed25519.pub", body, "the worker's own root, not a fixed path")
        self.assertNotIn("{root}", body)

    def test_the_instructions_refuse_bad_values_and_show_the_workers_key(self):
        ssh = self.tree.root / ".ssh"
        ssh.mkdir()
        (ssh / "id_ed25519.pub").write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKey setup@worker1\n")
        body = self.instructions(os="windows", panel="none", ssl="none", host="x;rm -rf /", user="Root User",
                                 path="/home/shop/app", port="80; id")
        for text in ("Netinkamas pasirinkimas", "lauke „Serveris“", "lauke „SSH vartotojas“", "negali baigtis /app",
                     "lauke „Projekto port&#x27;as“"):
            with self.subTest(text=text):
                self.assertIn(text, body)
        self.assertNotIn("rm -rf", body.split("Netinkama reikšmė", 1)[0] + body.split("</ol>")[0].split('class="steps"')[-1])
        self.assertIn("echo &#x27;ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKey setup@worker1&#x27; &gt;&gt; ~deploy/.ssh/authorized_keys", body)
        self.assertNotIn("rakto nepavyko perskaityti", body)
        prefilled = self.request("GET", "/actions?target=" + urlencode({"": "prod=ssh://shop@shop.example.lt/home/shop/site"})[1:])[2]
        self.assertIn(">prod=ssh://shop@shop.example.lt/home/shop/site</textarea>", prefilled)
        refused = self.request("GET", "/actions?target=" + urlencode({"": "prod=ssh://u@h/p -oProxyCommand=x"})[1:])[2]
        self.assertNotIn("ProxyCommand", refused)

    # --- Bitbucket -------------------------------------------------------------------------------
    def test_a_stage_class_environment_gets_its_deploy_its_grants_and_a_bulk_row(self):
        asana = self.tree.projects / "asana_tracker/app"
        (asana / "env/common.env").write_text('PROJECT_NAME=asana\nSTAGE_ENVIRONMENTS="mdv"\n')
        self.write_keys()
        project = self.request("GET", "/project/asana_tracker?fresh=1")[2]
        self.assertIn('name="pipeline" value="deploy-mdv"', project, "listed: offered before its first deploy")
        self.assertIn("Deploy į mdv", project)
        status, _, body = self.preview("pipeline", project="asana_tracker", pipeline="deploy-mdv", ref="feature/x")
        self.assertEqual(status, 200, body)
        self.assertIn("&quot;pattern&quot;: &quot;deploy-mdv&quot;", body)
        self.assertIn("&quot;key&quot;: &quot;REF&quot;,\n      &quot;value&quot;: &quot;feature/x&quot;", body)
        self.assertEqual(self.preview("pipeline", project="asana_tracker", pipeline="deploy-qa")[0], 400, "not listed")
        key = self.request("GET", "/access/tomas")[2]
        self.assertIn('name="grant:asana_tracker:mdv"', key)
        self.assertIn('name="scope" value="other"', key)
        self.preview("key-bulk", key_name="tomas", scope="other", level="docker", only="all")
        self.assertEqual(self.sudo_calls("add-backup-key")[-1],
                         f"-n {self.library}/bin/add-backup-key --check --name tomas --pull-all none --pull asana_tracker:prod=db "
                         "--grant asana_tracker:mdv=docker --grant asana_tracker:prod=full --grant forsena_docker:stage=docker")
        self.preview("add-backup-key", key_name="tomas", merge="yes", grants="shop-two:mdv=phpfpm")
        self.assertIn("--grant shop-two:mdv=phpfpm", self.sudo_calls("add-backup-key")[-1])
        self.assertEqual(self.preview("backup-local", project="asana_tracker", environment="mdv")[0], 200)

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
        self.assertNotIn("CACHE", json.dumps(sent), "without the box the pipeline's CACHE=auto decides")
        status, _, body = self.preview("pipeline", project="asana_tracker", pipeline="deploy-production",
                                       ref="v1.2.0", cache="yes")
        self.assertEqual(status, 200, body)
        self.confirm(body)
        sent = [request for request in self.api.requests if request[0] == "POST"][-1][3]
        self.assertEqual(sent["variables"][-1], {"key": "CACHE", "value": "yes"}, "the box asks for a clear")
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
