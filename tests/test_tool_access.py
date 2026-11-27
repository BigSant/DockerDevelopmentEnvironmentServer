"""Who opens the projects' browser tools (bin/tool-access): the state, the files in the build roots and their sending."""

import hermetic  # noqa: F401  first: no test touches the installed server (tests/hermetic.py)
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import pwd
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
BLOB = "AAAAC3NzaC1lZDI1NTE5AAAAIGq0dB1i0xJm6yJYGQq3o2Wl3wq8l6q1u2f7m8c9d0e1"
PASSWORD = "correct-horse-battery"


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class ToolAccessTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home, self.config = self.root / "srv", self.root / "config"
        for project, environments in (("shop", ("prod", "stage")), ("blog", ("prod",))):
            app = self.home / f"projects/{project}/app"
            (app / "env").mkdir(parents=True)
            (app / "Makefile").write_text("tool-access:\n\t@echo applied $(ENV)\n")
            for environment in environments:
                (app / f"env/{environment}.env").write_text("")
        (self.home / "projects/blog/app/env/common.env").write_text("STAGE_ENVIRONMENTS=demo\n")
        (self.home / ".ssh").mkdir()
        (self.home / ".ssh/authorized_keys").write_text(
            f'from="10.8.0.0/24",command="/usr/local/sbin/setup-server-access --key tomas --pull-all none",restrict,pty ssh-ed25519 {BLOB} tomas\n'
            f'from="10.8.0.0/24",command="/usr/local/sbin/setup-server-backup-pull --key ona --pull-all db",restrict ssh-ed25519 {BLOB}x ona\n')
        setup = self.root / "setup/docker"
        setup.mkdir(parents=True)
        (setup / "project.mk").write_text("tool-access:\n\t@$(RUN_PROJECT) $@\n")
        environment = {"SETUP_SERVER_ROOT": str(self.home), "SETUP_SERVER_CONFIG": str(self.config),
                       "SETUP_SERVER_USER": pwd.getpwuid(os.getuid()).pw_name, "SETUP_DIRECTORY": str(self.root / "setup")}
        patcher = patch.dict(os.environ, environment)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.module = load("tool_access", ROOT / "bin/tool-access")
        self.sent = []

    def run_as_root(self, *arguments, stdin="", sent=True):
        """main() as root; make tool-access recorded (sent=True) instead of run as the setup account."""
        fake = lambda item: (self.sent.append(item), (True, "applied"))[1]
        with patch.object(self.module.os, "geteuid", return_value=0), patch.object(self.module.os, "chown"), \
                patch.object(self.module, "apply", fake) if sent else contextlib.nullcontext(), \
                patch.object(sys, "stdin", io.StringIO(stdin)), contextlib.redirect_stdout(io.StringIO()) as output:
            code = self.module.main(list(arguments))
        return code, output.getvalue()

    def files(self, project, environment):
        directory = self.home / f"projects/{project}/tool-access/{environment}"
        return {path.name: path.read_text() for path in directory.iterdir()} if directory.is_dir() else None

    def state(self):
        return json.loads((self.config / "tool-access.json").read_text())

    def test_a_grant_writes_the_user_file_and_sends_it_at_once(self):
        code, output = self.run_as_root("--name", "tomas", "--password", "-", "--tools", "shop:prod,blog", stdin=PASSWORD + "\n")
        self.assertEqual(code, 0, output)
        self.assertEqual(sorted(self.sent), ["blog:prod", "blog:stage", "shop:prod"], "without :ENV stage and prod")
        users = self.files("shop", "prod")["users.htpasswd"]
        self.assertRegex(users, r"^tomas:\$6\$[^\n]+\n$")
        self.assertEqual(self.files("shop", "prod")["allowed-ips"], "")
        self.assertIsNone(self.files("shop", "stage"), "an environment nobody was granted is left alone")
        self.assertNotIn(PASSWORD, output + (self.config / "tool-access.json").read_text())
        self.assertNotIn("$6$", output, "a hash is not printed either")
        self.assertEqual(stat.S_IMODE((self.config / "tool-access.json").stat().st_mode), 0o600)
        mode = stat.S_IMODE((self.home / "projects/shop/tool-access/prod/users.htpasswd").stat().st_mode)
        self.assertEqual(mode, 0o600, "setup's copy to the target makes it readable there, not here")
        self.assertIn("tomas: + shop prod", output)
        # The hash is the one nginx checks: crypt SHA-512 of the password.
        import subprocess
        salt = users.split("$")[2]
        again = subprocess.run(["openssl", "passwd", "-6", "-salt", salt, "-stdin"], input=PASSWORD + "\n",
                               capture_output=True, text=True).stdout.strip()
        self.assertEqual(users.strip(), f"tomas:{again}")
        shown = json.loads(self.run_as_root("--show")[1])
        self.assertEqual(shown["users"]["tomas"]["projects"], ["blog:prod", "blog:stage", "shop:prod"])
        self.assertTrue(shown["users"]["tomas"]["password"])
        self.assertNotIn("$6$", json.dumps(shown))

    def test_a_revocation_empties_the_file_but_keeps_it(self):
        self.run_as_root("--name", "tomas", "--password", "-", "--tools", "shop:prod", stdin=PASSWORD + "\n")
        self.run_as_root("--name", "ona", "--password", "-", "--tools", "shop:prod", stdin=PASSWORD + "-ona\n")
        self.assertEqual(self.files("shop", "prod")["users.htpasswd"].count("\n"), 2)
        self.sent.clear()
        code, output = self.run_as_root("--name", "tomas", "--no-tools")
        self.assertEqual((code, self.sent), (0, ["shop:prod"]))
        self.assertTrue(self.files("shop", "prod")["users.htpasswd"].startswith("ona:"))
        self.run_as_root("--remove", "ona")
        self.assertEqual(self.files("shop", "prod")["users.htpasswd"], "", "nobody is let in, not the IP rule alone")
        self.assertEqual(self.state()["managed"], ["shop:prod"])

    def test_the_addresses_reach_every_managed_environment(self):
        self.run_as_root("--name", "tomas", "--password", "-", "--tools", "shop", stdin=PASSWORD + "\n")
        self.sent.clear()
        code, output = self.run_as_root("--allowed-ips", "198.51.100.7, 203.0.113.0/24 198.51.100.7")
        self.assertEqual(code, 0, output)
        self.assertEqual(sorted(self.sent), ["shop:prod", "shop:stage"])
        self.assertEqual(self.files("shop", "stage")["allowed-ips"], "198.51.100.7\n203.0.113.0/24\n")
        self.assertIsNone(self.files("blog", "prod"), "a project nobody may open gets no address either")
        self.sent.clear()
        self.assertEqual(self.run_as_root("--allowed-ips", "198.51.100.7,203.0.113.0/24")[0], 0)
        self.assertEqual(self.sent, [], "nothing changed: nothing is sent")
        self.run_as_root("--apply", "shop:stage")
        self.assertEqual(self.sent, ["shop:stage"], "--apply sends again")

    def test_wrong_values_are_refused_before_anything_is_written(self):
        cases = ((("--name", "nobody", "--tools", "shop"), "", "no developer key named nobody"),
                 (("--name", "tomas", "--tools", "other"), "", "no project other"),
                 (("--name", "tomas", "--tools", "shop:demo"), "", "shop has no environment demo"),
                 (("--name", "tomas", "--tools", "blog:demo,shop:../x"), "", "is not PROJECT"),
                 (("--name", "tomas", "--password", "-"), "short\n", "12 to 128 characters"),
                 (("--allowed-ips", "0.0.0.0/0"), "", "every address"),
                 (("--allowed-ips", "all"), "", "not an IP"))
        for arguments, stdin, message in cases:
            with self.subTest(arguments=arguments), self.assertRaisesRegex(self.module.Refused, message):
                self.run_as_root(*arguments, stdin=stdin)
        self.assertFalse((self.config / "tool-access.json").exists())
        self.assertEqual(self.sent, [])

    def test_check_shows_the_change_and_writes_nothing(self):
        with patch.object(sys, "stdin", io.StringIO(PASSWORD + "\n")), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(self.module.main(["--check", "--name", "tomas", "--password", "-", "--tools", "shop:prod"]), 0)
        self.assertIn("tomas: new password", output.getvalue())
        self.assertIn("would send shop prod: 1 users", output.getvalue())
        self.assertFalse((self.config / "tool-access.json").exists())
        self.assertIsNone(self.files("shop", "prod"))

    def test_make_tool_access_runs_in_the_build_root_or_waits(self):
        self.assertEqual(self.module.apply("shop:prod"), (True, "applied"))
        (self.home / "projects/shop/app/env/stage.env").unlink()
        self.assertEqual(self.module.apply("shop:stage")[0], None, "not deployed from here yet")
        (self.home / "projects/shop/app/Makefile").write_text("tool-access:\n\t@echo nginx-proxy did not take it; exit 2\n")
        self.assertEqual(self.module.apply("shop:prod"), (False, "nginx-proxy did not take it"))
        (self.root / "setup/docker/project.mk").write_text("deploy:\n")
        ok, note = self.module.apply("shop:prod")
        self.assertIsNone(ok)
        self.assertIn("update it", note)

    def test_a_failed_sending_is_kept_and_named(self):
        fail = lambda item: (False, "ssh: connect to host shop.example port 22: Connection refused")
        with patch.object(self.module.os, "geteuid", return_value=0), patch.object(self.module.os, "chown"), \
                patch.object(self.module, "apply", fail), patch.object(sys, "stdin", io.StringIO(PASSWORD + "\n")), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(self.module.main(["--name", "tomas", "--password", "-", "--tools", "shop:prod"]), 1)
        self.assertIn("shop prod: FAILED: ssh: connect", output.getvalue())
        self.assertIs(self.state()["applied"]["shop:prod"]["ok"], False)
        self.assertTrue(self.files("shop", "prod"), "the files wait in the build root for --apply or the next deploy")

    def test_removing_a_key_takes_its_tool_access_away(self):
        self.run_as_root("--name", "tomas", "--password", "-", "--tools", "shop:prod", stdin=PASSWORD + "\n")
        keys = load("add_backup_key", ROOT / "bin/add-backup-key")
        calls = []
        with patch.object(keys.os, "geteuid", return_value=0), patch.object(keys.os, "chown"), \
                patch.object(keys.subprocess, "run", lambda command, **options: calls.append((command, options['env']))), \
                contextlib.redirect_stdout(io.StringIO()):
            keys.main(["--remove", "tomas"])
        self.assertEqual(calls[0][0][1:], [str(ROOT / "bin/tool-access"), "--remove", "tomas"])
        self.assertEqual(calls[0][1]["SETUP_SERVER_CONFIG"], str(self.config))
        calls.clear()
        with patch.object(keys.os, "geteuid", return_value=0), patch.object(keys.os, "chown"), \
                patch.object(keys.subprocess, "run", lambda command, **options: calls.append(command)), \
                contextlib.redirect_stdout(io.StringIO()):
            keys.main(["--remove", "ona"])
        self.assertEqual(calls, [], "ona has no tool access: nothing to forget")

    def test_a_login_of_their_own_replaces_the_keys_name_in_the_user_files(self):
        self.run_as_root("--name", "tomas", "--password", "-", "--tools", "shop:prod", stdin=PASSWORD + "\n")
        self.sent.clear()
        code, output = self.run_as_root("--name", "tomas", "--full-name", " Jane  Doe ", "--login", "tomas@example.lt")
        self.assertEqual(code, 0, output)
        self.assertIn("tomas: full name Jane Doe", output)
        self.assertIn("tomas: login tomas -> tomas@example.lt", output)
        self.assertEqual(self.sent, ["shop:prod"], "the new login reaches the target at once")
        self.assertRegex(self.files("shop", "prod")["users.htpasswd"], r"^tomas@example\.lt:\$6\$[^\n]+\n$")
        shown = json.loads(self.run_as_root("--show")[1])["users"]["tomas"]
        self.assertEqual((shown["full_name"], shown["login"]), ("Jane Doe", "tomas@example.lt"))
        status = load("server_status", ROOT / "bin/server-status").tool_access()["users"]["tomas"]
        self.assertEqual((status["full_name"], status["login"]), ("Jane Doe", "tomas@example.lt"))
        # A full name alone changes no file: nothing is sent.
        self.sent.clear()
        self.assertEqual(self.run_as_root("--name", "tomas", "--full-name", "Jane D.")[0], 0)
        self.assertEqual(self.sent, [])
        # Empty: no full name, the key's name again.
        code, output = self.run_as_root("--name", "tomas", "--full-name", "", "--login", "")
        self.assertIn("tomas: full name removed", output)
        self.assertIn("tomas: login tomas@example.lt -> tomas", output)
        self.assertTrue(self.files("shop", "prod")["users.htpasswd"].startswith("tomas:$6$"))
        self.assertNotIn("login", self.state()["users"]["tomas"])
        self.assertNotIn("full_name", self.state()["users"]["tomas"])
        # A key without tool access yet may have a person too.
        self.assertEqual(self.run_as_root("--name", "ona", "--full-name", "Ona Kairė")[0], 0)
        self.assertEqual(self.state()["users"]["ona"], {"full_name": "Ona Kairė", "projects": []})

    def test_a_login_two_keys_would_share_is_refused(self):
        self.run_as_root("--name", "tomas", "--login", "Tomas@Example.lt")
        cases = (("ona", "tomas@example.LT", "taken by the key tomas"),
                 ("ona", "TOMAS", "taken by the key tomas"),
                 ("tomas", "ona", "taken by the key ona"),
                 ("ona", "ona k", "a name or an e-mail address"),
                 ("ona", "ona:k", "a name or an e-mail address"))
        for name, login, message in cases:
            with self.subTest(login=login), self.assertRaisesRegex(self.module.Refused, message):
                self.run_as_root("--name", name, "--login", login)
        with self.assertRaisesRegex(self.module.Refused, "no control characters"):
            self.run_as_root("--name", "ona", "--full-name", "Ona\x1b[31m")
        self.assertEqual(self.run_as_root("--name", "tomas", "--login", "tomas@example.lt")[0], 0, "its own, in another case")
        keys = load("add_backup_key", ROOT / "bin/add-backup-key")
        public = self.root / "new.pub"
        public.write_text(f"ssh-ed25519 {BLOB}z new\n")
        with patch.object(keys.os, "geteuid", return_value=0), self.assertRaisesRegex(SystemExit, "tools login of the key tomas"):
            keys.main([str(public), "--name", "Tomas@example.lt", "--from", "10.8.0.0/24"])

    def test_the_status_shows_the_state_without_hashes(self):
        self.run_as_root("--name", "tomas", "--password", "-", "--tools", "shop:prod", stdin=PASSWORD + "\n")
        self.run_as_root("--allowed-ips", "198.51.100.7")
        status = load("server_status", ROOT / "bin/server-status")
        found = status.tool_access()
        self.assertEqual(found["allowed_ips"], ["198.51.100.7"])
        self.assertEqual(found["users"]["tomas"]["projects"], ["shop:prod"])
        self.assertTrue(found["users"]["tomas"]["password"])
        self.assertTrue(found["applied"]["shop:prod"]["ok"])
        self.assertNotIn("$6$", json.dumps(found))


if __name__ == "__main__":
    unittest.main()
