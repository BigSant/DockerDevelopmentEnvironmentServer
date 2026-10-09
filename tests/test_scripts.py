"""Host scripts in --check mode with stubbed system commands: nothing is installed or changed.

B level (opt-in): SETUP_SERVER_UBUNTU_IMAGE=ubuntu:24.04 runs prepare-host --check inside a real
Ubuntu container and confirms the Java package the runner needs is installable there.
"""

import getpass
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
# The shape of Bitbucket's runner installation dialog (6.0.x), with made-up identifiers and secret.
DIALOG = """# download the runner zip
curl https://product-downloads.atlassian.com/software/bitbucket/pipelines/atlassian-bitbucket-pipelines-runner-6.0.8.tar.gz --output atlassian-bitbucket-pipelines-runner.tar.gz

# extract the file
mkdir atlassian-bitbucket-pipelines-runner && tar -xzvf atlassian-bitbucket-pipelines-runner.tar.gz -C atlassian-bitbucket-pipelines-runner

# launch the runner
cd atlassian-bitbucket-pipelines-runner/bin

./start.sh --accountUuid {11111111-2222-3333-4444-555555555555} --repositoryUuid {66666666-7777-8888-9999-000000000000} --runnerUuid {aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee} --OAuthClientId FakeClientId123 --OAuthClientSecret ATOAfake_secret-VALUE_9 --runtime linux-shell --workingDirectory ../temp
"""
RUN_COMMAND = ("cd atlassian-bitbucket-pipelines-runner/bin; ./start.sh --accountUuid {aaaa-1111} "
               "--runnerUuid {bbbb-2222} --OAuthClientId client123 --OAuthClientSecret s3cr3t-VALUE "
               "--runtime linux-shell --workingDirectory ../temp\n")


class ScriptTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.os_release('ubuntu', '24.04')
        self.meminfo(16)

    def stub(self, name, script):
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + script)
        path.chmod(0o755)

    def fake_root(self):
        """Stubs that let a script's root part run as this user: `sudo` records its call and runs the command
        with `id -u` answering 0 and `install` without -o/-g; the account seems to be in the docker group."""
        root_bin = self.root / "root-bin"
        root_bin.mkdir(exist_ok=True)
        stubs = {"id": f'case "$*" in -u) echo 0;; "-nG "*) echo "$2 docker";; *) exec {shutil.which("id")} "$@";; esac\n',
                 "install": 'for argument; do shift; case "$skip$argument" in -o|-g) skip=1;; 1*) skip=;; *) set -- "$@" "$argument";; esac; done\n'
                            f'exec {shutil.which("install")} "$@"\n'}
        for name, script in stubs.items():
            (root_bin / name).write_text("#!/bin/sh\n" + script)
            (root_bin / name).chmod(0o755)
        self.sudo_log = self.root / "sudo.calls"
        self.stub("sudo", f'echo "$*" >> {self.sudo_log}\n'
                          'while [ $# -gt 0 ]; do case "$1" in -u) shift 2;; -*) shift;; *) break;; esac; done\n'
                          f'PATH={root_bin}:$PATH exec "$@"\n')
        return root_bin

    def os_release(self, distribution, version):
        (self.root / "os-release").write_text(f'ID={distribution}\nVERSION_ID="{version}"\nVERSION_CODENAME=noble\n'
                                               f'PRETTY_NAME="{distribution} {version}"\n')

    def meminfo(self, gigabytes):
        (self.root / "meminfo").write_text(f"MemTotal:       {gigabytes * 1048576} kB\n")

    def run_script(self, name, *arguments, stdin="", **overrides):
        env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}",
               "SETUP_SERVER_OS_RELEASE": str(self.root / "os-release"),
               "SETUP_SERVER_MEMINFO": str(self.root / "meminfo"),
               "SETUP_SERVER_ROOT": str(self.root / "srv"), "SETUP_SERVER_USER": "setup-test-account-missing",
               **overrides}
        return subprocess.run([str(ROOT / "bin" / name), *arguments], input=stdin, env=env,
                              capture_output=True, text=True, timeout=60)


