"""Host scripts in --check mode with stubbed system commands: nothing is installed or changed.

B level (opt-in): SETUP_SERVER_UBUNTU_IMAGE=ubuntu:24.04 runs prepare-host --check inside a real
Ubuntu container and confirms the Java package the runner needs is installable there, and installs the
runner (a stand-in archive) for systemd-analyze verify of its unit.
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


class PrepareHostKeysTest(ScriptTest):
    """prepare-host as (fake) root on a ready host: the git hosts' SSH keys it records."""
    PUBLISHED = {  # the providers' known_hosts lines (see the fingerprints in bin/prepare-host)
        "github.com": "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl",
        "bitbucket.org": "bitbucket.org ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIIazEu89wgQZ4bqs3d63QSMzYVa0MuJ2e2gKTKqu+UUO",
    }

    def setUp(self):
        super().setUp()
        root_bin = self.fake_root()
        self.stub("dpkg-query", 'printf "install ok installed"\n')
        self.stub("docker", 'echo 2.29.1\n')
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.root / "other")], check=True)
        other = (self.root / "other.pub").read_text().split()[1]
        # The first scan of a host answers its published key, every later one another key (or always with
        # FAKE_HOSTKEY): what is recorded must be what was checked.
        cases = "".join(f'  {host}) published="{line}";;\n' for host, line in self.PUBLISHED.items())
        self.stub("ssh-keyscan", f'for host; do :; done\ncase "$host" in\n{cases}esac\n'
                                 f'if [ -z "${{FAKE_HOSTKEY:-}}" ] && [ ! -e {self.root}/scanned-$host ]; then\n'
                                 f'  touch {self.root}/scanned-$host; echo "$published"\n'
                                 f'else echo "$host ssh-ed25519 {other}"; fi\n')
        self.known = self.root / "srv/.ssh/known_hosts"
        self.prepare = lambda **env: self.run_script(
            "prepare-host", PATH=f"{root_bin}:{self.bin}:{os.environ['PATH']}", SETUP_SERVER_USER=getpass.getuser(),
            SETUP_REPO=str(self.root / "missing.git"), **env)

    def test_the_published_host_keys_are_recorded_with_their_own_fingerprints(self):
        result = self.prepare()
        self.assertEqual(sorted(self.known.read_text().splitlines()), sorted(self.PUBLISHED.values()),
                         result.stdout + result.stderr)
        self.assertIn("github.com fingerprint: SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU (published)", result.stdout)
        self.assertIn("bitbucket.org fingerprint: SHA256:ybgmFkzwOSotHTHLJgHO0QN8L0xErw6vd0VhFA9m3SM (published)",
                      result.stdout)
        self.assertNotIn("host key", result.stdout.split("Needs attention")[-1])

    def test_a_host_key_that_is_not_the_published_one_is_refused(self):
        result = self.prepare(FAKE_HOSTKEY="1")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self.known.read_text() if self.known.exists() else "", "", "nothing recorded")
        self.assertIn("github.com: the scanned host key SHA256:", result.stdout)
        self.assertIn("is not the published SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU; nothing recorded",
                      result.stdout)


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

    def interactive(self, answers, *arguments):
        """Run install-runner --check in a real terminal and type the answers one line at a time."""
        import pty, select
        env = {**os.environ, "SETUP_SERVER_ROOT": str(self.root / "srv"), "PATH": f"{self.bin}:{os.environ['PATH']}"}
        pid, fd = pty.fork()
        if pid == 0:
            os.execve(str(ROOT / "bin/install-runner"), [str(ROOT / "bin/install-runner"), "--check", *arguments], env)
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


# The shape of GitHub's "New self-hosted runner" page (Linux x64), with a made-up token.
GITHUB_TOKEN = "AAGHFAKETOKEN0123456789ABCDE"
GITHUB_PAGE = f"""# Create a folder
$ mkdir actions-runner && cd actions-runner
# Download the latest runner package
$ curl -o actions-runner-linux-x64-2.338.0.tar.gz -L https://github.com/actions/runner/releases/download/v2.338.0/actions-runner-linux-x64-2.338.0.tar.gz
# Optional: Validate the hash
$ echo "af4b794c1bc41d73d40535e3fe092a39f9679cd8d965954c2aca25a05ca41d32  actions-runner-linux-x64-2.338.0.tar.gz" | shasum -a 256 -c
# Extract the installer
$ tar xzf ./actions-runner-linux-x64-2.338.0.tar.gz
# Create the runner and start the configuration experience
$ ./config.sh --url https://github.com/BigSant --token {GITHUB_TOKEN}
# Last step, run it!
$ ./run.sh
"""
GITHUB_UNIT = "setup-server-github-runner@bigsant.service"


