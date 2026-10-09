"""Host scripts in --check mode with stubbed system commands: nothing is installed or changed.

B level (opt-in): SETUP_SERVER_UBUNTU_IMAGE=ubuntu:24.04 runs prepare-host --check inside a real
Ubuntu container and confirms the Java package the runner needs is installable there.
"""

import os
from pathlib import Path
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

    def os_release(self, distribution, version):
        (self.root / "os-release").write_text(f'ID={distribution}\nVERSION_ID="{version}"\nVERSION_CODENAME=noble\n'
                                               f'PRETTY_NAME="{distribution} {version}"\n')

    def meminfo(self, gigabytes):
        (self.root / "meminfo").write_text(f"MemTotal:       {gigabytes * 1048576} kB\n")

    def run_script(self, name, *arguments, stdin=""):
        env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}",
               "SETUP_SERVER_OS_RELEASE": str(self.root / "os-release"),
               "SETUP_SERVER_MEMINFO": str(self.root / "meminfo"),
               "SETUP_SERVER_ROOT": str(self.root / "srv"), "SETUP_SERVER_USER": "setup-test-account-missing"}
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