class PrepareHostTest(ScriptTest):
    def test_only_supported_ubuntu_releases(self):
        self.os_release('debian', '12')
        result = self.run_script("prepare-host", "--check")
        self.assertEqual((result.returncode, result.stderr.strip()), (65, "Only Ubuntu is supported (found debian)"))
        self.os_release('ubuntu', '20.04')
        self.assertIn("22.04 or newer", self.run_script("prepare-host", "--check").stderr)

    def test_changes_need_root_but_check_does_not(self):
        if os.getuid() == 0:
            self.skipTest("runs as root")
        self.assertEqual(self.run_script("prepare-host").returncode, 77)

    def test_check_reports_what_it_would_change_and_keeps_an_existing_docker(self):
        self.stub("dpkg-query", 'case "$*" in *openjdk-25*) exit 1;; *) printf "install ok installed";; esac\n')
        self.stub("docker", 'echo 2.20.1\n')
        self.meminfo(4)
        result = self.run_script("prepare-host", "--check")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("would apt-get install openjdk-25-jre-headless", result.stdout)
        self.assertIn("Docker: present (Compose 2.20.1); left as it is", result.stdout)
        self.assertIn("Docker Compose 2.20.1 is older than 2.24.4", result.stdout)
        self.assertIn("RAM 4 GB is below the 8 GB", result.stdout)
        self.assertIn("would create system account setup-test-account-missing", result.stdout)
        self.assertFalse((self.root / "srv").exists(), "--check changes nothing")

    def test_a_ready_host_passes_the_check(self):
        self.stub("dpkg-query", 'printf "install ok installed"\n')
        self.stub("docker", 'echo 2.29.1\n')
        result = self.run_script("prepare-host", "--check")
        self.assertNotIn("Needs attention", result.stdout)
        self.assertIn("Packages: 0 missing", result.stdout)
        self.assertNotIn("install Docker Engine", result.stdout)