class InstallGitHubRunnerTest(ScriptTest):
    """install-runner --provider github: the page's URL and token, the pinned archive and its SHA-256."""

    def setUp(self):
        super().setUp()
        self.stub("uname", 'echo x86_64\n')
        self.stub("hostname", 'echo worker1\n')

    def github(self, *arguments, stdin=GITHUB_PAGE, **env):
        return self.run_script("install-runner", "--provider", "github", *arguments, stdin=stdin, **env)

    def test_check_takes_the_url_and_token_from_the_page_and_hides_the_token(self):
        result = self.github("--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        output = result.stdout + result.stderr
        self.assertIn("GitHub runner of the organization BigSant (https://github.com/BigSant)", result.stdout)
        self.assertIn("runner archive: https://github.com/actions/runner/releases/download/v2.338.0/"
                      "actions-runner-linux-x64-2.338.0.tar.gz (SHA-256 "
                      "af4b794c1bc41d73d40535e3fe092a39f9679cd8d965954c2aca25a05ca41d32)", result.stdout)
        self.assertIn("runner worker1: labels self-hosted, Linux, X64 (GitHub's) and setup.build; "
                      f"directory {self.root}/srv/github-runner/bigsant", result.stdout)
        self.assertIn(f"registration token: <hidden> ({len(GITHUB_TOKEN)} characters", result.stdout)
        self.assertIn(f"service {GITHUB_UNIT}", result.stdout)
        self.assertNotIn(GITHUB_TOKEN, output)
        self.assertNotIn("Note:", result.stderr, "the page offers the pinned version")
        self.assertFalse((self.root / "srv").exists(), "--check changes nothing")

    def test_a_repository_runner_has_its_own_scope_and_a_name_can_be_given(self):
        page = f"./config.sh --url https://github.com/BigSant/Forsena_Docker.git/ --token {GITHUB_TOKEN}\n"
        result = self.github("--check", "--name", "vpn-server", stdin=page)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("GitHub runner of the repository BigSant/Forsena_Docker", result.stdout)
        self.assertIn(f"runner vpn-server: labels", result.stdout)
        self.assertIn(f"{self.root}/srv/github-runner/bigsant.forsena_docker", result.stdout)
        self.assertIn("setup-server-github-runner@bigsant.forsena_docker.service", result.stdout)

    def test_a_newer_version_on_the_page_is_noted_and_the_pinned_one_kept(self):
        result = self.github("--check", stdin=GITHUB_PAGE.replace("2.338.0", "2.340.1"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("GitHub offers 2.340.1; this installs the pinned 2.338.0", result.stderr)
        self.assertIn("v2.338.0/actions-runner-linux-x64-2.338.0.tar.gz", result.stdout)

    def test_bad_values_are_refused(self):
        page = GITHUB_PAGE
        for text, arguments, code, message in (
                ("$ ./run.sh\n", (), 65, "No organization or repository URL"),
                (page.replace("https://github.com/BigSant", "https://gitlab.com/BigSant"), (), 65, "Not a github.com"),
                (page.replace("https://github.com/BigSant", "https://github.com/Big;Sant"), (), 65, "Not a github.com"),
                (page.replace(f" --token {GITHUB_TOKEN}", ""), (), 65, "No registration token"),
                (page.replace(GITHUB_TOKEN, "abc$(id)"), (), 65, "Unexpected characters"),
                (page, ("--name", "bad name;"), 65, "Unexpected runner name"),
                (page, ("--sha256", "abc"), 64, "64 hex digits")):
            with self.subTest(message=message):
                result = self.github("--check", *arguments, stdin=text)
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                self.assertIn(message, result.stderr)
                self.assertNotIn(GITHUB_TOKEN, result.stdout + result.stderr)
        self.stub("uname", 'echo riscv64\n')
        self.assertIn("No pinned GitHub runner for riscv64", self.github("--check").stderr)

    def test_the_provider_options_are_checked(self):
        self.assertEqual(self.run_script("install-runner", "--check", "--provider", "gitlab").returncode, 64)
        bitbucket = self.run_script("install-runner", "--check", "--sha256", "a" * 64, stdin=RUN_COMMAND)
        self.assertEqual(bitbucket.returncode, 64)
        self.assertIn("for --provider github", bitbucket.stderr)
        # The default stays Bitbucket.
        self.assertIn("--OAuthClientSecret <hidden>",
                      self.run_script("install-runner", "--check", "--tarball", "x", stdin=RUN_COMMAND).stdout)

    def test_a_terminal_is_asked_for_the_url_and_then_the_token_which_is_not_shown(self):
        output = InstallRunnerTest.interactive(self, ["https://github.com/BigSant", GITHUB_TOKEN], "--provider", "github")
        self.assertIn("GitHub runner of the organization BigSant", output)
        self.assertIn(f"registration token: <hidden> ({len(GITHUB_TOKEN)} characters", output)
        self.assertNotIn(GITHUB_TOKEN, output, "the token is neither echoed nor printed")


class InstallGitHubRunnerRootTest(ScriptTest):
    """install-runner --provider github as (fake) root: a stand-in runner archive, config.sh and systemctl."""

    def setUp(self):
        super().setUp()
        self.root_bin = self.fake_root()
        self.stub("uname", 'echo x86_64\n')
        self.stub("hostname", 'echo worker1\n')
        self.stub("ldconfig", 'echo "libicuuc.so.74 (libc6,x86-64) => /lib/x86_64-linux-gnu/libicuuc.so.74"\n')
        self.calls = self.root / "systemctl.calls"
        self.stub("systemctl", f'echo "$*" >> {self.calls}\n'
                               'case "$1" in is-active) [ -n "${FAKE_ACTIVE:-}" ] || { [ "$2" = --quiet ] || echo inactive; exit 3; }; '
                               '[ "$2" = --quiet ] || echo active;; esac\n')
        # The stand-in archive: config.sh records how it was called and registers (.runner).
        self.config_log = self.root / "config.calls"
        archive = self.root / "archive"
        (archive / "bin").mkdir(parents=True)
        (archive / "config.sh").write_text(
            "#!/bin/sh\n"
            f'echo "args: $*" >> {self.config_log}\n'
            f'echo "token: ${{ACTIONS_RUNNER_INPUT_TOKEN:-none}}" >> {self.config_log}\n'
            f'echo "cwd: $(pwd)" >> {self.config_log}\n'
            '[ -z "${FAKE_CONFIG_FAILS:-}" ] || exit 1\n'
            'echo \'{"agentName": "worker1"}\' > .runner\n')
        (archive / "bin/runsvc.sh").write_text("#!/bin/sh\nexit 0\n")
        (archive / "bin/installdependencies.sh").write_text(f"#!/bin/sh\necho installed >> {self.root}/dependencies.calls\n")
        for script in ("config.sh", "bin/runsvc.sh", "bin/installdependencies.sh"):
            (archive / script).chmod(0o755)
        self.archive = self.root / "runner.tar.gz"
        subprocess.run(["tar", "-czf", str(self.archive), "-C", str(archive), "."], check=True)
        import hashlib
        self.sha256 = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.directory = self.root / "srv/github-runner/bigsant"
        self.template = self.root / "systemd/setup-server-github-runner@.service"

    def install(self, *arguments, stdin=GITHUB_PAGE, **env):
        return self.run_script("install-runner", "--provider", "github", "--tarball", str(self.archive), *arguments,
                               stdin=stdin, PATH=f"{self.root_bin}:{self.bin}:{os.environ['PATH']}",
                               SETUP_SERVER_USER=getpass.getuser(), SETUP_SERVER_SYSTEMD=str(self.root / "systemd"),
                               SETUP_SERVER_UNIT=str(self.root / "systemd/setup-server-runner.service"), **env)

    def everything_written(self):
        texts = [self.calls.read_text() if self.calls.exists() else "", self.sudo_log.read_text()]
        for path in (self.root / "srv").rglob("*"):
            if path.is_file():
                texts.append(path.read_text(errors="replace"))
        for path in (self.root / "systemd").rglob("*"):
            texts.append(path.read_text())
        return "\n".join(texts)

    def test_it_verifies_registers_as_the_account_and_starts_its_own_service(self):
        (self.root / "systemd").mkdir()
        result = self.install("--sha256", self.sha256.upper())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SHA-256 verified", result.stdout)
        config = self.config_log.read_text()
        self.assertIn("args: --unattended --url https://github.com/BigSant --name worker1 --labels setup.build "
                      "--work _work --replace", config)
        self.assertIn(f"token: {GITHUB_TOKEN}", config, "config.sh gets the token from its environment")
        self.assertIn(f"cwd: {self.directory}", config)
        sudo = self.sudo_log.read_text()
        self.assertIn(f"-u {getpass.getuser()} -H --preserve-env=ACTIONS_RUNNER_INPUT_TOKEN ./config.sh", sudo)
        self.assertNotIn(GITHUB_TOKEN, self.everything_written() + result.stdout + result.stderr,
                         "the token is on no command line and in no file")
        self.assertTrue((self.directory / ".runner").is_file())
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o750, "not the archive's 0755")
        self.assertNotIn("previous unit", result.stdout)
        self.assertFalse((self.root / "dependencies.calls").exists(), "ICU is there: no dependencies installed")
        unit = self.template.read_text()
        self.assertIn(f"User={getpass.getuser()}", unit)
        self.assertIn(f"WorkingDirectory={self.root}/srv/github-runner/%i", unit)
        self.assertIn(f"ExecStart={self.root}/srv/github-runner/%i/bin/runsvc.sh", unit)
        self.assertIn(f"Environment=SETUP_SERVER_ROOT={self.root}/srv", unit)
        self.assertIn(f"Environment=SETUP_DIRECTORY={self.root}/srv/setup", unit)
        self.assertIn("KillMode=process", unit)
        calls = self.calls.read_text().splitlines()
        self.assertIn(f"enable --now {GITHUB_UNIT}", calls)
        self.assertNotIn("restart", self.calls.read_text())
        self.assertFalse([call for call in calls if "setup-server-runner" in call], "the Bitbucket runner is not touched")
        self.assertFalse((self.root / "systemd/setup-server-runner.service").exists())

    def test_a_registered_scope_needs_no_token_and_is_not_registered_again(self):
        (self.root / "systemd").mkdir()
        self.assertEqual(self.install("--sha256", self.sha256).returncode, 0)
        self.config_log.unlink()
        again = self.install(stdin="./config.sh --url https://github.com/BigSant\n", FAKE_ACTIVE="1")
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertIn("registered already", again.stdout)
        self.assertFalse(self.config_log.exists(), "no second registration")
        self.assertNotIn("still runs the previous unit", again.stdout, "the unit did not change")
        self.template.write_text("[Service]\n")
        changed = self.install(stdin="./config.sh --url https://github.com/BigSant\n", FAKE_ACTIVE="1")
        self.assertIn("restart it when no job runs", changed.stdout)
        self.assertNotRegex(self.calls.read_text(), r"(?m)^restart ", "a running runner is never restarted")

    def test_an_archive_with_another_checksum_is_not_installed(self):
        result = self.install()  # the stand-in archive is not the pinned release
        self.assertEqual(result.returncode, 65, result.stdout + result.stderr)
        self.assertIn(f"SHA-256 is {self.sha256}, not af4b794c", result.stderr)
        self.assertFalse(self.config_log.exists())
        self.assertFalse((self.directory / "config.sh").exists(), "nothing unpacked")
        self.assertFalse(self.template.exists())

    def test_missing_icu_runs_the_runners_dependency_script_and_a_failed_registration_stops(self):
        (self.root / "systemd").mkdir()
        self.stub("ldconfig", 'echo "libc.so.6 (libc6,x86-64) => /lib/x86_64-linux-gnu/libc.so.6"\n')
        result = self.install("--sha256", self.sha256, FAKE_CONFIG_FAILS="1")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual((self.root / "dependencies.calls").read_text(), "installed\n")
        self.assertIn("take a new one and run this again", result.stderr)
        self.assertFalse(self.template.exists(), "no service without a registration")


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


class AddProjectTargetTest(ScriptTest):
    """The deploy target is the server's: target.env (or target.<env>.env) in the build root, never the project's env."""

    def run_add(self, *arguments, **env):
        root_bin = self.fake_root()
        self.stub("git", 'case "$1" in\n'
                         '  clone) for last; do :; done; mkdir -p "$last/.git"; echo "$3" > "$last/.git/origin";;\n'
                         '  -C) cat "$2/.git/origin";;\n'
                         'esac\n')
        self.stub("ssh", 'echo "$*" >> "$SSH_LOG"\n'
                         '[ -z "${FAKE_SSH_FAIL:-}" ] || { echo "Permission denied (publickey)." >&2; exit 255; }\n'
                         'printf "1012\\n1013\\n27.3.1\\nwritable\\n"\n')
        self.stub("docker", 'echo "$*" >> "$DOCKER_LOG"\ncase "$1 $2" in "context inspect") exit 1;; esac\n')
        self.stub("ssh-keygen", 'echo "shop.example ssh-ed25519 AAAA"\n')
        (self.root / "srv/.ssh").mkdir(parents=True, exist_ok=True)
        (self.root / "srv/.ssh/id_ed25519.pub").write_text("ssh-ed25519 AAAAsetupkey setup@worker\n")
        self.ssh_log, self.docker_log = self.root / "ssh.calls", self.root / "docker.calls"
        return self.run_script("add-project", *arguments, PATH=f"{root_bin}:{self.bin}:{os.environ['PATH']}",
                               SETUP_SERVER_USER=getpass.getuser(), SSH_LOG=str(self.ssh_log),
                               DOCKER_LOG=str(self.docker_log), **env)

    def test_check_plans_the_local_target_and_a_remote_one(self):
        build_root = self.root / "srv/projects/asana_tracker"
        local = self.run_script("add-project", "--check", "git@bitbucket.org:rsmaizys/asana_tracker.git")
        self.assertEqual(local.returncode, 0, local.stdout + local.stderr)
        self.assertIn(f"would write {build_root}/target.env:", local.stdout)
        self.assertIn(f"DEPLOY_CONTEXT=default\n    DEPLOY_SSH=\n    DEPLOY_PATH={build_root}\n", local.stdout)
        remote = self.run_script("add-project", "--check", "--target", "prod=ssh://deploy@shop.example/home/deploy/shop",
                                 "git@bitbucket.org:rsmaizys/asana_tracker.git")
        self.assertEqual(remote.returncode, 0, remote.stdout + remote.stderr)
        self.assertIn(f"would write {build_root}/target.prod.env:", remote.stdout)
        self.assertIn("DEPLOY_CONTEXT=setup-asana_tracker-prod\n    DEPLOY_SSH=deploy@shop.example\n"
                      "    DEPLOY_PATH=/home/deploy/shop", remote.stdout)
        bad = self.run_script("add-project", "--check", "--target", "ftp://x/y", "git@bitbucket.org:rsmaizys/asana_tracker.git")
        self.assertEqual(bad.returncode, 64)
        self.assertIn("expected local or ssh://USER@HOST/ABSOLUTE/PATH", bad.stderr)
        self.assertFalse((self.root / "srv").exists(), "--check changes nothing")

    def test_the_local_target_is_written_once_and_kept_on_a_rerun(self):
        result = self.run_add("git@bitbucket.org:rsmaizys/asana_tracker.git")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        target = self.root / "srv/projects/asana_tracker/target.env"
        text = target.read_text()
        self.assertIn(f"DEPLOY_PATH={self.root}/srv/projects/asana_tracker\n", text)
        self.assertIn(f"APP_UID={os.getuid()}\nAPP_GID={os.getgid()}\n", text)
        self.assertEqual(target.stat().st_mode & 0o777, 0o640)
        target.write_text(text + "# edited\n")
        again = self.run_add("git@bitbucket.org:rsmaizys/asana_tracker.git")
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertIn("deploy target kept", again.stdout)
        self.assertTrue(target.read_text().endswith("# edited\n"))

    def test_a_remote_target_needs_the_key_then_gets_a_context_and_the_users_ids(self):
        arguments = ("--target", "prod=ssh://deploy@shop.example/home/deploy/shop", "git@bitbucket.org:rsmaizys/forsena_docker.git")
        refused = self.run_add(*arguments, FAKE_SSH_FAIL="1")
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertIn("cannot log in to deploy@shop.example without a password", refused.stderr)
        self.assertIn("ssh-ed25519 AAAAsetupkey setup@worker", refused.stderr)
        target = self.root / "srv/projects/forsena_docker/target.prod.env"
        self.assertFalse(target.exists())
        result = self.run_add(*arguments)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("BatchMode=yes", self.ssh_log.read_text())
        self.assertIn("context create setup-forsena_docker-prod --docker host=ssh://deploy@shop.example",
                      self.docker_log.read_text())
        text = target.read_text()
        for line in ("DEPLOY_CONTEXT=setup-forsena_docker-prod", "DEPLOY_SSH=deploy@shop.example",
                     "DEPLOY_PATH=/home/deploy/shop", "APP_UID=1012", "APP_GID=1013"):
            self.assertIn(line + "\n", text)


class AddGitHubProjectTest(ScriptTest):
    """add-project with GitHub repositories: a deploy key and host alias per repository (GitHub refuses one
    deploy key on several repositories), or the account's key with --shared-key."""

    def test_check_plans_a_deploy_key_and_alias_per_repository(self):
        result = self.run_script("add-project", "--check", "git@github.com:BigSant/Forsena_Docker.git",
                                 "ssh://git@github.com/BigSant/forsena.git")
        self.assertEqual(result.returncode, 0, result.stderr)
        ssh = self.root / "srv/.ssh"
        self.assertIn(f"would create the deploy key {ssh}/github-bigsant.forsena_docker (unless present) and the host "
                      f"alias github.com-bigsant.forsena_docker in {ssh}/config", result.stdout)
        self.assertIn("would clone git@github.com-bigsant.forsena_docker:BigSant/Forsena_Docker.git into "
                      f"{self.root}/srv/projects/forsena_docker/app", result.stdout)
        self.assertIn("would clone git@github.com-bigsant.forsena:BigSant/forsena.git into "
                      f"{self.root}/srv/projects/forsena_docker/app/public", result.stdout)
        self.assertIn("Workflows of 'forsena_docker' find it themselves", result.stdout)
        self.assertIn("GitHub environment secrets", result.stdout)
        self.assertFalse((self.root / "srv").exists(), "--check changes nothing")

    def test_a_shared_key_clones_the_url_as_given(self):
        result = self.run_script("add-project", "--check", "--shared-key", "git@github.com:BigSant/asana_tracker.git")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("would clone git@github.com:BigSant/asana_tracker.git into", result.stdout)
        self.assertNotIn("deploy key", result.stdout)

    def test_the_key_waits_for_github_and_the_alias_reaches_github_with_only_that_key(self):
        root_bin = self.fake_root()
        # git: ls-remote reads only with FAKE_ACCESS; clone records its URL as origin.
        self.stub("git", 'case "$1" in\n'
                         '  ls-remote) echo "$*" >> "$GIT_LOG"; [ -n "${FAKE_ACCESS:-}" ];;\n'
                         '  clone) for last; do :; done; mkdir -p "$last/.git"; echo "$3" > "$last/.git/origin"; echo "$*" >> "$GIT_LOG";;\n'
                         '  -C) cat "$2/.git/origin";;\n'
                         'esac\n')
        git_log = self.root / "git.calls"
        run = lambda **env: self.run_script("add-project", "git@github.com:BigSant/Forsena_Docker.git",
                                            PATH=f"{root_bin}:{self.bin}:{os.environ['PATH']}",
                                            SETUP_SERVER_USER=getpass.getuser(), GIT_LOG=str(git_log), **env)
        waiting = run()
        self.assertEqual(waiting.returncode, 1, waiting.stdout + waiting.stderr)
        key = self.root / "srv/.ssh/github-bigsant.forsena_docker"
        self.assertIn("https://github.com/BigSant/Forsena_Docker/settings/keys", waiting.stdout)
        self.assertIn(key.with_name(key.name + ".pub").read_text().strip(), waiting.stdout)
        self.assertNotIn("clone", git_log.read_text())
        public = key.with_name(key.name + ".pub").read_text()
        config = self.root / "srv/.ssh/config"
        self.assertEqual(config.stat().st_mode & 0o777, 0o600)

        cloned = run(FAKE_ACCESS="1")
        self.assertEqual(cloned.returncode, 0, cloned.stdout + cloned.stderr)
        self.assertIn("clone -q git@github.com-bigsant.forsena_docker:BigSant/Forsena_Docker.git", git_log.read_text())
        self.assertEqual(key.with_name(key.name + ".pub").read_text(), public, "the key is made once")
        self.assertEqual(config.read_text().count("Host github.com-bigsant.forsena_docker"), 1, "the alias is added once")
        again = run(FAKE_ACCESS="1")
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertIn("app: present", again.stdout)
        # Real ssh resolves the alias to github.com with only this key (the host key of github.com is checked).
        resolved = subprocess.run(["ssh", "-G", "-F", str(config), "github.com-bigsant.forsena_docker"],
                                  capture_output=True, text=True, check=True).stdout.splitlines()
        for line in ("hostname github.com", "user git", f"identityfile {key}", "identitiesonly yes"):
            self.assertIn(line, resolved)


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

    def test_the_older_app_docker_layout_counts_too(self):
        # backup-projects and worker-metrics also use app/docker/: its backups are not lost silently.
        (self.build_root / "app/docker/env").mkdir(parents=True)
        (self.build_root / "app/docker/Makefile").write_text("include x\n")
        (self.build_root / "app/docker/.generated/backups").mkdir(parents=True)
        (self.build_root / "app/docker/.generated/backups/worker-prod-20261009T013000Z.sql.gz").write_text("x")
        (self.build_root / "app/docker/.generated/release/stage").mkdir(parents=True)
        (self.build_root / "app/docker/.generated/release/stage/deployments.json").write_text("[]")
        refused = self.run_script("remove-project", "--check", "demo", SETUP_SERVER_CONFIG=str(self.root / "etc"))
        self.assertEqual(refused.returncode, 65, refused.stdout + refused.stderr)
        self.assertIn("database backups: 1; environments with deploy history: 1", refused.stdout)
        allowed = self.run_script("remove-project", "--check", "--with-backups", "demo",
                                  SETUP_SERVER_CONFIG=str(self.root / "etc"))
        self.assertEqual(allowed.returncode, 0, allowed.stdout + allowed.stderr)

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
            for unit in ("setup-server-runner", "setup-server-github-runner@"):
                self.assertNotRegex(calls, rf"(?m)^{verb}( \S+)* {unit}", f"systemctl {verb} was called:\n{calls}")
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

    def github_runner(self):
        """Both runners active: Bitbucket's as PID 4242, a GitHub one as PID 6161 (6262 after a restart
        with FAKE_GITHUB_RESTARTED)."""
        self.stub("systemctl", f'echo "$*" >> {self.calls}\n'
                               'for unit; do :; done\n'
                               'case "$1" in\n'
                               f'  list-units) echo "{GITHUB_UNIT} loaded active running GitHub Actions runner bigsant (setup-server)";;\n'
                               '  show) case "$unit" in\n'
                               f'    *github*) n=$(grep -c "^show.*github" {self.calls}); '
                               '[ -n "${FAKE_GITHUB_RESTARTED:-}" ] && [ "$n" -gt 1 ] && echo 6262 || echo 6161;;\n'
                               '    *) echo 4242;; esac;;\n'
                               'esac\n')

    def test_a_github_runner_beside_the_bitbucket_one_is_checked_and_never_restarted(self):
        self.github_runner()
        result = self.update("--server-ref", "v0.2.0")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"runner {GITHUB_UNIT} active (PID 6161); an update never restarts it", result.stdout)
        self.assertIn("runner setup-server-runner: still active, same PID 4242", result.stdout)
        self.assertIn(f"runner {GITHUB_UNIT}: still active, same PID 6161", result.stdout)
        self.assertRegex(self.calls.read_text(), r"(?m)^list-units .*--state=active .*setup-server-github-runner@\*\.service")
        self.runner_untouched()
        self.calls.unlink()
        restarted = self.update("--server-ref", "v0.1.0", FAKE_GITHUB_RESTARTED="1")
        self.assertEqual(restarted.returncode, 1, restarted.stdout + restarted.stderr)
        self.assertIn(f"runner {GITHUB_UNIT}: FAILED — PID changed from 6161 to 6262 (restarted)", restarted.stdout)
        self.assertEqual(self.head(self.server), "v0.2.0", "the first update stays, the second returned")
        self.runner_untouched()

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

    def test_a_tag_moved_upstream_is_an_error_and_the_local_tag_stays(self):
        self.assertEqual(self.update("--check", "--setup-ref", "v1.1.0").returncode, 0)  # fetches v1.1.0
        before = self.git("-C", str(self.setup), "rev-parse", "v1.1.0")
        work = str(self.root / "setup-work")
        self.commit("setup", None, {"Makefile": "VERSION=rewritten\n"}, "Rewritten 1.1.0")
        self.git("-C", work, "tag", "-f", "v1.1.0")
        self.git("-C", work, "push", "-q", "-f", "origin", "v1.1.0")
        for arguments in (("--check", "--setup-ref", "v1.1.0"), ("--setup-ref", "v1.0.0")):
            with self.subTest(arguments=arguments):
                result = self.update(*arguments)
                self.assertEqual(result.returncode, 65, result.stdout + result.stderr)
                self.assertIn("tag v1.1.0 was moved on origin", result.stderr)
                self.assertEqual(self.git("-C", str(self.setup), "rev-parse", "v1.1.0"), before)
                self.assertEqual(self.head(self.setup), "v1.0.0")

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
        for ref in ("master", "origin/master"):  # a branch of origin, with or without the remote's name
            with self.subTest(ref=ref):
                branch = self.update("--server-ref", ref, "--check")
                self.assertEqual(branch.returncode, 0, branch.stdout + branch.stderr)


UBUNTU = os.environ.get("SETUP_SERVER_UBUNTU_IMAGE")
# The test containers share the host with running stacks: capped, removed when done.
DOCKER_RUN = ["docker", "run", "--rm", "--memory", "1g"]


@unittest.skipUnless(UBUNTU, "Set SETUP_SERVER_UBUNTU_IMAGE (e.g. ubuntu:24.04) to check on a real Ubuntu")
class RealUbuntuTest(unittest.TestCase):
    def test_check_runs_on_ubuntu_and_the_runner_java_is_installable(self):
        script = ("apt-get update -qq >/dev/null && apt-cache policy openjdk-25-jre-headless | grep -q 'Candidate: [0-9]' "
                  "&& echo JAVA_AVAILABLE; /setup-server/bin/prepare-host --check; true")
        result = subprocess.run([*DOCKER_RUN, "-v", f"{ROOT}:/setup-server:ro", UBUNTU, "bash", "-c", script],
                                capture_output=True, text=True, timeout=600)
        self.assertIn("JAVA_AVAILABLE", result.stdout, result.stderr)
        self.assertIn("would apt-get install", result.stdout)
        self.assertIn("Docker: missing", result.stdout)

    def test_the_installed_runner_unit_verifies(self):
        # A stand-in archive (bin/start.sh) and a stub systemctl: the unit is checked by systemd-analyze.
        script = """set -eu
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null && apt-get install -y -qq systemd >/dev/null
useradd --system --create-home --home-dir /srv/setup-server --shell /bin/bash setup
mkdir -p /stubs /tmp/archive/bin
printf '#!/bin/sh\\nexit 0\\n' > /stubs/systemctl
printf '#!/bin/sh\\nexec java -jar runner.jar\\n' > /tmp/archive/bin/start.sh
chmod +x /stubs/systemctl /tmp/archive/bin/start.sh
tar -czf /tmp/runner.tar.gz -C /tmp/archive bin
PATH=/stubs:$PATH /setup-server/bin/install-runner --tarball /tmp/runner.tar.gz <<'DIALOG'
""" + RUN_COMMAND + """DIALOG
# verify only warns (exit 0) about a bad setting: any output is a finding.
systemd-analyze verify /etc/systemd/system/setup-server-runner.service >/tmp/verify.log 2>&1 || true
cat /tmp/verify.log; [ ! -s /tmp/verify.log ]
echo RUNNER_UNIT_VERIFIED
stat -c '%U %a %n' /etc/setup-server/runner.env
"""
        result = subprocess.run([*DOCKER_RUN, "-v", f"{ROOT}:/setup-server:ro", UBUNTU, "bash", "-c", script],
                                capture_output=True, text=True, timeout=600)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("RUNNER_UNIT_VERIFIED", output)
        self.assertIn("root 600 /etc/setup-server/runner.env", result.stdout)
        self.assertNotIn("s3cr3t", output)

    def test_the_github_runner_registers_through_real_sudo_and_its_unit_verifies(self):
        # A stand-in archive whose config.sh behaves like GitHub's on the points that matter here: it refuses
        # root and takes the token from ACTIONS_RUNNER_INPUT_TOKEN. Both runners' units then coexist.
        script = """set -eu
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null && apt-get install -y -qq systemd sudo >/dev/null
useradd --system --create-home --home-dir /srv/setup-server --shell /bin/bash setup
mkdir -p /stubs /tmp/archive/bin /tmp/bitbucket/bin
printf '#!/bin/sh\\nexit 0\\n' > /stubs/systemctl
cat > /tmp/archive/config.sh <<'CONFIG'
#!/bin/bash
[ "$(id -u)" != 0 ] || { echo "Must not run with sudo"; exit 1; }
[ "${ACTIONS_RUNNER_INPUT_TOKEN:-}" = """ + f'"{GITHUB_TOKEN}"' + """ ] || { echo "no token in the environment"; exit 1; }
case "$*" in *""" + GITHUB_TOKEN + """*) echo "token on the command line"; exit 1;; esac
echo "registered as $(id -un): $*"
echo '{}' > .runner
CONFIG
printf '#!/bin/sh\\nexit 0\\n' > /tmp/archive/bin/runsvc.sh
printf '#!/bin/sh\\necho dependencies installed\\n' > /tmp/archive/bin/installdependencies.sh
chmod +x /stubs/systemctl /tmp/archive/config.sh /tmp/archive/bin/*.sh
tar -czf /tmp/runner.tar.gz -C /tmp/archive .
printf '#!/bin/sh\\nexec java -jar runner.jar\\n' > /tmp/bitbucket/bin/start.sh
chmod +x /tmp/bitbucket/bin/start.sh
tar -czf /tmp/bitbucket.tar.gz -C /tmp/bitbucket bin
PATH=/stubs:$PATH /setup-server/bin/install-runner --tarball /tmp/bitbucket.tar.gz <<'DIALOG' >/dev/null
""" + RUN_COMMAND + """DIALOG
PATH=/stubs:$PATH /setup-server/bin/install-runner --provider github --tarball /tmp/runner.tar.gz \\
    --sha256 $(sha256sum /tmp/runner.tar.gz | cut -d' ' -f1) <<'PAGE'
""" + GITHUB_PAGE + """PAGE
for unit in setup-server-github-runner@bigsant.service setup-server-runner.service; do
    systemd-analyze verify /etc/systemd/system/$unit >/tmp/verify.log 2>&1 || true
    cat /tmp/verify.log; [ ! -s /tmp/verify.log ]
done
echo UNITS_VERIFIED
stat -c '%U %a %n' /srv/setup-server/github-runner/bigsant /srv/setup-server/github-runner/bigsant/.runner
! grep -rq """ + GITHUB_TOKEN + """ /srv /etc/systemd /etc/setup-server
echo TOKEN_NOWHERE
"""
        result = subprocess.run([*DOCKER_RUN, "-v", f"{ROOT}:/setup-server:ro", UBUNTU, "bash", "-c", script],
                                capture_output=True, text=True, timeout=600)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("registered as setup: --unattended --url https://github.com/BigSant", output)
        self.assertIn("UNITS_VERIFIED", output)
        self.assertIn("setup 750 /srv/setup-server/github-runner/bigsant\n", output)
        self.assertIn("TOKEN_NOWHERE", output)
        self.assertNotIn(GITHUB_TOKEN, output)


if __name__ == "__main__":
    unittest.main()
