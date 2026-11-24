"""Developer downloads of the newest backup: the forced command (bin/backup-pull) and its keys (bin/add-backup-key)."""

import contextlib
import importlib.machinery
import importlib.util
import io
import os
from pathlib import Path
import pwd
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGq0dB1i0xJm6yJYGQq3o2Wl3wq8l6q1u2f7m8c9d0e1 tomas@laptop"


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class BackupPullTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        app = self.root / "projects/shop/app"
        (app / "env").mkdir(parents=True)
        (app / "Makefile").write_text("include x\n")
        self.backups = app / ".generated/backups"
        self.backups.mkdir(parents=True)
        for index, name in enumerate(["worker-prod-20261010T033000Z.sql.gz", "shop-prod-20261011T120000123456.sql.gz",
                                      "worker-stage-20261012T033000Z.sql.gz", "notes.txt"]):
            path = self.backups / name
            path.write_bytes(name.encode())
            os.utime(path, (1_760_000_000 + index * 100, 1_760_000_000 + index * 100))

    def pull(self, request, key="tomas", fresh=False, command=None, options=()):
        """A key written before the backup grants (no options: every project; fresh with --allow-fresh), or with
        the options bin/add-backup-key writes now."""
        environment = {**os.environ, "SETUP_SERVER_ROOT": str(self.root), "SSH_ORIGINAL_COMMAND": request,
                       "SSH_CONNECTION": "10.8.0.20 50000 10.8.0.14 22",
                       "SETUP_SERVER_BACKUP_COMMAND": str(command or self.root / "missing-backup-command")}
        return subprocess.run([sys.executable, str(ROOT / "bin/backup-pull"), "--key", key,
                               *(["--allow-fresh"] if fresh else []), *options], env=environment, capture_output=True, timeout=30)

    def backup_command(self, body):
        command = self.root / "setup-server-backup"
        command.write_text("#!/bin/sh\n" + body)
        command.chmod(0o755)
        return command

    def test_full_backups_and_info_answer_the_newest_with_its_size(self):
        (self.backups / "full-prod-20261004T043000Z.sql.gz").write_bytes(b"full")
        os.utime(self.backups / "full-prod-20261004T043000Z.sql.gz", (1_760_000_500, 1_760_000_500))
        newest = self.backups / "fresh-full-prod-20261005T120000Z.sql.gz"
        newest.write_bytes(b"fresh full!")
        os.utime(newest, (1_760_000_900, 1_760_000_900))
        self.assertEqual(self.pull("latest shop prod full").stdout.decode().strip(), newest.name)
        self.assertEqual(self.pull("info shop prod full").stdout.decode().strip(), f"{newest.name} 11")
        self.assertEqual(self.pull("info shop prod").stdout.decode().strip(),
                         "shop-prod-20261011T120000123456.sql.gz 38", "registry backups only without full")
        self.assertEqual(self.pull(f"get shop prod {newest.name}").stdout, b"fresh full!")
        self.assertEqual(self.pull("latest shop stage full").returncode, 65)
        self.assertIn(b"no stage full backup of shop yet", self.pull("latest shop stage full").stderr)
        self.assertEqual(self.pull("latest shop prod everything").returncode, 65)
        self.assertIn(b"may only be full or files", self.pull("latest shop prod everything").stderr)

    def test_files_backups_are_a_content_of_their_own_with_the_same_grants(self):
        older, newest = self.backups / "files-prod-20261010T033000Z.tar.gz", self.backups / "files-prod-20261011T033000Z.tar.gz"
        for index, path in enumerate((older, newest)):
            path.write_bytes(b"tar" * (index + 1))
            os.utime(path, (1_760_001_000 + index, 1_760_001_000 + index))
        self.assertEqual(self.pull("info shop prod files").stdout.decode().strip(), f"{newest.name} 6")
        self.assertEqual(self.pull("latest shop prod").stdout.decode().strip(), "shop-prod-20261011T120000123456.sql.gz",
                         "a newer files backup is not a database backup")
        self.assertNotIn(b"files-", self.pull("latest shop prod full").stdout)
        self.assertEqual(self.pull(f"get shop prod {older.name}").stdout, b"tar")
        self.assertIn(b"no stage files backup of shop yet", self.pull("info shop stage files").stderr)
        granted = ["--pull-all", "none", "--pull", "shop:prod=db"]
        self.assertEqual(self.pull("info shop prod files", options=granted).returncode, 0)
        self.assertIn(b"may not download backups of shop stage", self.pull("info shop stage files", options=granted).stderr)
        command = self.backup_command('echo "$*" >> "$(dirname "$0")/calls"\n'
                                      f'printf new > "{self.backups}/files-prod-20261013T150000Z.tar.gz"\n')
        self.assertIn(b"may not ask for a fresh backup", self.pull("fresh shop prod files", command=command, options=granted).stderr)
        made = self.pull("fresh shop prod files", command=command, options=["--pull-all", "fresh"])
        self.assertEqual(made.returncode, 0, made.stderr)
        self.assertEqual(made.stdout.decode().strip(), "files-prod-20261013T150000Z.tar.gz 3")
        self.assertEqual((self.root / "calls").read_text(), "--fresh --files --progress shop prod\n")

    def test_fresh_needs_the_key_permission_and_streams_the_progress(self):
        command = self.backup_command(
            'echo "$*" >> "$(dirname "$0")/calls"\n'
            'echo "progress backup 10 20" >&2\n'
            'echo "==> shop prod: backing up"\n'
            f'printf fresh > "{self.backups}/fresh-prod-20261013T150000Z.sql.gz"\n')
        refused = self.pull("fresh shop prod", command=command)
        self.assertEqual(refused.returncode, 65)
        self.assertIn(b"may not ask for a fresh backup", refused.stderr)
        self.assertFalse((self.root / "calls").exists())
        result = self.pull("fresh shop prod", fresh=True, command=command)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.decode(), "fresh-prod-20261013T150000Z.sql.gz 5\n", "stdout holds the answer only")
        self.assertIn(b"progress backup 10 20", result.stderr)
        self.assertIn(b"==> shop prod: backing up", result.stderr)
        self.assertEqual((self.root / "calls").read_text(), "--fresh --progress shop prod\n")
        self.pull("fresh shop prod full", fresh=True, command=self.backup_command(
            f'echo "$*" >> "{self.root}/calls"\nprintf f > "{self.backups}/fresh-full-prod-20261013T150500Z.sql.gz"\n'))
        self.assertIn("--fresh --full --progress shop prod", (self.root / "calls").read_text())
        log = (self.root / "backup-pulls.log").read_text()
        self.assertIn("fresh shop prod -> refused: this key may not ask", log)
        self.assertIn("-> fresh fresh-prod-20261013T150000Z.sql.gz", log)

    def test_a_failed_fresh_backup_is_refused_and_logged(self):
        failing = self.backup_command('echo "Database backup failed" >&2\nexit 1\n')
        result = self.pull("fresh shop prod", fresh=True, command=failing)
        self.assertEqual(result.returncode, 65)
        self.assertIn(b"the backup failed (exit 1)", result.stderr)
        self.assertIn(b"Database backup failed", result.stderr)
        self.assertEqual(result.stdout, b"")
        missing = self.pull("fresh shop prod", fresh=True)
        self.assertIn(b"need the backup command", missing.stderr)

    def test_latest_names_the_newest_backup_of_the_environment_nightly_or_pre_deploy(self):
        result = self.pull("latest shop prod")
        self.assertEqual((result.returncode, result.stdout.decode().strip()), (0, "shop-prod-20261011T120000123456.sql.gz"))
        self.assertEqual(self.pull("latest shop stage").stdout.decode().strip(), "worker-stage-20261012T033000Z.sql.gz")

    def test_get_sends_only_a_listed_backup_and_every_answer_is_logged(self):
        result = self.pull("get shop prod worker-prod-20261010T033000Z.sql.gz")
        self.assertEqual((result.returncode, result.stdout), (0, b"worker-prod-20261010T033000Z.sql.gz"))
        for request in ("get shop prod ../../../../etc/passwd", "get shop prod notes.txt",
                        "get shop prod worker-stage-20261012T033000Z.sql.gz", "latest ../shop prod", "latest shop local",
                        "bash -i", "", "latest other prod"):
            with self.subTest(request=request):
                result = self.pull(request)
                self.assertEqual(result.returncode, 65)
                self.assertEqual(result.stdout, b"")
        log = (self.root / "backup-pulls.log").read_text()
        self.assertIn("key=tomas from=10.8.0.20 get shop prod worker-prod-20261010T033000Z.sql.gz -> sent 35 bytes", log)
        self.assertIn("refused: no such backup", log)

    def test_an_environment_the_project_lists_is_pulled_like_stage(self):
        app = self.root / "projects/shop/app"
        (app / "env/common.env").write_text('STAGE_ENVIRONMENTS="demo"\n')
        (self.backups / "worker-demo-20261013T033000Z.sql.gz").write_bytes(b"demo")
        result = self.pull("info shop demo")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.decode().strip(), "worker-demo-20261013T033000Z.sql.gz 4")
        refused = self.pull("latest shop qa")
        self.assertEqual(refused.returncode, 65)
        self.assertIn(b"has no environment qa", refused.stderr)

    def test_a_key_pulls_only_the_project_environments_it_is_granted(self):
        command = self.backup_command(f'printf f > "{self.backups}/fresh-prod-20261013T150000Z.sql.gz"\n')
        other = self.root / "projects/other/app"
        (other / "env").mkdir(parents=True)
        (other / "Makefile").write_text("include x\n")
        granted = ["--pull-all", "none", "--pull", "shop:prod=db"]
        self.assertEqual(self.pull("info shop prod", options=granted).returncode, 0)
        for request, options, message in (("info shop stage", granted, b"may not download backups of shop stage"),
                                          ("latest other prod", granted, b"may not download backups of other prod"),
                                          ("get shop stage worker-stage-20261012T033000Z.sql.gz", granted, b"may not"),
                                          ("fresh shop prod", granted, b"may not ask for a fresh backup of shop prod"),
                                          ("info shop prod", ["--pull-all", "none"], b"may not download"),
                                          ("info shop prod", ["--pull-all", "everything"], b"may not download"),
                                          ("info shop prod", ["--grant", "shop:prod=full", "--bogus"], b"may not download")):
            with self.subTest(request=request, options=options):
                refused = self.pull(request, options=options, command=command)
                self.assertEqual(refused.returncode, 65)
                self.assertIn(message, refused.stderr)
        self.assertFalse((self.backups / "fresh-prod-20261013T150000Z.sql.gz").exists(), "no fresh backup was made")
        # Every project's level, raised for one environment; a grant of the gate is not ours to judge.
        options = ["--pull-all", "db", "--pull", "shop:prod=fresh", "--grant", "shop:prod=full"]
        self.assertEqual(self.pull("info shop stage", options=options).returncode, 0)
        self.assertEqual(self.pull("fresh shop prod", options=options, command=command).returncode, 0)
        self.assertEqual(self.pull("fresh shop stage", options=options, command=command).returncode, 65)

    def test_no_backup_yet_is_a_clear_refusal(self):
        for path in self.backups.iterdir():
            path.unlink()
        result = self.pull("latest shop prod")
        self.assertEqual(result.returncode, 65)
        self.assertIn(b"no prod backup of shop yet", result.stderr)


class AddBackupKeyTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.key = self.root / "tomas.pub"
        self.key.write_text(KEY + "\n")
        environment = {"SETUP_SERVER_ROOT": str(self.root / "home"), "SETUP_SERVER_SBIN": str(self.root / "sbin"),
                       "SETUP_SERVER_USER": pwd.getpwuid(os.getuid()).pw_name}
        with patch.dict(os.environ, environment):
            self.module = load("add_backup_key", ROOT / "bin/add-backup-key")
        (self.root / "home/.ssh").mkdir(parents=True)

    def run_as_root(self, *arguments):
        with patch.object(self.module.os, "geteuid", return_value=0), patch.object(self.module.os, "chown"), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.module.main(list(arguments))
        return output.getvalue()

    def test_the_key_logs_in_only_from_the_vpn_and_runs_only_the_pull_command(self):
        keys = self.root / "home/.ssh/authorized_keys"
        keys.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOther other@admin\n")
        output = self.run_as_root(str(self.key), "--name", "tomas", "--from", "10.8.0.0/24")
        command = self.root / "sbin/setup-server-backup-pull"
        self.assertEqual(keys.read_text().splitlines()[1],
                         f'from="10.8.0.0/24",command="{command} --key tomas --pull-all none",restrict {KEY.rsplit(" ", 1)[0]} tomas')
        self.assertIn("tomas downloads no backups", output, "a new key gets nothing it was not given")
        self.assertEqual(keys.stat().st_mode & 0o777, 0o600)
        self.assertIn(f"'SETUP_SERVER_ROOT', '{self.root / 'home'}'", command.read_text(), "sshd starts it without our environment")
        # Again with another network and backups: replaced, not added; the other key stays.
        self.run_as_root(str(self.key), "--name", "tomas", "--from", "10.8.0.0/24,10.9.0.5", "--pull", "shop=db",
                         "--pull", "other:prod=fresh")
        lines = keys.read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn('from="10.8.0.0/24,10.9.0.5/32"', lines[1])
        self.assertIn("--pull-all none --pull other:prod=fresh --pull shop:prod=db --pull shop:stage=db\"", lines[1])
        self.assertIn("tomas  from 10.8.0.0/24,10.9.0.5/32  backups all=none other:prod=fresh shop:prod=db shop:stage=db",
                      self.run_as_root("--list"))
        # --allow-fresh (the option before --pull) is every project's fresh.
        self.run_as_root("--name", "tomas", "--allow-fresh", "--no-pulls")
        self.assertIn(f'command="{command} --key tomas --pull-all fresh"', keys.read_text())
        self.run_as_root("--remove", "tomas")
        self.assertEqual(keys.read_text(), "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOther other@admin\n")

    def test_an_entry_written_before_the_backup_grants_keeps_every_project_until_it_changes(self):
        keys = self.root / "home/.ssh/authorized_keys"
        command = self.root / "sbin/setup-server-backup-pull"
        keys.write_text(f'from="10.8.0.0/24",command="{command} --key ona --allow-fresh",restrict {KEY.rsplit(" ", 1)[0]} ona\n')
        self.assertIn("ona  from 10.8.0.0/24  backups all=fresh", self.run_as_root("--list"))
        self.run_as_root("--name", "ona", "--grant", "shop:prod=phpfpm")
        self.assertIn("--key ona --pull-all fresh --grant shop:prod=phpfpm\"", keys.read_text(), "written out explicitly")

    def test_grants_make_the_access_command_with_a_terminal_and_keep_the_backups(self):
        keys = self.root / "home/.ssh/authorized_keys"
        (self.root / "home/projects/shop").mkdir(parents=True)
        output = self.run_as_root(str(self.key), "--name", "tomas", "--from", "10.8.0.0/24", "--pull-all", "db",
                                  "--grant", "shop=phpfpm-readonly", "--grant", "shop:prod=docker,other:stage=full")
        access = self.root / "sbin/setup-server-access"
        self.assertEqual(keys.read_text().splitlines()[0],
                         f'from="10.8.0.0/24",command="{access} --key tomas --pull-all db --grant other:stage=full '
                         f'--grant shop:prod=docker --grant shop:stage=phpfpm-readonly",restrict,pty {KEY.rsplit(" ", 1)[0]} tomas')
        self.assertIn("no project other on this server", output)
        self.assertIn("full on a target of this server", output)
        installed = access.read_text()
        self.assertIn(f"'SETUP_SERVER_ROOT', '{self.root / 'home'}'", installed)
        self.assertIn(repr(str(self.root / "sbin/setup-server-backup-pull")), installed, "backups still go to the pull command")
        self.assertTrue((self.root / "sbin/setup-server-backup-pull").is_file())
        self.assertIn("tomas  from 10.8.0.0/24  backups all=db  access other:stage=full shop:prod=docker "
                      "shop:stage=phpfpm-readonly", self.run_as_root("--list"))
        # Without the grants again: back to the pull command alone, the same single entry.
        self.run_as_root("--name", "tomas", "--no-grants")
        self.assertEqual(len(keys.read_text().splitlines()), 1)
        self.assertIn("setup-server-backup-pull --key tomas --pull-all db\",restrict ssh-ed25519", keys.read_text())
        self.assertIn("==> " + str(access) + ": unchanged", self.run_as_root("--refresh"))
        self.run_as_root("--remove", "tomas")
        self.assertEqual(keys.read_text(), "")
        for option, grant in (("--grant", "shop:local=phpfpm"), ("--grant", "shop:x=phpfpm"), ("--grant", "shop=root"),
                              ("--grant", "../x=full"), ("--grant", "shop:prod"), ("--pull", "shop=full"),
                              ("--pull", "shop:test=db")):
            with self.subTest(option=option, grant=grant), self.assertRaisesRegex(SystemExit, option):
                self.run_as_root(str(self.key), "--name", "tomas", "--from", "10.8.0.0/24", option, grant)

    def test_an_existing_key_changes_what_it_is_given_by_name_alone(self):
        keys = self.root / "home/.ssh/authorized_keys"
        self.run_as_root(str(self.key), "--name", "tomas", "--from", "10.8.0.0/24,10.9.0.5", "--pull-all", "db")
        self.run_as_root("--name", "tomas", "--grant", "shop:prod=phpfpm", "--pull", "shop:prod=fresh")
        line = keys.read_text().splitlines()[0]
        self.assertTrue(line.startswith('from="10.8.0.0/24,10.9.0.5/32",command="'), line)
        self.assertIn("--key tomas --pull-all db --pull shop:prod=fresh --grant shop:prod=phpfpm\",restrict,pty "
                      + KEY.rsplit(" ", 1)[0] + " tomas", line)
        # Only the networks: the rest stays.
        self.run_as_root("--name", "tomas", "--from", "10.8.0.0/24")
        line = keys.read_text().splitlines()[0]
        self.assertIn('from="10.8.0.0/24",command=', line)
        self.assertIn("--pull-all db --pull shop:prod=fresh --grant shop:prod=phpfpm", line)
        self.run_as_root("--name", "tomas", "--no-grants", "--no-pulls", "--pull-all", "none")
        self.assertIn("setup-server-backup-pull --key tomas --pull-all none\",restrict ssh", keys.read_text())
        with self.assertRaises(SystemExit):
            self.run_as_root("--name", "ona", "--grant", "shop:prod=phpfpm")

    def test_unsafe_requests_are_refused(self):
        private = self.root / "id_ed25519"
        private.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----\n")
        for arguments, message in (([str(self.key), "--name", "tomas", "--from", "0.0.0.0/0"], "not every address"),
                                   ([str(self.key), "--name", "tomas", "--from", "vpn"], "not an IP"),
                                   ([str(private), "--name", "tomas", "--from", "10.8.0.0/24"], "public key"),
                                   ([str(self.key), "--name", "to mas", "--from", "10.8.0.0/24"], "--name")):
            with self.subTest(arguments=arguments), self.assertRaisesRegex(SystemExit, message):
                self.run_as_root(*arguments)
        self.assertFalse((self.root / "home/.ssh/authorized_keys").exists())

    def test_check_changes_nothing_and_needs_no_root(self):
        output = subprocess.run([sys.executable, str(ROOT / "bin/add-backup-key"), str(self.key), "--name", "tomas",
                                 "--from", "10.8.0.0/24", "--check"], capture_output=True, text=True, timeout=30,
                                env={**os.environ, "SETUP_SERVER_ROOT": str(self.root / "home"), "SETUP_SERVER_SBIN": str(self.root / "sbin")})
        self.assertEqual(output.returncode, 0, output.stderr)
        self.assertIn("would write", output.stdout)
        self.assertFalse((self.root / "sbin").exists())
        self.assertFalse((self.root / "home/.ssh/authorized_keys").exists())


if __name__ == "__main__":
    unittest.main()