class InstallRunnerTest(ScriptTest):
    def test_the_pasted_command_is_kept_and_the_secret_hidden(self):
        result = self.run_script("install-runner", "--check", "--tarball", "runner.tar.gz", stdin=RUN_COMMAND)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--accountUuid {aaaa-1111} --runnerUuid {bbbb-2222} --OAuthClientId client123 "
                      "--OAuthClientSecret <hidden> --runtime linux-shell --workingDirectory "
                      f"{self.root}/srv/runner/temp", result.stdout)
        self.assertNotIn("s3cr3t", result.stdout + result.stderr)
        self.assertNotIn("../temp", result.stdout)

    def test_the_whole_dialog_text_is_accepted_and_the_download_link_taken_from_it(self):
        result = self.run_script("install-runner", "--check", stdin=DIALOG)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("runner archive: https://product-downloads.atlassian.com/software/bitbucket/pipelines/"
                      "atlassian-bitbucket-pipelines-runner-6.0.8.tar.gz", result.stdout)
        self.assertIn("--repositoryUuid {66666666-7777-8888-9999-000000000000}", result.stdout)
        self.assertIn("--OAuthClientSecret <hidden>", result.stdout)
        self.assertNotIn("fake_secret", result.stdout + result.stderr)
        foreign = DIALOG.replace("https://product-downloads.atlassian.com", "https://example.com")
        refused = self.run_script("install-runner", "--check", stdin=foreign)
        self.assertEqual(refused.returncode, 65)
        self.assertIn("No runner download link", refused.stderr)

    def interactive(self, answers):
        """Run install-runner --check in a real terminal and type the answers one line at a time."""
        import pty, select
        env = {**os.environ, "SETUP_SERVER_ROOT": str(self.root / "srv")}
        pid, fd = pty.fork()
        if pid == 0:
            os.execve(str(ROOT / "bin/install-runner"), [str(ROOT / "bin/install-runner"), "--check"], env)
        output, pending = b"", list(answers)
        while True:
            ready, _, _ = select.select([fd], [], [], 5)
            if not ready:
                break
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            output += chunk
            if pending and output.rstrip().endswith(b":"):
                os.write(fd, (pending.pop(0) + "\n").encode())
        os.waitpid(pid, 0)
        return output.decode(errors="replace")

    def test_a_terminal_is_asked_one_value_at_a_time_and_the_secret_is_not_shown(self):
        output = self.interactive([
            "curl https://product-downloads.atlassian.com/software/bitbucket/pipelines/atlassian-bitbucket-pipelines-runner-6.0.8.tar.gz --output x.tar.gz",
            "--accountUuid {11111111-2222-3333-4444-555555555555}", "66666666-7777-8888-9999-000000000000",
            "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "FakeClientId123", "ATOAfake_secret-VALUE_9"])
        self.assertIn("runner archive: https://product-downloads.atlassian.com/software/bitbucket/pipelines/"
                      "atlassian-bitbucket-pipelines-runner-6.0.8.tar.gz", output)
        self.assertIn("--accountUuid {11111111-2222-3333-4444-555555555555} --repositoryUuid "
                      "{66666666-7777-8888-9999-000000000000} --runnerUuid {aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee} "
                      "--OAuthClientId FakeClientId123 --OAuthClientSecret <hidden>", output)
        self.assertNotIn("fake_secret", output, "the secret is neither echoed nor printed")

    def test_a_missing_value_in_the_terminal_is_an_error(self):
        output = self.interactive(["https://product-downloads.atlassian.com/software/bitbucket/pipelines/"
                                   "atlassian-bitbucket-pipelines-runner-6.0.8.tar.gz", ""])
        self.assertIn("No value given for: accountUuid", output)
        self.assertNotIn("runner arguments", output)

    def test_unexpected_text_and_other_runtimes_are_refused(self):
        for text, message in ((RUN_COMMAND.replace("client123", "client123;rm"), "Unexpected text"),
                              (RUN_COMMAND.replace("linux-shell", "linux-docker"), "Only the Linux Shell runner"),
                              (RUN_COMMAND.replace("--runnerUuid {bbbb-2222} ", ""), "lacks --runnerUuid"),
                              ("echo hello\n", "no start.sh command")):
            with self.subTest(message=message):
                result = self.run_script("install-runner", "--check", "--tarball", "x", stdin=text)
                self.assertEqual(result.returncode, 65)
                self.assertIn(message, result.stderr)


class AddProjectTest(ScriptTest):
    def test_check_plans_the_build_root(self):
        result = self.run_script("add-project", "--check", "asana", "git@bitbucket.org:ws/asana_tracker.git")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"would clone git@bitbucket.org:ws/asana_tracker.git into {self.root}/srv/projects/asana/app",
                      result.stdout)
        self.assertIn(f"BUILD_ROOT={self.root}/srv/projects/asana", result.stdout)
        self.assertEqual(self.run_script("add-project", "--check", "Bad Name", "x").returncode, 65)

    def test_the_build_root_defaults_to_the_repository_slug(self):
        result = self.run_script("add-project", "--check", "git@bitbucket.org:rsmaizys/asana_tracker.git")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"into {self.root}/srv/projects/asana_tracker/app", result.stdout)
        self.assertIn("find it themselves", result.stdout)
        two = self.run_script("add-project", "--check", "git@bitbucket.org:rsmaizys/Forsena_Docker.git",
                              "git@bitbucket.org:rsmaizys/forsena.git")
        self.assertIn(f"into {self.root}/srv/projects/forsena_docker/app/public", two.stdout)


