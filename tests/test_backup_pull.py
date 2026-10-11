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

    def pull(self, request, key="tomas"):
        environment = {**os.environ, "SETUP_SERVER_ROOT": str(self.root), "SSH_ORIGINAL_COMMAND": request,
                       "SSH_CONNECTION": "10.8.0.20 50000 10.8.0.14 22"}
        return subprocess.run([sys.executable, str(ROOT / "bin/backup-pull"), "--key", key], env=environment,
                              capture_output=True, timeout=30)

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
        self.run_as_root(str(self.key), "--name", "tomas", "--from", "10.8.0.0/24")
        command = self.root / "sbin/setup-server-backup-pull"
        self.assertEqual(keys.read_text().splitlines()[1],
                         f'from="10.8.0.0/24",command="{command} --key tomas",restrict {KEY.rsplit(" ", 1)[0]} tomas')
        self.assertEqual(keys.stat().st_mode & 0o777, 0o600)
        self.assertIn(f"'SETUP_SERVER_ROOT', '{self.root / 'home'}'", command.read_text(), "sshd starts it without our environment")
        # Again with another network: replaced, not added; the other key stays.
        self.run_as_root(str(self.key), "--name", "tomas", "--from", "10.8.0.0/24,10.9.0.5")
        lines = keys.read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn('from="10.8.0.0/24,10.9.0.5/32"', lines[1])
        self.assertIn("tomas  from 10.8.0.0/24,10.9.0.5/32", self.run_as_root("--list"))
        self.run_as_root("--remove", "tomas")
        self.assertEqual(keys.read_text(), "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOther other@admin\n")

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