class RemoveProjectTest(ScriptTest):
    def setUp(self):
        super().setUp()
        self.build_root = self.root / "srv/projects/demo"
        (self.build_root / "app/.git").mkdir(parents=True)
        (self.build_root / "app/index.php").write_text("<?php\n")
        # No running containers unless a test says otherwise.
        self.stub("docker", 'case "$1" in ps) printf "%s" "${FAKE_CONTAINERS:-}";; '
                            'inspect) printf "%s\\n" "${FAKE_MOUNTS:-}";; esac\n')

    def test_check_shows_and_keeps_and_a_plain_build_root_is_removed(self):
        result = self.run_script("remove-project", "--check", "demo")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("database backups: 0", result.stdout)
        self.assertIn("would remove", result.stdout)
        self.assertTrue(self.build_root.exists(), "--check removes nothing")
        if os.getuid() != 0:
            self.assertEqual(self.run_script("remove-project", "demo").returncode, 77, "removing needs sudo")

    def test_backups_and_deploy_history_need_with_backups(self):
        (self.build_root / "app/.generated/backups").mkdir(parents=True)
        (self.build_root / "app/.generated/backups/demo-prod-20261009.sql.gz").write_text("x")
        (self.build_root / "app/.generated/release/prod").mkdir(parents=True)
        (self.build_root / "app/.generated/release/prod/deployments.json").write_text("[]")
        refused = self.run_script("remove-project", "--check", "demo")
        self.assertEqual(refused.returncode, 65)
        self.assertIn("database backups: 1; environments with deploy history: 1", refused.stdout)
        self.assertIn("add --with-backups", refused.stderr)
        self.assertEqual(self.run_script("remove-project", "--check", "--with-backups", "demo").returncode, 0)

    def test_a_build_root_used_by_a_running_container_is_kept(self):
        env_root = self.build_root
        os.environ["FAKE_CONTAINERS"] = "abc123"
        os.environ["FAKE_MOUNTS"] = f"/demo-prod-php-fpm {env_root}/app/public /var/run/docker.sock"
        self.addCleanup(os.environ.pop, "FAKE_CONTAINERS")
        self.addCleanup(os.environ.pop, "FAKE_MOUNTS")
        result = self.run_script("remove-project", "--check", "--with-backups", "demo")
        self.assertEqual(result.returncode, 65)
        self.assertIn("Containers still use", result.stderr)
        self.assertIn("demo-prod-php-fpm", result.stderr)

    def test_unknown_and_invalid_names_are_refused(self):
        self.assertIn("No build root", self.run_script("remove-project", "--check", "missing").stderr)
        self.assertEqual(self.run_script("remove-project", "--check", "../srv").returncode, 65)


PASSING_TESTS = "import unittest\n\nclass Health(unittest.TestCase):\n    def test_ok(self):\n        pass\n"
FAILING_TESTS = PASSING_TESTS.replace("pass", "self.fail('broken release')")
# A broken release whose tests also leave the checkout unwritable, so the automatic return fails too.
LOCKING_TESTS = ("import os, unittest\nfrom pathlib import Path\n\nclass Health(unittest.TestCase):\n"
                 "    def test_locks(self):\n        os.chmod(Path(__file__).resolve().parents[1] / '.git', 0o500)\n"
                 "        self.fail('broken release')\n")
GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
           "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}


class UpdateTest(ScriptTest):
    """bin/update against temporary repositories: bare remotes, the admin's setup-server clone and the
    shared setup checkout, with systemctl and pgrep stubbed (the stubs record every call)."""

    def setUp(self):
        super().setUp()
        self.calls = self.root / "systemctl.calls"
        # The runner is active as PID 4242; FAKE_RESTARTED makes every later look see a new PID and
        # FAKE_STOPPED makes it inactive after the first look.
        self.stub("systemctl", f'echo "$*" >> {self.calls}\n'
                               'case "$1" in\n'
                               f'  is-active) n=$(grep -c "^is-active" {self.calls}); [ "${{FAKE_RUNNER:-active}}" = active ] '
                               '&& { [ -z "${FAKE_STOPPED:-}" ] || [ "$n" -le 1 ]; };;\n'
                               f'  show) n=$(grep -c "^show" {self.calls}); '
                               '[ -n "${FAKE_RESTARTED:-}" ] && [ "$n" -gt 1 ] && echo 5151 || echo 4242;;\n'
                               'esac\n')
        self.stub("pgrep", '[ -n "${FAKE_MAKE:-}" ] && echo 777\n')
        self.server_remote = self.remote("server", [("v0.1.0", {"tests/test_health.py": PASSING_TESTS, "bin/x": "1"})])
        self.setup_remote = self.remote("setup", [("v1.0.0", {"Makefile": "VERSION=1.0.0\n"})])
        self.server = self.root / "home/setup-server"
        self.setup = self.root / "srv/setup"
        self.git("clone", "-q", str(self.server_remote), str(self.server))
        self.git("-C", str(self.server), "checkout", "-q", "v0.1.0")
        self.git("clone", "-q", str(self.setup_remote), str(self.setup))
        self.git("-C", str(self.setup), "checkout", "-q", "v1.0.0")
        # Newer releases appear on the remotes after the clones, so the update must fetch them.
        self.commit("server", "v0.2.0", {"tests/test_health.py": PASSING_TESTS, "bin/x": "2"}, "Second release")
        self.commit("server", "v0.3.0", {"tests/test_health.py": FAILING_TESTS, "bin/x": "3"}, "Broken release")
        self.commit("setup", "v1.1.0", {"Makefile": "VERSION=1.1.0\n"}, "Setup 1.1.0")
        self.commit("setup", None, {"Makefile": "VERSION=stage\n"}, "Stage fix", branch="stage-fix")

    def git(self, *arguments):
        return subprocess.run(["git", *arguments], env=GIT_ENV, check=True, capture_output=True,
                              text=True).stdout.strip()

    def remote(self, name, commits):
        work = self.root / f"{name}-work"
        self.git("init", "-q", "-b", "master", str(work))
        self.git("init", "-q", "--bare", str(self.root / f"{name}.git"))
        self.git("-C", str(work), "remote", "add", "origin", str(self.root / f"{name}.git"))
        for tag, files in commits:
            self.commit(name, tag, files, f"{name} {tag}")
        return self.root / f"{name}.git"

    def commit(self, name, tag, files, message, branch="master"):
        work = str(self.root / f"{name}-work")
        if branch != "master":
            self.git("-C", work, "checkout", "-q", "-b", branch)
        for path, text in files.items():
            (self.root / f"{name}-work" / path).parent.mkdir(parents=True, exist_ok=True)
            (self.root / f"{name}-work" / path).write_text(text)
            (self.root / f"{name}-work" / path).chmod(0o755 if path.startswith("bin/") else 0o644)
        self.git("-C", work, "add", "-A")
        self.git("-C", work, "commit", "-q", "-m", message)
        if tag:
            self.git("-C", work, "tag", tag)
        self.git("-C", work, "push", "-q", "--tags", "origin", branch)
        if branch != "master":
            self.git("-C", work, "checkout", "-q", "master")
        return self.git("-C", work, "rev-parse", "HEAD")

    def head(self, checkout):
        return self.git("-C", str(checkout), "describe", "--tags", "--always")

    def update(self, *arguments, **env):
        return self.run_script("update", *arguments, SETUP_SERVER_CHECKOUT=str(self.server),
                               SETUP_SERVER_USER=getpass.getuser(), SETUP_SERVER_SBIN=str(self.root / "sbin"),
                               SETUP_SERVER_SYSTEMD=str(self.root / "systemd"),
                               SETUP_SERVER_CONFIG=str(self.root / "etc"),
                               SETUP_SERVER_SUDOERS=str(self.root / "sudoers.d"), **env)

    def runner_untouched(self):
        calls = self.calls.read_text() if self.calls.exists() else ""
        for verb in ("restart", "try-restart", "stop", "start", "reload", "kill", "daemon-reload", "enable", "disable"):
            self.assertNotRegex(calls, rf"(?m)^{verb}( \S+)* setup-server-runner\b", f"systemctl {verb} was called:\n{calls}")
            self.assertNotRegex(calls, rf"(?m)^{verb}$", f"systemctl {verb} was called:\n{calls}")

    def release_with_installers(self, tag, tests, marker):
        """A setup-server release that carries the real installers and commands, marked so its copies are known."""
        files = {"tests/test_health.py": tests}
        for name in ("install-backups", "install-metrics"):
            files[f"bin/{name}"] = (ROOT / "bin" / name).read_text()
        for name in ("backup-projects", "worker-metrics"):
            files[f"bin/{name}"] = (ROOT / "bin" / name).read_text() + f"# {marker}\n"
        self.commit("server", tag, files, f"Release {tag}")

    def test_an_update_refreshes_the_installed_copies_and_a_return_restores_them(self):
        self.fake_root()
        sbin = self.root / "sbin"
        sbin.mkdir()
        (sbin / "setup-server-backup").write_text("previous backup copy\n")
        (sbin / "setup-server-metrics").write_text("previous metrics copy\n")
        self.release_with_installers("v0.5.0", PASSING_TESTS, "release 0.5.0")
        self.release_with_installers("v0.6.0", FAILING_TESTS, "release 0.6.0")

        check = self.update("--check", "--server-ref", "v0.5.0")
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
        self.assertIn(f"refresh the installed copies: {sbin}/setup-server-backup {sbin}/setup-server-metrics", check.stdout)
        self.assertEqual((sbin / "setup-server-backup").read_text(), "previous backup copy\n")

        broken = self.update("--server-ref", "v0.6.0")
        self.assertEqual(broken.returncode, 1, broken.stdout + broken.stderr)
        self.assertIn(f"wrote {sbin}/setup-server-backup", broken.stdout, "copied before the health check")
        self.assertIn("Restored the previous installed copies", broken.stdout)
        self.assertEqual((sbin / "setup-server-backup").read_text(), "previous backup copy\n")
        self.assertEqual((sbin / "setup-server-metrics").read_text(), "previous metrics copy\n")
        self.assertEqual(self.head(self.server), "v0.1.0")

        result = self.update("--server-ref", "v0.5.0")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        backup = (sbin / "setup-server-backup").read_text()
        self.assertIn("# release 0.5.0", backup)
        self.assertIn(f"os.environ.get('SETUP_SERVER_ROOT', '{self.root}/srv')", backup, "the host's paths are kept")
        self.assertIn("# release 0.5.0", (sbin / "setup-server-metrics").read_text())
        self.assertIn("--refresh", self.sudo_log.read_text())
        self.assertRegex(self.calls.read_text(), r"(?m)^try-restart setup-server-metrics.service$",
                         "the metrics service runs the new copy")
        self.runner_untouched()
        # Only what is installed is refreshed.
        (sbin / "setup-server-metrics").unlink()
        self.assertEqual(self.update("--server-ref", "v0.2.0").returncode, 0)
        self.assertFalse((sbin / "setup-server-metrics").exists())

    def test_check_shows_what_would_change_and_changes_nothing(self):
        result = self.update("--check", "--server-ref", "v0.2.0", "--setup-ref", "v1.1.0")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("v0.1.0 -> v0.2.0", result.stdout)
        self.assertIn("1 commit forward", result.stdout)
        self.assertIn("Second release", result.stdout)
        self.assertIn("v1.0.0 -> v1.1.0", result.stdout)
        self.assertIn("Setup 1.1.0", result.stdout)
        self.assertIn("runner setup-server-runner active (PID 4242)", result.stdout)
        self.assertIn("nothing was changed", result.stdout)
        self.assertEqual((self.head(self.server), self.head(self.setup)), ("v0.1.0", "v1.0.0"))
        self.assertEqual(self.git("-C", str(self.server), "status", "--porcelain"), "")
        self.assertNotIn("Ran 1 test", result.stdout + result.stderr, "--check runs no health check")
        self.runner_untouched()

    def test_an_update_moves_both_checkouts_and_never_restarts_the_runner(self):
        result = self.update("--server-ref", "v0.2.0", "--setup-ref", "v1.1.0")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.head(self.server), self.head(self.setup)), ("v0.2.0", "v1.1.0"))
        self.assertIn("setup-server tests: ok", result.stdout)
        self.assertIn("setup at v1.1.0, readable by", result.stdout)
        self.assertIn("runner setup-server-runner: still active, same PID 4242", result.stdout)
        self.assertIn("Updated", result.stdout)
        self.runner_untouched()
        # Only one side can be given; the other checkout stays where it is.
        only_setup = self.update("--setup-ref", "v1.0.0")
        self.assertEqual(only_setup.returncode, 0, only_setup.stdout + only_setup.stderr)
        self.assertIn("1 commit back", only_setup.stdout)
        self.assertEqual((self.head(self.server), self.head(self.setup)), ("v0.2.0", "v1.0.0"))

    def test_a_failed_health_check_returns_both_checkouts_to_the_previous_commits(self):
        result = self.update("--server-ref", "v0.3.0", "--setup-ref", "v1.1.0")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("setup-server tests: FAILED", result.stdout)
        self.assertIn("broken release", result.stdout)
        self.assertIn("Returned both checkouts to the previous commits", result.stdout)
        self.assertEqual((self.head(self.server), self.head(self.setup)), ("v0.1.0", "v1.0.0"))
        self.runner_untouched()

    def test_a_restarted_or_stopped_runner_fails_the_health_check(self):
        for env, message in (({"FAKE_RESTARTED": "1"}, "PID changed from 4242 to 5151"),
                             ({"FAKE_STOPPED": "1"}, "not active any more (it was PID 4242)")):
            with self.subTest(message=message):
                self.calls.unlink(missing_ok=True)  # the stubs count their own calls
                result = self.update("--server-ref", "v0.2.0", **env)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(message, result.stdout)
                self.assertEqual(self.head(self.server), "v0.1.0")
        # A runner that was not active before is reported, not a reason to refuse an update.
        idle = self.update("--server-ref", "v0.2.0", FAKE_RUNNER="inactive")
        self.assertEqual(idle.returncode, 0, idle.stdout + idle.stderr)
        self.assertIn("runner setup-server-runner is not active; not checked", idle.stdout)

    def test_when_the_return_fails_too_the_manual_recovery_is_printed(self):
        if os.getuid() == 0:
            self.skipTest("root ignores the read-only .git the broken release leaves behind")
        self.commit("server", "v0.4.0", {"tests/test_health.py": LOCKING_TESTS}, "Locking release")
        self.addCleanup(lambda: (self.server / ".git").chmod(0o755))
        previous = self.git("-C", str(self.server), "rev-parse", "HEAD")
        result = self.update("--server-ref", "v0.4.0", "--setup-ref", "v1.1.0")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("The automatic return failed", result.stdout)
        self.assertIn(f"git -C {self.server} checkout --force {previous}", result.stdout)
        self.assertEqual(self.head(self.setup), "v1.0.0", "the other checkout is still returned")

    def test_unknown_refs_and_bad_states_are_errors_that_change_nothing(self):
        unknown = self.update("--setup-ref", "v9.9.9")
        self.assertEqual(unknown.returncode, 65)
        self.assertIn("Unknown setup ref v9.9.9", unknown.stderr)
        self.assertIn("v1.1.0", unknown.stderr, "the known tags are listed")
        self.assertEqual(self.update("--server-ref", "nope", "--check").returncode, 65)
        self.assertEqual(self.update().returncode, 64, "a ref is required")
        (self.server / "bin/x").write_text("local edit")
        dirty = self.update("--server-ref", "v0.2.0")
        self.assertEqual(dirty.returncode, 65)
        self.assertIn("local changes", dirty.stderr)
        (self.server / "bin/x").write_text("1")
        busy = self.update("--setup-ref", "v1.1.0", FAKE_MAKE="1")
        self.assertEqual(busy.returncode, 75)
        self.assertIn("pipeline", busy.stderr)
        self.assertEqual((self.head(self.server), self.head(self.setup)), ("v0.1.0", "v1.0.0"))
        self.runner_untouched()

    def test_branches_and_commits_are_allowed_but_setup_off_a_tag_is_stage_only(self):
        stage = self.update("--setup-ref", "stage-fix")
        self.assertEqual(stage.returncode, 0, stage.stdout + stage.stderr)
        self.assertIn("not a release tag", stage.stdout)
        self.assertIn("stage only", stage.stdout)
        self.assertEqual(self.git("-C", str(self.setup), "show", "HEAD:Makefile"), "VERSION=stage")
        commit = self.git("-C", str(self.root / "server-work"), "rev-parse", "v0.2.0")
        by_commit = self.update("--server-ref", commit[:10])
        self.assertEqual(by_commit.returncode, 0, by_commit.stdout + by_commit.stderr)
        self.assertEqual(self.git("-C", str(self.server), "rev-parse", "HEAD"), commit)
        self.assertNotIn("stage only", by_commit.stdout, "setup-server itself may run untagged")


UBUNTU = os.environ.get("SETUP_SERVER_UBUNTU_IMAGE")


@unittest.skipUnless(UBUNTU, "Set SETUP_SERVER_UBUNTU_IMAGE (e.g. ubuntu:24.04) to check on a real Ubuntu")
class RealUbuntuTest(unittest.TestCase):
    def test_check_runs_on_ubuntu_and_the_runner_java_is_installable(self):
        script = ("apt-get update -qq >/dev/null && apt-cache policy openjdk-25-jre-headless | grep -q 'Candidate: [0-9]' "
                  "&& echo JAVA_AVAILABLE; /setup-server/bin/prepare-host --check; true")
        result = subprocess.run(["docker", "run", "--rm", "-v", f"{ROOT}:/setup-server:ro", UBUNTU, "bash", "-c", script],
                                capture_output=True, text=True, timeout=600)
        self.assertIn("JAVA_AVAILABLE", result.stdout, result.stderr)
        self.assertIn("would apt-get install", result.stdout)
        self.assertIn("Docker: missing", result.stdout)


if __name__ == "__main__":
    unittest.main()
