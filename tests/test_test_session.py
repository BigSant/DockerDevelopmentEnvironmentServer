"""bin/test-session: the trust model (host code from a trusted commit, the caller's specs only as files), the tag rule,
patches as git apply sees them, the report digest, the root interface, the gates, cancel and timeouts, the session
lifecycle (reconcile, a real systemd unit), the results and the tombstones of client_run_id.

In-process over a temporary worker tree (every path of the module points there; nothing of an installed server is
read) with real git repositories and a fake docker; make is replaced where a test says so. What needs Docker and the
Playwright image (a session's make test-prepare, a real browser) is tests/fixtures/test_session_e2e.py, run on a
machine with Docker (README: Test environments).
"""

import hermetic  # noqa: F401  first: no test touches the installed server (tests/hermetic.py)
import contextlib
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests/fixtures"))
import test_session_project as fixture  # noqa: E402

UUID = "0f8fad5b-d9cb-469f-a165-70867728950e"
FAKE_DOCKER = r'''#!/bin/sh
# The tests' docker: every call logged; answers from files next to the log.
here=$(dirname "$0")
printf '%s\n' "$*" >> "$here/docker.log"
case "$1 $2" in
  "info --format") case "$*" in *"{{.ID}}"*) echo "FAKE:DAEMON:ID";; *) cat "$here/root.txt";; esac;;
  "version --format") cat "$here/version.txt" 2>/dev/null || echo 29.0.0;;
  "network ls") echo net1;;
  "ps -aq")
    # containers.txt: the names this fake daemon has; a name filter answers the one it names.
    name=$(printf '%s' "$*" | sed -n 's/.*name=^\([^$]*\)\$.*/\1/p')
    [ -n "$name" ] && grep -x -- "$name" "$here/containers.txt" 2>/dev/null
    exit 0;;
  "rm --force")
    [ -f "$here/rm.fail" ] && exit 1
    for id; do sed -i "/^$id\$/d" "$here/containers.txt" 2>/dev/null; done
    exit 0;;
  "network inspect") cat "$here/network.json" 2>/dev/null || echo '[{"Name": "net1", "Internal": true, "Options": {"com.docker.network.bridge.gateway_mode_ipv4": "isolated", "com.docker.network.bridge.gateway_mode_ipv6": "isolated"}, "IPAM": {"Config": [{"Subnet": "10.0.0.0/24"}]}}]';;
  "ps -a") cat "$here/ps.txt" 2>/dev/null;;
  "image ls") cat "$here/images.txt" 2>/dev/null; exit 0;;
  "image rm")
    [ -f "$here/image-rm.fail" ] && exit 1
    grep -vxF -- "$3" "$here/images.txt" > "$here/images.new" 2>/dev/null; mv "$here/images.new" "$here/images.txt" 2>/dev/null
    exit 0;;
  "builder prune") echo "Total:	1.2GB";;
  "compose -p")
    case "$*" in *" down "*) exit "$(cat "$here/down.exit" 2>/dev/null || echo 0)";; esac;;
esac
exit 0
'''


def load(root):
    loader = importlib.machinery.SourceFileLoader("test_session", str(ROOT / "bin/test-session"))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader("test_session", loader))
    with patch.dict(os.environ, {"SETUP_SERVER_ROOT": str(root / "srv"), "SETUP_SERVER_CONFIG": str(root / "etc"),
                                 "SETUP_SERVER_PROC": str(root / "proc"), "SETUP_SERVER_HANDOFF": str(root / "handoff"),
                                 "SETUP_SERVER_PATH": f"{root / 'fakebin'}:{os.environ['PATH']}"}):
        loader.exec_module(module)
    return module


def git(directory, *arguments):
    return subprocess.run(["git", "-C", str(directory), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *arguments],
                          check=True, capture_output=True, text=True).stdout.strip()


SPEC = "import { test } from '@playwright/test';\ntest('home', async () => {});\n"


def budget(directory, target, memory=3000, cpus=6):
    """What setup's test-init writes from the rendered model (tests replace make)."""
    if target == "test-init":
        (directory / ".generated/test").mkdir(parents=True, exist_ok=True)
        (directory / ".generated/test/session-budget.json").write_text(
            json.dumps({"memory_mb": memory, "cpus": cpus, "services": ["php-fpm"]}))


class Base(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ("srv/projects", "etc", "proc", "handoff/prod", "handoff/stage", "fakebin", "dockerroot"):
            (self.root / name).mkdir(parents=True)
        (self.root / "proc/meminfo").write_text("MemTotal: 16000000 kB\nMemAvailable: 9000000 kB\n")
        # This machine's tests share it knowingly (the host_separation gate has its own test).
        self.config("")
        (self.root / "proc/locks").write_text("")
        docker = self.root / "fakebin/docker"
        docker.write_text(FAKE_DOCKER)
        docker.chmod(0o755)
        (self.root / "fakebin/root.txt").write_text(f"{self.root / 'dockerroot'}\n")
        environment = patch.dict(os.environ, {"PATH": f"{self.root / 'fakebin'}:{os.environ['PATH']}"})
        environment.start()
        self.addCleanup(environment.stop)
        self.ts = load(self.root)
        self.assertTrue(str(self.ts.ROOT).startswith(str(self.root)))
        self.detached = []
        for name, value in (("detach", lambda *arguments: self.detached.append(arguments) or 999999999),
                            ("require_setup", lambda: None), ("STOP_GRACE", 1), ("worker_scope", lambda command: [])):
            patcher = patch.object(self.ts, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.project, self.work = self.build_root("alpha")

    def config(self, text):
        """$CONFIG/test-sessions.env with text; this machine's tests share the host knowingly (host_separation has
        its own test)."""
        (self.root / "etc/test-sessions.env").write_text("TEST_SESSIONS_SHARE_PRODUCTION_HOST=accept-risk\n" + text)

    def docker_calls(self):
        log = self.root / "fakebin/docker.log"
        return log.read_text().splitlines() if log.exists() else []

    def build_root(self, name):
        """An origin repository, a work clone to push from, and the build root (a clone with origin/HEAD = master)."""
        origin = self.root / "origins" / f"{name}.git"
        work = self.root / "work" / name
        app = work / "app"
        (app / "env").mkdir(parents=True)
        (app / "Makefile").write_text("include $(SETUP_DIRECTORY)/docker/project.mk\n")
        (app / "env/common.env").write_text(f"PROJECT_NAME={name}\n")
        (app / "env/stage.env.example").write_text("DOMAIN=stage.alpha.example www.stage.alpha.example\n")
        (app / "env/prod.env.example").write_text("DOMAIN=alpha.example\n")
        (app / "env/local.env.example").write_text("DOMAIN=alpha.local\n")
        (app / "config/playwright").mkdir(parents=True)
        (app / "config/playwright/playwright.config.cjs").write_text("module.exports = {};\n")
        tests = app / "tests/playwright"
        tests.mkdir(parents=True)
        (tests / "home.spec.ts").write_text(SPEC)
        (tests / "fixme.spec.ts").write_text(fixture.SPECS["fixme.spec.ts"])
        (tests / "package.json").write_text('{"name": "reviewed"}\n')
        (tests / "package-lock.json").write_text('{"lockfileVersion": 3}\n')
        git(work, "init", "-q", "-b", "master")
        git(work, "add", "-A")
        git(work, "commit", "-q", "-m", "init")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "master", str(origin)], check=True)
        git(work, "remote", "add", "origin", str(origin))
        git(work, "push", "-q", "origin", "master")
        root = self.ts.PROJECTS / name
        subprocess.run(["git", "clone", "-q", str(origin), str(root)], check=True)
        return root, work

    def push_branch(self, branch, files, links=()):
        git(self.work, "checkout", "-q", "-B", branch, "master")
        for path, text in files.items():
            target = self.work / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        for path, destination in links:
            (self.work / path).symlink_to(destination)
        git(self.work, "add", "-A", "-f")
        git(self.work, "commit", "-q", "-m", branch)
        git(self.work, "push", "-q", "-f", "origin", branch)
        git(self.work, "checkout", "-q", "master")
        git(self.project, "fetch", "-q", "origin")
        return git(self.project, "rev-parse", f"origin/{branch}")

    def refused(self, status, call, *arguments, reason=None, **keywords):
        with self.assertRaises(self.ts.Refusal) as caught:
            call(*arguments, **keywords)
        self.assertEqual(caught.exception.answer["status"], status, caught.exception.answer)
        if reason:
            self.assertEqual(caught.exception.answer.get("reason"), reason, caught.exception.answer)
        return caught.exception.answer

    def options(self, **keywords):
        return type("Options", (), {"project": "alpha", "db": "dataset:dev", "env_ref": None, "shop_ref": None, "ttl": None,
                                    "by": "token:agent", "client_run_id": None, **keywords})()

    def session(self, status="ready", **fields):
        directory = self.ts.SESSIONS / "alpha"
        moment = int(time.time())
        master = git(self.project, "rev-parse", "origin/master")
        data = {"id": "ts-0123456789ab", "project": "alpha", "status": status, "db": "dataset:dev", "created_at": moment,
                "expires_at": moment + 7200, "domain": "alpha.test.local", "env_commit": master, "tests_commit": master,
                "env_ref": "master", "compose_project": "alpha-test", "format": self.ts.SESSION_FORMAT, **fields}
        self.ts.write_json(directory / "session.json", data)
        return data

    def ready_session(self, env_ref=None):
        """A session made by create and its worker for real (git), with make replaced: (session, make calls)."""
        calls = []
        made = self.ts.create(self.options(env_ref=env_ref))[0]

        def fake_make(log, directory, target, *arguments, **keywords):
            budget(directory, target)
            content = self.ts.SESSIONS / "alpha/content"
            calls.append((target, arguments, keywords, sorted(str(path.relative_to(content)) for path in content.rglob("*")
                                                              if path.is_file()) if content.is_dir() else []))
            if target == "test-init":
                with open(directory / "env/test.env", "a") as handle:
                    handle.write("DOMAIN=alpha.test.local\n")
            return 0
        with patch.object(self.ts, "make", fake_make):
            self.ts.create_worker(made["id"])
        return self.ts.find_session(made["id"]), calls


class TrustTest(Base):
    """1, 12, 17: the host-side tree is a trusted commit's; a branch's specs are files on it."""

    def test_host_code_comes_only_from_the_deploy_branch_and_a_branch_gives_only_its_specs(self):
        master = git(self.project, "rev-parse", "origin/master")
        evil = self.push_branch("evil", {"app/Makefile": "all:\n\ttouch /tmp/pwned\n", "app/env/common.env": "PROJECT_NAME=evil\n",
                                         "app/tests/playwright/home.spec.ts": "// the branch's spec\n",
                                         "app/tests/playwright/package.json": '{"scripts": {"preinstall": "evil"}}\n',
                                         "app/tests/playwright/.npmrc": "registry=https://evil.example/\n",
                                         "app/tests/playwright/node_modules/ms/index.js": "planted()\n",
                                         "app/config/playwright/playwright.config.cjs": "// branch config\n"},
                                links=[("app/tests/playwright/link.ts", "/etc/passwd")])
        self.assertEqual(self.ts.trusted_commit(self.project, "evil"), (None, None))
        self.assertEqual(self.ts.trusted_commit(self.project), (master, "master"))
        session, calls = self.ready_session(env_ref="evil")
        self.assertEqual((session["status"], session["env_commit"], session["tests_commit"]), ("ready", master, evil), session)
        self.assertEqual(session["compose_project"], f"alpha-{session['id'][3:]}-test",
                         "named after the session (no other stack's), kept for a teardown without the clone")
        repo = self.ts.SESSIONS / "alpha/repo"
        app = repo / "app"
        self.assertEqual((app / "Makefile").read_text(), "include $(SETUP_DIRECTORY)/docker/project.mk\n")
        self.assertEqual((app / "env/common.env").read_text(), "PROJECT_NAME=alpha\n")
        content = self.ts.SESSIONS / "alpha/content"
        # The trusted tree keeps its own specs; the branch's are data directories outside it.
        self.assertEqual((app / "tests/playwright/home.spec.ts").read_text(), SPEC)
        self.assertEqual((content / "tests/home.spec.ts").read_text(), "// the branch's spec\n")
        self.assertEqual((content / "config/playwright.config.cjs").read_text(), "// branch config\n")
        self.assertEqual((content / "tests/package.json").read_text(), '{"name": "reviewed"}\n')
        self.assertFalse((content / "tests/.npmrc").exists(), "the package files are the trusted commit's only")
        self.assertFalse(os.path.lexists(content / "tests/link.ts"), "no symlink is written")
        self.assertEqual(list((content / "tests/node_modules").iterdir()), [], "an empty mount point only")
        self.assertFalse((content / "tests/node_modules/ms").exists())
        local_env = (app / "env/local.env").read_text()
        for line in ("TEST_SESSION=on", f"E2E_TESTS_DIRECTORY={content}/tests", f"TEST_APP_SOURCE={content}/public"):
            self.assertIn(line + "\n", local_env)
            self.assertIn(line + "\n", (app / "env/test.env").read_text())
        test_env = (app / "env/test.env").read_text()
        for line in ("TEST_HOST_ROUTE=off", "CRON_SCHEDULE=off", "PLAYWRIGHT_MEMORY=1024m", "PHP_MEMORY=512m"):
            self.assertIn(line + "\n", test_env)
        self.assertEqual([call[0] for call in calls], ["build", "test-init", "test-prepare", "e2e-install"])
        self.assertEqual(calls[0][3], [], "the images are built before anything of the caller's is written")
        self.config("TEST_TRUSTED_REFS_ALPHA=evil\n")
        self.assertEqual(self.ts.trusted_commit(self.project, "evil"), (evil, "evil"), "the operator's allowlist")

    def test_stage_and_live_run_only_a_trusted_commit_and_never_a_patch(self):
        master = git(self.project, "rev-parse", "origin/master")
        self.push_branch("feature", {"app/tests/playwright/x.spec.ts": "// x\n"})
        for target in ("stage", "live"):
            with self.subTest(target=target):
                self.refused(400, self.ts.start_run, {"target": target, "project": "alpha", "env_ref": "feature"}, "t", True, {})
                answer = self.refused(400, self.ts.start_run, {"target": target, "project": "alpha", "patch": fixture.FIX_PATCH},
                                      "t", True, {})
                self.assertIn("target test only", answer["error"])
                self.refused(400, self.ts.start_run, {"target": target, "project": "alpha", "path": "/",
                                                      "patch": fixture.FIX_PATCH}, "t", True, {}, kind="snapshot")
        run, _ = self.ts.start_run({"target": "stage", "project": "alpha"}, "t", True, {})
        self.assertEqual((run["env_commit"], run["env_ref"], run["tests_commit"]), (master, "master", master))
        self.assertFalse(list(self.ts.RUNS.glob("*/patch.diff")))

    def test_an_external_clone_keeps_its_dependency_install(self):
        master = git(self.project, "rev-parse", "origin/master")
        data = {"id": "run-00000000000e", "project": "alpha", "env_commit": master}
        self.ts.write_json(self.ts.run_dir(data["id"]) / "run.json", data)
        log = self.ts.run_dir(data["id"]) / "log"
        app = self.ts.external_app(data, log)
        (app / ".generated/e2e").mkdir(parents=True)
        (app / ".generated/e2e/.setup-e2e-install").write_text("digest\n")
        app = self.ts.external_app(data, log)
        self.assertEqual((app / ".generated/e2e/.setup-e2e-install").read_text(), "digest\n", "make e2e-install's cache stays")
        self.assertIn("PLAYWRIGHT_MEMORY=1024m\n", (app / "env/local.env").read_text())


class TagRuleTest(Base):
    """7: a tag is a word of Playwright's grep text."""

    @staticmethod
    def matches(grep, invert, title):
        """Playwright tests grep against "<project> <file> <titles> <tags>"; Python's lookarounds agree with JavaScript's."""
        return (not grep or re.search(grep, title) is not None) and (not invert or re.search(invert, title) is None)

    def test_each_target_runs_only_its_tags_and_the_caller_only_narrows(self):
        titles = {"plain": "chromium home.spec.ts home page", "stage": "chromium t.spec.ts reads @stage",
                  "write": "chromium t.spec.ts writes @stage-write", "live": "chromium t.spec.ts reads @live",
                  "deploy": "chromium t.spec.ts check @live @deploy", "both": "chromium t.spec.ts both @test @stage",
                  "stage+write": "chromium t.spec.ts deletes @stage @stage-write",
                  "live+write": "chromium t.spec.ts Live block @live wipes @stage-write",
                  "email": "chromium t.spec.ts registers jonas@live.com", "path": "chromium @live/x.spec.ts reads",
                  "dotted": "chromium t.spec.ts reads @stage.old"}
        cases = {("test", ()): {"plain", "both", "email", "path", "dotted"}, ("stage", ()): {"stage", "both"},
                 ("stage", ("stage_write_safe",)): {"stage", "write", "both", "stage+write", "live+write"},
                 ("live", ()): {"live", "deploy"}, ("live", ("deploy_check",)): {"deploy"}}
        for (target, flags), expected in cases.items():
            grep, invert = self.ts.selection(target, {flag: True for flag in flags}, manage=True)
            with self.subTest(target=target, flags=flags):
                self.assertEqual({key for key, title in titles.items() if self.matches(grep, invert, title)}, expected)
        grep, invert = self.ts.selection("live", {"grep": "reads|check", "grep_invert": "@deploy"}, manage=True)
        self.assertEqual({key for key, title in titles.items() if self.matches(grep, invert, title)}, {"live"})
        grep, _ = self.ts.selection("stage", {"grep": ".*"}, manage=True)
        self.assertFalse(self.matches(grep, None, titles["live"]), "a caller's .* does not widen stage to @live")
        self.refused(403, self.ts.selection, "stage", {"stage_write_safe": True}, manage=False)

    def test_a_pattern_that_could_leave_its_group_is_refused(self):
        for pattern in ("x)|(.*", "[^]))|.*(?:(]", "[]))|(", "(", "a\nb", "x" * 1001, "(?P<", 5):
            with self.subTest(pattern=pattern):
                self.refused(400, self.ts.scan_pattern, pattern, "grep")
        for pattern in ("cart|checkout", r"\(literal\)", "[()]", "@smoke(?![\\w-])"):
            self.assertEqual(self.ts.scan_pattern(pattern, "grep"), pattern)

    def test_files_are_spec_paths_inside_the_tests(self):
        self.assertEqual(self.ts.check_files(["cart.spec.ts", "checkout/pay.spec.ts:12"]), ["cart.spec.ts", "checkout/pay.spec.ts:12"])
        for files in (["../x.spec.ts"], ["/etc/passwd"], ["-x"], "cart.spec.ts", ["a b"], ["x"] * 101):
            with self.subTest(files=files):
                self.refused(400, self.ts.check_files, files)


class PatchTest(Base):
    """3: a patch is judged by what git apply does with it, and its result again."""

    def setUp(self):
        super().setUp()
        self.directory = self.ts.run_dir("run-0000000000ff")
        self.directory.mkdir(parents=True)

    def specs(self):
        master = git(self.project, "rev-parse", "origin/master")
        return self.ts.spec_files(self.project, {}, master, master, self.root / "log")

    def test_every_poc_patch_is_refused(self):
        patches = {
            "no a/ b/ prefixes: -p1 makes public/y.ts y.ts": "--- tests/playwright/x.ts\n+++ public/y.ts\n@@ -0,0 +1 @@\n+b\n",
            "a symlink as 120755": "diff --git a/tests/playwright/l b/tests/playwright/l\nnew file mode 120755\n--- /dev/null\n"
                                   "+++ b/tests/playwright/l\n@@ -0,0 +1 @@\n+/etc\n\\ No newline at end of file\n",
            "a symlink as 120000": "diff --git a/tests/playwright/l b/tests/playwright/l\nnew file mode 120000\n",
            "the install marker as a symlink": "diff --git a/tests/playwright/node_modules/.setup-e2e-install "
                                               "b/tests/playwright/node_modules/.setup-e2e-install\nnew file mode 120644\n",
            "a space hides package.json": "diff --git a/tests/playwright/a b/package.json b/tests/playwright/a b/package.json\n"
                                          "new file mode 100644\n--- /dev/null\n+++ b/tests/playwright/a b/package.json\n"
                                          "@@ -0,0 +1 @@\n+{}\n",
            "a mode change of the Makefile": "diff --git a/Makefile b/Makefile\nold mode 100644\nnew mode 100755\n",
            "an empty new file outside": "diff --git a/env/x.env b/env/x.env\nnew file mode 100644\nindex 0000000..e69de29\n",
            "planted dependency code": "diff --git a/tests/playwright/node_modules/ms/index.js b/tests/playwright/node_modules/ms/index.js\n"
                                       "new file mode 100644\n--- /dev/null\n+++ b/tests/playwright/node_modules/ms/index.js\n"
                                       "@@ -0,0 +1 @@\n+planted()\n",
            "a submodule": "diff --git a/tests/playwright/sub b/tests/playwright/sub\nnew file mode 160000\nindex 0000000..1234567\n"
                           "--- /dev/null\n+++ b/tests/playwright/sub\n@@ -0,0 +1 @@\n+Subproject commit 1234567\n",
            "outside the roots": fixture.OUTSIDE_PATCH,
            "the lock file": fixture.FIX_PATCH.replace("fixme.spec.ts", "package-lock.json"),
            "a rename out": "diff --git a/tests/playwright/a.ts b/public/a.ts\nsimilarity index 100%\nrename from tests/playwright/a.ts\n"
                            "rename to public/a.ts\n",
            "binary": "diff --git a/tests/playwright/x.png b/tests/playwright/x.png\nGIT binary patch\nliteral 1\n",
            "not a diff": "not a diff\n",
            "dot-dot": fixture.FIX_PATCH.replace("tests/playwright/fixme.spec.ts", "tests/playwright/../../public/x.php"),
        }
        for label, text in patches.items():
            with self.subTest(patch=label):
                self.refused(400, self.ts.apply_patch, self.specs(), text, self.directory)

    def test_a_good_patch_changes_only_the_staging_copy(self):
        specs = self.specs()
        self.assertEqual(self.ts.patch_paths(fixture.FIX_PATCH, self.directory), ["tests/playwright/fixme.spec.ts"])
        patched = self.ts.apply_patch(specs, fixture.FIX_PATCH, self.directory)
        self.assertIn(b"'ready'", patched["tests/playwright"]["fixme.spec.ts"][0])
        self.assertIn(b"'broken'", specs["tests/playwright"]["fixme.spec.ts"][0], "the specs given are not changed")
        self.assertEqual(patched["tests/playwright"]["package.json"], specs["tests/playwright"]["package.json"])
        self.assertEqual(sorted(path.name for path in self.directory.iterdir()), [], "the staging copy is gone")
        self.assertIn("does not apply", self.refused(400, self.ts.apply_patch, patched, fixture.FIX_PATCH, self.directory)["error"])


class CleanupTest(Base):
    """4: a cleanup unlinks a symlink and never leaves the session tree."""

    def test_a_symlink_is_unlinked_and_nothing_outside_is_removed(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "keep").write_text("keep")
        app = self.ts.SESSIONS / "alpha/repo/app"
        app.mkdir(parents=True)
        (app / "public").symlink_to(outside)
        mounted = []
        with patch.object(self.ts, "playwright_image", lambda: mounted.append(True) or "image"):
            self.ts.remove_tree(app / "public")
            self.assertFalse(os.path.lexists(app / "public"), "the link itself is removed")
            with self.assertRaisesRegex(RuntimeError, "outside the test session"):
                self.ts.remove_tree(outside)
            shutil.rmtree(app)
            app.symlink_to(outside)  # a component of the path is a symlink
            with self.assertRaisesRegex(RuntimeError, "not a directory"):
                self.ts.remove_tree(app / "keep")
            with self.assertRaisesRegex(RuntimeError, "not a directory"):
                self.ts.inside_managed(app / "keep")
        self.assertEqual(mounted, [], "no root container ever saw a path outside")
        self.assertEqual((outside / "keep").read_text(), "keep")


class RootInterfaceTest(Base):
    """5: as root only the public commands, the request from bounded stdin, the storage states read by root."""

    def as_root(self, arguments, stdin=b""):
        launched = []

        def fake_run(command, **keywords):
            launched.append((command, keywords.get("input")))
            return subprocess.CompletedProcess(command, 0)
        output = io.StringIO()
        with patch.object(self.ts.os, "geteuid", lambda: 0), patch.object(self.ts, "as_account_options", lambda: {}), \
                patch.object(self.ts.subprocess, "run", fake_run), contextlib.redirect_stdout(output), \
                patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin))):
            code = self.ts.main(arguments)
        return code, json.loads(output.getvalue()) if output.getvalue() else None, launched

    def test_internal_commands_and_options_are_refused_as_root(self):
        for arguments in (["run-worker", "--id=run-000000000001"], ["create-worker", "--id=ts-0123456789ab"],
                          ["run", "--states", '{"x": "/etc/shadow"}'], ["run", "--request=/etc/shadow"],
                          ["snapshot", "--checked"], ["list", "--help"], ["show", "--i=ts-0123456789ab"]):
            with self.subTest(arguments=arguments):
                code, answer, launched = self.as_root(arguments)
                self.assertEqual((code, answer["status"], launched), (1, 400, []), answer)

    def test_values_stay_values_and_the_request_is_bounded(self):
        code, _, launched = self.as_root(["create", "--project=--help", "--env-ref=--manage", "--db=dataset:dev", "--by=t"])
        self.assertEqual(code, 0)
        self.assertEqual(launched[0][0][2:], ["create", "--project=--help", "--db=dataset:dev", "--env-ref=--manage", "--by=t"])
        code, answer, launched = self.as_root(["run", "--by=t"], b"{" + b" " * (self.ts.REQUEST_LIMIT + 1))
        self.assertEqual((answer["status"], launched), (413, []))

    def test_root_reads_the_storage_states_and_hands_their_text_over(self):
        state = self.root / "handoff/stage" / f"{UUID}.json"
        state.write_text('{"cookies": [], "origins": []}')
        request = {"target": "stage", "project": "alpha", "storage_states": {"customer": UUID}}
        code, _, launched = self.as_root(["run", "--by=t", "--manage"], json.dumps(request).encode())
        command, payload = launched[0]
        self.assertEqual(command[2:], ["run", "--by=t", "--manage", "--checked"])
        self.assertEqual(json.loads(payload), {"request": request, "states": {"customer": '{"cookies": [], "origins": []}'}})
        state.unlink()
        state.symlink_to(self.root / "etc")
        self.refused(409, self.ts.read_storage_states, request, "stage", reason="storage_state_missing")
        state.unlink()
        os.mkfifo(state)
        self.refused(409, self.ts.read_storage_states, request, "stage", reason="storage_state_missing")
        state.unlink()
        state.write_text("x" * (self.ts.STATE_LIMIT + 1))
        self.refused(409, self.ts.read_storage_states, request, "stage", reason="storage_state_missing")
        state.write_text("{}")
        old = time.time() - 3600
        os.utime(state, (old, old))
        self.refused(409, self.ts.read_storage_states, request, "stage", reason="storage_state_expired")
        for given in ({"Customer": UUID}, {"customer": "../../etc/passwd"}, ["customer"]):
            self.refused(400, self.ts.read_storage_states, {"storage_states": given}, "stage")

    def test_a_known_client_run_id_is_looked_up_before_its_storage_states(self):
        run, _ = self.ts.start_run({"target": "stage", "project": "alpha", "client_run_id": "k1"}, "t", True, {})
        request = {"target": "stage", "project": "alpha", "client_run_id": "k1", "storage_states": {"customer": UUID}}
        code, answer, launched = self.as_root(["run", "--by=t"], json.dumps(request).encode())
        self.assertEqual(json.loads(launched[0][1])["states"], {}, "an expired state of a known run is no 409")

    def test_the_account_alone_reads_no_storage_state(self):
        options = self.ts.parser(internal=True).parse_args(["run", "--by=t"])
        with patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(b'{"target": "stage", "storage_states": {"a": "b"}}'))), \
                contextlib.redirect_stdout(io.StringIO()):
            self.refused(403, self.ts.dispatch, options)


class SnapshotPathTest(Base):
    """8: a snapshot stays on its site."""

    def test_a_scheme_relative_path_is_refused_and_the_script_guards_the_origin(self):
        for path in ("//172.17.0.1:9102/", "//169.254.169.254/latest/meta-data", "/\\evil.example/", "http://x/", "x"):
            with self.subTest(path=path):
                self.assertIsNone(self.ts.PAGE_PATH.fullmatch(path))
        self.session()
        self.refused(400, self.ts.start_run, {"target": "test", "session": "ts-0123456789ab", "path": "//172.17.0.1:9102/"},
                     "t", True, {}, kind="snapshot")
        for guard in ("route.abort('blockedbyclient')", "wanted.origin !== base.origin", "new URL(page.url()).origin !== base.origin",
                      "serviceWorkers: 'block'"):
            self.assertIn(guard, self.ts.SNAPSHOT_SCRIPT)


class GateTest(Base):
    """9, 13: limits that agree, gates that fail closed and run before every run."""

    def test_the_session_estimate_is_the_sum_of_the_limits_it_writes(self):
        limits = self.ts.settings()
        text = self.ts.session_env(limits, self.root / "content")
        written = sum(int(value) for value in re.findall(r"^[A-Z]+_MEMORY=(\d+)m$", text, re.M))
        self.assertEqual(written + 512, self.ts.session_memory_mb(limits), "with setup's fixed 512 MB doctrine runner")
        self.assertEqual(self.ts.session_memory_mb(limits), 2208 + 512 + 1024, "the doctrine runner (512 MB) counts")
        self.assertEqual(self.ts.session_budget(limits), (3744, 7.75))

    def test_gates_fail_closed_and_guard_every_run(self):
        (self.root / "proc/meminfo").unlink()
        self.refused(503, self.ts.create, self.options(), reason="gate_unknown")
        self.refused(503, self.ts.start_run, {"target": "stage", "project": "alpha"}, "t", True, {}, reason="gate_unknown")
        (self.root / "proc/meminfo").write_text("MemAvailable: 3000000 kB\n")
        answer = self.refused(409, self.ts.start_run, {"target": "stage", "project": "alpha"}, "t", True, {}, reason="memory")
        self.assertTrue(answer["capacity"])
        self.refused(409, self.ts.create, self.options(), reason="memory")
        self.assertFalse(list(self.ts.RUNS.glob("run-*")), "nothing recorded for a refused run")
        (self.root / "proc/meminfo").write_text("MemAvailable: 9000000 kB\n")
        real = os.statvfs
        small = os.statvfs_result((4096, 4096, 1000, 10, 10, 1000, 10, 10, 0, 255))
        with patch.object(self.ts.os, "statvfs", lambda path: small if str(path) == str(self.root / "dockerroot") else real(path)):
            self.refused(409, self.ts.start_run, {"target": "stage", "project": "alpha"}, "t", True, {}, reason="disk")
        (self.root / "fakebin/root.txt").write_text("")
        if True:
            self.refused(503, self.ts.start_run, {"target": "stage", "project": "alpha"}, "t", True, {}, reason="gate_unknown")

    def test_finished_runs_stay_within_their_budget(self):
        for index in range(3):
            identifier = f"run-00000000000{index}"
            self.ts.write_json(self.ts.run_dir(identifier) / "run.json", {"id": identifier, "status": "passed",
                                                                           "finished_at": int(time.time()) - 90000 + index})
            (self.ts.run_dir(identifier) / "report.json").write_bytes(b"x" * 1024 * 1024)
        removed = self.ts.prune_runs(int(2.5 * 1024 * 1024))
        self.assertEqual(removed, ["removed run-000000000000 (TEST_RUNS_TOTAL_MB)"])
        self.ts.write_json(self.ts.run_dir("run-0000000000a1") / "run.json", {"id": "run-0000000000a1", "status": "passed",
                                                                               "finished_at": int(time.time()) - 60})
        (self.ts.run_dir("run-0000000000a1") / "report.json").write_bytes(b"x" * 3 * 1024 * 1024)
        self.ts.prune_runs(1024 * 1024)
        self.assertTrue(self.ts.run_dir("run-0000000000a1").exists(), "a run finished a minute ago stays readable (24 h)")

    def test_a_run_over_its_disk_quota_is_stopped(self):
        quota = self.root / "results"
        quota.mkdir()
        log = self.root / "log"
        with patch.object(self.ts, "QUOTA_EVERY", 1):
            code = self.ts.run_logged(log, ["sh", "-c", f"head -c 3000000 /dev/zero > {quota}/big; sleep 60"], timeout=120,
                                      quota=(quota, 1024 * 1024))
        self.assertEqual(code, 125)


class CancelTest(Base):
    """10: cancel wins over a worker that has not started, never overwrites an outcome, stops everything."""

    def test_a_cancel_before_the_worker_starts_wins(self):
        self.session()
        run, _ = self.ts.start_run({"target": "test", "session": "ts-0123456789ab", "client_run_id": "asana-17"}, "t", True, {})
        with patch.object(self.ts.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", "")):
            first = self.ts.public_run(self.ts.cancel_run(run["id"], "cancelled by t", "t"))
        self.assertEqual((first["status"], first["executed"], first["cancelled_by"]), ("cancelled", False, "t"))
        with patch.object(self.ts, "make", lambda *a, **k: self.fail("the suite must not run")):
            self.ts.run_worker(run["id"])
        self.assertEqual(self.ts.find_run(run["id"])["status"], "cancelled")
        self.assertIsNone(self.ts.find_session("ts-0123456789ab").get("active_run"), "the session takes the next run")
        again = self.ts.public_run(self.ts.cancel_run(run["id"], "cancelled by t", "t"))
        self.assertEqual(again, first)
        same, created = self.ts.start_run({"target": "test", "session": "ts-0123456789ab", "client_run_id": "asana-17"}, "t", True, {})
        self.assertEqual((same["id"], created), (run["id"], False), "the executor's retry gets the same run")

    def test_a_finished_run_keeps_its_outcome(self):
        self.session()
        run, _ = self.ts.start_run({"target": "test", "session": "ts-0123456789ab"}, "t", True, {})
        self.ts.update_run(run["id"], status="passed", executed=True)
        self.assertEqual(self.ts.cancel_run(run["id"], "x")["status"], "passed")
        self.assertTrue(self.ts.public_run(self.ts.find_run(run["id"]))["executed"])

    def test_a_timeout_and_a_cancel_stop_the_whole_command_and_its_container(self):
        log = self.root / "log"
        pid_file = self.root / "child.pid"
        identifier = "run-0000000000aa"
        self.ts.write_json(self.ts.run_dir(identifier) / "run.json", {"id": identifier, "status": "running"})
        (self.root / "fakebin/containers.txt").write_text(identifier + "\n")
        self.ts.PROC = Path("/proc")  # this process's children, as they are (in CI it may be PID 1: no reaper)
        command = ["sh", "-c", f"trap '' TERM; sleep 60 & echo $! > {pid_file}; wait"]
        self.assertEqual(self.ts.run_logged(log, command, timeout=2, run=identifier), 124)
        child = int(pid_file.read_text())
        time.sleep(0.2)
        self.assertFalse(self.ts.alive(child), "SIGKILL after the grace reaches what ignores SIGTERM")
        self.assertIn(f"rm --force --volumes {identifier}", self.docker_calls())
        threading.Timer(1, lambda: self.ts.update_run(identifier, status="cancelled")).start()
        self.assertEqual(self.ts.run_logged(log, ["sleep", "60"], timeout=120, run=identifier), 130)

    def test_reconcile_removes_containers_without_an_active_run_and_ends_crashed_snapshots(self):
        moment = int(time.time())
        for identifier, data in (("run-00000000000a", {"status": "failed", "finished_at": moment}),
                                 ("run-00000000000c", {"status": "running", "worker": self.ts.identity(os.getpid()),
                                                       "created_at": moment, "started_at": moment}),
                                 ("snap-00000000000d", {"status": "running", "kind": "snapshot", "worker": 999999999,
                                                        "created_at": moment - 600, "target": "test", "project": "alpha",
                                                        "session": "ts-0123456789ab"})):
            self.ts.write_json(self.ts.run_dir(identifier) / "run.json", {"id": identifier, "project": "alpha", **data})
        self.ts.write_json(self.ts.RUNS / "active/ts-0123456789ab", {"run": "snap-00000000000d"})
        (self.root / "fakebin/ps.txt").write_text("run-00000000000a\t\t\nrun-00000000000b\t\t\nrun-00000000000c\t\t\n")
        (self.root / "fakebin/containers.txt").write_text("run-00000000000a\nrun-00000000000b\nrun-00000000000c\n")
        done = self.ts.reconcile()["reconciled"]
        calls = self.docker_calls()
        self.assertIn("rm --force --volumes run-00000000000a", calls)
        self.assertIn("rm --force --volumes run-00000000000b", calls, "a container whose run is gone")
        self.assertNotIn("rm --force --volumes run-00000000000c", calls, "a running run keeps its container")
        self.assertEqual(self.ts.find_run("snap-00000000000d")["status"], "error")
        self.assertFalse((self.ts.RUNS / "active/ts-0123456789ab").exists(), "its slot is free again")
        self.assertIn("ended snap-00000000000d (crashed)", done)


class SessionLifecycleTest(Base):
    """11: expiry in reconcile itself, a failed teardown, replacing a failed session, destroy while creating."""

    def test_reconcile_tears_an_expired_session_down_itself(self):
        self.session(expires_at=int(time.time()) - 1)
        done = self.ts.reconcile()["reconciled"]
        self.assertIn("expired ts-0123456789ab", done)
        self.assertEqual(self.detached, [], "no worker the timer's unit would kill")
        self.assertEqual(self.ts.find_session("ts-0123456789ab")["status"], "expired")
        self.assertIn("compose -p alpha-test down --volumes --remove-orphans --timeout 20", self.docker_calls(),
                      "the stored compose project, though the clone is gone")

    def test_a_failed_compose_down_is_tried_again(self):
        self.session(expires_at=int(time.time()) - 1)
        (self.root / "fakebin/down.exit").write_text("1")
        self.ts.reconcile()
        data = self.ts.find_session("ts-0123456789ab")
        self.assertEqual((data["status"], data["end_reason"]), ("destroying", "expired"))
        self.assertIn("teardown is not complete", data["cleanup"])
        (self.root / "fakebin/down.exit").write_text("0")
        self.ts.reconcile()
        self.assertEqual(self.ts.find_session("ts-0123456789ab")["status"], "expired")

    def test_a_failed_session_is_replaced_and_a_live_one_is_no_capacity_refusal(self):
        self.session(status="failed")
        made = self.ts.create(self.options())[0]
        self.assertNotEqual(made["id"], "ts-0123456789ab")
        answer = self.refused(409, self.ts.create, self.options(), reason="session_exists")
        self.assertNotIn("capacity", answer)

    def test_destroy_while_creating_is_not_undone_by_the_worker(self):
        made = self.ts.create(self.options())[0]
        self.ts.destroy(made["id"], "t")
        self.assertEqual(self.ts.find_session(made["id"])["status"], "destroying")
        with patch.object(self.ts, "make", lambda *a, **k: self.fail("nothing is built for an ended session")):
            self.ts.create_worker(made["id"])
        self.assertEqual(self.ts.find_session(made["id"])["status"], "destroying")

    def test_expiry_under_a_real_systemd_unit(self):
        """The timer's oneshot unit kills what it leaves behind when reconcile exits: the session must still end as expired."""
        probe = subprocess.run(["systemd-run", "--user", "--wait", "--quiet", "--collect", "true"], capture_output=True)
        if probe.returncode:
            self.skipTest("no systemd user manager here")
        self.session(expires_at=int(time.time()) - 1)
        environment = [f"SETUP_SERVER_ROOT={self.root / 'srv'}", f"SETUP_SERVER_CONFIG={self.root / 'etc'}",
                       f"SETUP_SERVER_PROC=/proc", f"SETUP_SERVER_HANDOFF={self.root / 'handoff'}",
                       f"SETUP_SERVER_PATH={self.root / 'fakebin'}:/usr/bin:/bin",
                       f"PATH={self.root / 'fakebin'}:/usr/bin:/bin"]
        result = subprocess.run(["systemd-run", "--user", "--wait", "--quiet", "--collect", "--pipe", "-p", "Type=oneshot",
                                 *[f"--setenv={item}" for item in environment], sys.executable, str(ROOT / "bin/test-session"),
                                 "reconcile"], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("expired ts-0123456789ab", result.stdout)
        time.sleep(1)
        self.assertEqual(self.ts.find_session("ts-0123456789ab")["status"], "expired")


class WorkerTest(Base):
    """E: run_worker, snapshot_worker and create_worker end to end (git real, make replaced)."""

    def report(self, results, passed=True):
        (results / "test-results/fixme").mkdir(parents=True)
        (results / "test-results/fixme/trace.zip").write_bytes(b"zip")
        failing = {"status": "failed", "error": {"message": "Expected 'broken' at https://user:secret@x.example/?token=abc"},
                   "attachments": [{"name": "trace", "path": "/e2e/data/test-results/fixme/trace.zip", "contentType": "application/zip"}]}
        test = {"status": "expected", "results": [{"status": "passed"}]} if passed else \
            {"status": "unexpected", "results": [failing]}
        report = {"stats": {"expected": 1 if passed else 0, "unexpected": 0 if passed else 1, "duration": 1000},
                  "suites": [{"title": "fixme.spec.ts", "file": "fixme.spec.ts", "specs": [
                      {"title": "status says ready", "file": "fixme.spec.ts", "tests": [test]}]}]}
        (results / "report.json").write_text(json.dumps(report))

    def test_a_failing_test_then_a_patch_that_fixes_it(self):
        session, _ = self.ready_session()
        made = []

        def fake_make(log, directory, target, *arguments, **keywords):
            made.append((target, arguments))
            if target == "e2e":
                spec = (self.ts.SESSIONS / "alpha/content/tests/fixme.spec.ts").read_text()
                self.report(directory / ".generated/test/data/playwright", passed="'ready'" in spec)
            return 0
        with patch.object(self.ts, "make", fake_make):
            run, _ = self.ts.start_run({"target": "test", "session": session["id"]}, "t", True, {})
            self.ts.run_worker(run["id"])
            failed = self.ts.public_run(self.ts.find_run(run["id"]))
            fixed, _ = self.ts.start_run({"target": "test", "session": session["id"], "patch": fixture.FIX_PATCH}, "t", True, {})
            self.ts.run_worker(fixed["id"])
            passed = self.ts.public_run(self.ts.find_run(fixed["id"]))
            again, _ = self.ts.start_run({"target": "test", "session": session["id"]}, "t", True, {})
            self.ts.run_worker(again["id"])
        self.assertEqual((failed["status"], failed["executed"], failed["summary"]["failed"]), ("failed", True, 1))
        self.assertEqual(failed["tests"][0]["error"], "Expected 'broken' at https://***@x.example/?token=***")
        self.assertEqual([item["name"] for item in failed["artifacts"]], ["001-trace.zip"])
        self.assertEqual((passed["status"], passed["executed"], passed["patch_files"]), ("passed", True, ["tests/playwright/fixme.spec.ts"]))
        self.assertEqual(self.ts.find_run(again["id"])["status"], "failed", "a later run without the patch has the specs back")
        e2e = [arguments for target, arguments in made if target == "e2e"][0]
        self.assertIn(f"container={run['id']}", e2e)
        self.assertTrue(any(item.startswith("grep_invert=") for item in e2e))

    def test_a_snapshot_returns_the_page_or_nothing_when_it_left_the_site(self):
        session, _ = self.ready_session()

        def fake_make(log, directory, target, *arguments, **keywords):
            command = next(item for item in arguments if item.startswith("cmd="))
            work = directory / ".generated/test/data/playwright" / command.split()[1].split("/")[3]
            left = "/elsewhere" in command
            (work / "snapshot.json").write_text(json.dumps(
                {"requested": "/", "url": "https://alpha.test.local/", "status": 200, "title": "Fixture", "aria": "- heading",
                 "dom": "<!doctype html>", **({"error": "the page went to http://169.254.169.254, not the site"} if left else {})}))
            (work / "screenshot.png").write_bytes(b"png")
            return 0
        with patch.object(self.ts, "make", fake_make):
            done, _ = self.ts.start_run({"target": "test", "session": session["id"], "path": "/"}, "t", True, {}, kind="snapshot")
            left, _ = self.ts.start_run({"target": "test", "session": session["id"], "path": "/elsewhere"}, "t", True, {},
                                        kind="snapshot")
        self.assertEqual((done["status"], done["snapshot"]["title"], done["artifacts"][0]["name"]), ("done", "Fixture", "screenshot.png"))
        self.assertEqual((left["status"], left.get("artifacts")), ("error", []))

    def test_a_create_that_fails_is_torn_down_and_can_be_replaced(self):
        made = self.ts.create(self.options())[0]
        with patch.object(self.ts, "make", lambda log, directory, target, *a, **k: 2 if target == "build" else 0):
            self.ts.create_worker(made["id"])
        data = self.ts.find_session(made["id"])
        self.assertEqual((data["status"], data["step"]), ("failed", "build"))
        self.assertIn(f"compose -p alpha-{made['id'][3:]}-test down --volumes --remove-orphans --timeout 20", self.docker_calls())
        self.assertNotEqual(self.ts.create(self.options())[0]["id"], made["id"])


class SessionImagesTest(Base):
    """The first real session: a failure after test-init says the session's domain, and a teardown removes the images
    the session built (named after it), never another stack's, and bounds BuildKit's cache."""

    def make_failing_at(self, step):
        def fake_make(log, directory, target, *arguments, **keywords):
            budget(directory, target)
            if target == "test-init":
                with open(directory / "env/test.env", "a") as handle:
                    handle.write("DOMAIN=alpha-0.test.local\n")
            return 2 if target == step else 0
        return fake_make

    def test_a_failed_session_says_its_domain_and_leaves_no_image_of_its_own(self):
        made = self.ts.create(self.options())[0]
        own = f"alpha-{made['id'][3:]}"
        images = self.root / "fakebin/images.txt"
        kept = ["php-fpm-base-local-8.4-3.4.0-2.8-0f1e2d:latest", "playwright-1.49.1:latest", "alpha-php-fpm-prod:latest",
                "alpha-0123456789ab-php-fpm-local:latest", "alpha-php-fpm-local:latest"]
        images.write_text("\n".join([f"{own}-php-fpm-local:latest", f"{own}-php-fpm-core-local:latest",
                                     f"{own}-php-fpm-test:latest", *kept]) + "\n")
        with patch.object(self.ts, "make", self.make_failing_at("test-prepare")):
            self.ts.create_worker(made["id"])
        data = self.ts.find_session(made["id"])
        self.assertEqual((data["status"], data["step"]), ("failed", "test-prepare"))
        self.assertEqual(data["domain"], "alpha-0.test.local", "known from test-init on, not only when ready")
        self.assertEqual(self.ts.public_session(data)["domain"], "alpha-0.test.local")
        self.assertEqual(images.read_text().split(), kept, "only the images named after this session")
        self.assertIn("builder prune --force --max-used-space 10gb", self.docker_calls())
        self.assertIn("removed the session's images", (self.ts.SESSIONS / "alpha/log").read_text())

    def test_a_teardown_is_not_done_while_an_image_of_the_session_is_left(self):
        self.config("TEST_BUILD_CACHE_GB=0\n")
        made = self.ts.create(self.options())[0]
        images = self.root / "fakebin/images.txt"
        images.write_text(f"alpha-{made['id'][3:]}-php-fpm-local:latest\n")
        (self.root / "fakebin/image-rm.fail").write_text("")
        with patch.object(self.ts, "make", self.make_failing_at("build")):
            self.ts.create_worker(made["id"])
        data = self.ts.find_session(made["id"])
        self.assertEqual(data["status"], "destroying", "tried again, never reported as done")
        self.assertIn("not complete", data["cleanup"])
        (self.root / "fakebin/image-rm.fail").unlink()
        self.ts.destroy_worker(made["id"])
        self.assertEqual(self.ts.find_session(made["id"])["status"], "failed")
        self.assertEqual(images.read_text().split(), [])
        self.assertFalse([call for call in self.docker_calls() if call.startswith("builder prune")], "0: the cache is left alone")

    def test_shop_ref_used_is_the_code_repositorys_current_branch_not_a_guess(self):
        code = self.root / "work/code"
        code.mkdir(parents=True)
        (code / "index.php").write_text("<?php 1;")
        git(code, "init", "-q", "-b", "main")
        git(code, "add", "-A")
        git(code, "commit", "-q", "-m", "c")
        git(code, "checkout", "-q", "-b", "production")
        subprocess.run(["git", "clone", "-q", str(code), str(self.project / "app/public")], check=True)
        self.assertEqual(self.ts.default_shop_ref(self.project, {"env_branch": "master"}), "production")
        head = git(self.project / "app/public", "rev-parse", "HEAD")
        git(self.project / "app/public", "checkout", "-q", "--detach")
        self.assertEqual(self.ts.default_shop_ref(self.project, {"env_branch": "master"}), head, "detached: its commit")
        shutil.rmtree(self.project / "app/public")
        self.assertEqual(self.ts.default_shop_ref(self.project, {"env_branch": "master"}), "master", "one repository")


class DigestTest(Base):
    """2, 14: the report and its attachments are the container's: read inside the results, bounded."""

    def test_attachments_are_copied_only_as_regular_files_inside_the_results(self):
        results = self.root / "results"
        (results / "test-results/cart").mkdir(parents=True)
        secret = self.root / "prod.env"
        secret.write_text("DATABASE_PASSWORD=secret\n")
        (results / "test-results/cart/trace.zip").write_bytes(b"zip")
        (results / "test-results/cart/link.zip").symlink_to(secret)
        (results / "test-results/sub").symlink_to(self.root)
        os.mkfifo(results / "test-results/cart/fifo")
        paths = ["/e2e/data/test-results/cart/trace.zip", "/e2e/data/../prod.env", "/e2e/data/test-results/cart/link.zip",
                 "/e2e/data/test-results/sub/prod.env", "/e2e/data/test-results/cart/fifo", "/e2e/data/test-results/../../prod.env",
                 "/etc/passwd", 5]
        failed = {"status": "failed", "error": {"message": "x"},
                  "attachments": [{"name": "a", "path": path, "contentType": "application/zip"} for path in paths]}
        report = {"stats": {"unexpected": 1}, "suites": [{"title": "cart.spec.ts", "specs": [
            {"title": "pays", "tests": [{"status": "unexpected", "results": [failed]}]}]}]}
        summary, tests, artifacts = self.ts.digest(report, results, self.root / "artifacts", 1)
        self.assertEqual([item["name"] for item in artifacts], ["001-trace.zip"])
        self.assertEqual(sorted(path.name for path in (self.root / "artifacts").iterdir()), ["001-trace.zip"])

    def test_counts_and_sizes_are_bounded(self):
        results = self.root / "results"
        results.mkdir()
        (results / "a.txt").write_bytes(b"x")
        many = {"status": "failed", "attachments": [{"path": "/e2e/data/a.txt"}] * 300}
        report = {"suites": [{"title": "f", "specs": [{"title": f"t{index}", "tests": [{"status": "unexpected", "results": [many]}]}
                                                      for index in range(600)]}]}
        _, tests, artifacts = self.ts.digest(report, results, self.root / "artifacts", 50)
        self.assertEqual((len(tests), len(artifacts)), (self.ts.TESTS_LIMIT, self.ts.ARTIFACTS_LIMIT))
        (results / "report.json").write_text("{}" + " " * 100)
        with patch.object(self.ts, "REPORT_LIMIT", 50):
            self.assertIsNone(self.ts.read_inside(results, "report.json", self.ts.REPORT_LIMIT))
        os.unlink(results / "report.json")
        (results / "report.json").symlink_to(self.root / "etc")
        directory = self.root / "run"
        directory.mkdir()
        self.assertEqual(self.ts.outcome_of(0, results, directory, self.ts.settings())["status"], "error")

    def test_an_outcome_says_why_nothing_ran(self):
        directory = self.root / "run"
        directory.mkdir()
        results = self.root / "results"
        results.mkdir()
        limits = self.ts.settings()
        (results / "report.json").write_text(json.dumps({"suites": [], "stats": {}, "errors": [
            {"message": "Error: No tests found.\nMake sure that arguments are regular expressions; https://bot:pw@git.example/x"}]}))
        outcome = self.ts.outcome_of(1, results, directory, limits)
        self.assertEqual((outcome["status"], outcome["executed"]), ("error", False))
        self.assertTrue(outcome["error"].startswith("Error: No tests found."))
        self.assertIn("https://***@git.example/x", outcome["errors"][0])
        (results / "report.json").write_text(json.dumps({"suites": [], "stats": {}}))
        outcome = self.ts.outcome_of(0, results, directory, limits)
        self.assertEqual((outcome["status"], outcome["executed"]), ("error", False))
        self.assertIn("no test ran", outcome["error"])
        (results / "report.json").write_text(json.dumps({"suites": [], "stats": {"expected": 3}}))
        outcome = self.ts.outcome_of(124, results, directory, limits)
        self.assertEqual((outcome["status"], outcome["executed"], outcome["error"]), ("error", True, "the run timed out"))
        (results / "report.json").write_text(json.dumps({"suites": [], "stats": {"expected": 3},
                                                         "errors": [{"message": "TypeScript: x.ts(3,1): error TS1005"}]}))
        self.assertEqual(self.ts.outcome_of(1, results, directory, limits)["status"], "error")

    def test_redaction(self):
        self.assertEqual(self.ts.redact("GET https://a:b@h/x?session_id=42&q=1 Cookie: sid=1; x=2"),
                         "GET https://***@h/x?session_id=***&q=1 Cookie: ***")


class BusyAndIdempotencyTest(Base):
    """15: the one-run slot does not depend on how many runs there are; runs() survives a removal."""

    def test_an_active_run_behind_many_newer_ones_still_blocks(self):
        first, _ = self.ts.start_run({"target": "stage", "project": "alpha"}, "t", True, {})
        for index in range(210):
            identifier = f"run-{index:012x}"
            self.ts.write_json(self.ts.run_dir(identifier) / "run.json", {"id": identifier, "status": "passed"})
        answer = self.refused(409, self.ts.start_run, {"target": "live", "project": "alpha"}, "t", True, {}, reason="run_active")
        self.assertEqual(answer["run"], first["id"])

    def test_runs_survives_a_run_removed_meanwhile(self):
        self.ts.write_json(self.ts.run_dir("run-000000000001") / "run.json", {"id": "run-000000000001", "status": "passed"})
        gone = self.ts.RUNS / "run-000000000009/run.json"
        real = Path.glob
        with patch.object(Path, "glob", lambda self, pattern: [*real(self, pattern), gone]):
            self.assertEqual([item["id"] for item in self.ts.runs()], ["run-000000000001"])


class TombstoneTest(Base):
    """19: a cancel by client_run_id that arrives before the run it names."""

    def test_a_cancel_before_the_run_exists_keeps_it_from_starting(self):
        first = self.ts.cancel_by_key("alpha", "registry-42", "token:b")
        self.assertEqual(first, {"cancelled": True, "run": None})
        answer = self.refused(409, self.ts.start_run, {"target": "stage", "project": "alpha", "client_run_id": "registry-42"},
                              "token:a", True, {}, reason="cancelled")
        self.assertNotIn("capacity", answer)
        self.assertEqual((self.detached, list(self.ts.RUNS.glob("run-*"))), ([], []), "nothing started")
        self.assertEqual(self.ts.cancel_by_key("alpha", "registry-42", "token:b"), first, "idempotent")
        self.refused(409, self.ts.create, self.options(client_run_id="registry-42"), reason="cancelled")

    def test_a_cancel_of_a_known_key_stops_its_run(self):
        run, _ = self.ts.start_run({"target": "stage", "project": "alpha", "client_run_id": "registry-43"}, "token:a", True, {})
        answer = self.ts.cancel_by_key("alpha", "registry-43", "token:b")
        self.assertEqual((answer["run"]["id"], answer["run"]["status"], answer["run"]["executed"]), (run["id"], "cancelled", False))
        self.assertEqual(self.ts.cancel_by_key("alpha", "registry-43", "token:b"), answer)
        self.refused(409, self.ts.start_run, {"target": "stage", "project": "alpha", "client_run_id": "registry-43"}, "token:c",
                     True, {}, reason="cancelled")
        self.refused(400, self.ts.cancel_by_key, "alpha", "bad key", "t")
        self.refused(404, self.ts.cancel_by_key, "nope", "k", "t")

    def test_a_tombstone_is_kept_two_days(self):
        self.ts.cancel_by_key("alpha", "registry-44", "t")
        path = self.ts.project_key_path("tombstones", "alpha", "registry-44")
        self.ts.reconcile()
        self.assertTrue(path.exists())
        self.ts.write_json(path, {**json.loads(path.read_text()), "at": int(time.time()) - 49 * 3600})
        self.ts.reconcile()
        self.assertFalse(path.exists())


class SessionApiTest(Base):
    def test_extend_never_passes_six_hours_from_creation(self):
        data = self.session(created_at=int(time.time()) - 5 * 3600, expires_at=int(time.time()) + 600)
        shown = self.ts.extend(data["id"], 120)
        self.assertEqual(shown["expires_at"], shown["max_expires_at"])
        self.refused(400, self.ts.extend, data["id"], 0)
        self.session(status="destroyed")
        self.refused(409, self.ts.extend, data["id"], 10)

    def test_a_session_is_refused_with_its_reason(self):
        self.assertIn("not supported yet", self.refused(400, self.ts.create, self.options(db="backup:prod"))["error"])
        self.refused(400, self.ts.create, self.options(db="dev"))
        self.refused(404, self.ts.create, self.options(project="nope"))
        self.refused(400, self.ts.create, self.options(ttl=361))
        self.config("TEST_SESSION_DISK_MB=999999999\n")
        self.refused(409, self.ts.create, self.options(), reason="disk")
        self.config("")
        lock = self.project / "app/.generated/release/stage/deploy.lock"
        lock.parent.mkdir(parents=True)
        lock.write_text("")
        details = lock.stat()
        (self.root / "proc/locks").write_text(
            f"1: FLOCK  ADVISORY  WRITE 1234 {os.major(details.st_dev):02x}:{os.minor(details.st_dev):02x}:{details.st_ino} 0 EOF\n")
        self.refused(409, self.ts.create, self.options(), reason="deploy_locked")
        (self.root / "proc/locks").write_text("")
        made = self.ts.create(self.options())[0]
        self.assertEqual((made["status"], made["project"], made["created_by"]), ("creating", "alpha", "token:agent"))
        self.assertEqual(self.detached, [("create-worker", f"--id={made['id']}")])

    def test_the_environment_address_comes_from_its_domain(self):
        master = git(self.project, "rev-parse", "origin/master")
        self.assertEqual(self.ts.environment_url(self.project, "stage", master), "https://stage.alpha.example")
        self.refused(404, self.ts.environment_url, self.project, "demo", master)
        self.config("TEST_URL_ALPHA_PROD=http://10.0.0.5:8080/\n")
        self.assertEqual(self.ts.environment_url(self.project, "prod", master), "http://10.0.0.5:8080")


def group(command):
    """A process group of its own (as a worker or a command is), recorded as the program records one."""
    process = subprocess.Popen(command, start_new_session=True)
    return process


class RoundThreeTest(Base):
    """Review round 2: each guard below fails its test when removed."""

    # 1
    def test_an_attachment_in_the_posix_root_form_cannot_name_a_host_file(self):
        results = self.root / "results"
        results.mkdir()
        (results / "ok.txt").write_text("ok")
        secret = self.root / "prod.env"
        secret.write_text("DATABASE_PASSWORD=secret\n")
        for path in ("//" + str(secret).lstrip("/"), "/" + str(secret).lstrip("/"), "a//b", "./ok.txt", "ok.txt/", "a/\0"):
            with self.subTest(path=path), self.assertRaises(OSError):
                self.ts.relative_parts(path)
        failed = {"status": "failed", "attachments": [{"path": "/e2e/data//" + str(secret).lstrip("/")},
                                                      {"path": "/e2e/data/" + str(secret)}, {"path": "/e2e/data/ok.txt"}]}
        report = {"suites": [{"title": "f", "specs": [{"title": "t", "tests": [{"status": "unexpected", "results": [failed]}]}]}]}
        _, _, artifacts = self.ts.digest(report, results, self.root / "artifacts", 1)
        self.assertEqual([item["name"] for item in artifacts], ["001-ok.txt"])

    # 2
    def test_a_tag_named_like_the_deploy_branch_does_not_choose_the_trusted_commit(self):
        master = git(self.project, "rev-parse", "refs/remotes/origin/master")
        evil = self.push_branch("evil", {"app/Makefile": "all:\n\ttouch /tmp/pwned\n"})
        git(self.project, "tag", "origin/master", evil)
        git(self.project, "tag", "master", evil)
        self.assertEqual(self.ts.trusted_commit(self.project), (master, "master"))
        self.assertEqual(self.ts.trusted_commit(self.project, "master"), (master, "master"))
        self.assertEqual(self.ts.resolve(self.project, "master"), master)

    # 5
    def test_a_branchs_blobs_are_counted_before_one_is_read(self):
        self.push_branch("big", {"app/tests/playwright/big.txt": "x" * (2 * 1024 * 1024)})
        commit = git(self.project, "rev-parse", "origin/big")
        self.config("TEST_SPECS_MB=1\n")
        with patch.object(self.ts, "stream_objects", lambda files: self.fail("nothing is read before the count")):
            answer = self.refused(400, self.ts.tree_files, self.project, {}, commit, "app/tests/playwright", self.root / "log",
                                  reason="content_too_large")
        self.assertIn("TEST_SPECS", answer["error"])
        self.config("TEST_CONTENT_MB=1\n")
        with patch.object(self.ts, "stream_objects", lambda files: self.fail("nothing is read before the count")):
            self.refused(400, self.ts.export_tree, self.project, {}, commit, "app", self.root / "srv/test-sessions/x", self.root / "log")

    def test_the_shop_export_streams_regular_files_and_says_what_it_left_out(self):
        self.push_branch("shop", {"app/public/index.php": "<?php echo 1;", "app/public/node_modules/a/i.js": "1",
                                  "app/public/sub/deep.txt": "d"}, links=[("app/public/link", "/etc/passwd")])
        commit = git(self.project, "rev-parse", "origin/shop")
        target = self.ts.SESSIONS / "alpha/content/public"
        dropped = self.ts.export_tree(self.project, {}, commit, "app/public", target, self.root / "log")
        self.assertEqual(dropped, {"symlink": 1})
        self.assertEqual(sorted(str(path.relative_to(target)) for path in target.rglob("*") if path.is_file()),
                         ["index.php", "node_modules/a/i.js", "sub/deep.txt"], "a shop keeps its node_modules")
        self.assertFalse(os.path.lexists(target / "link"))

    def test_a_log_is_bounded_and_a_quick_command_over_its_quota_is_caught(self):
        log = self.root / "log"
        self.config("TEST_LOG_MB=1\n")
        self.assertEqual(self.ts.run_logged(log, ["sh", "-c", "head -c 3000000 /dev/zero | tr '\\0' x"]), 0)
        self.assertLess(log.stat().st_size, 1024 * 1024 + 4096)
        quota = self.root / "results"
        quota.mkdir()
        code = self.ts.run_logged(self.root / "log2", ["sh", "-c", f"head -c 3000000 /dev/zero > {quota}/big"],
                                  quota=(quota, 1024 * 1024))
        self.assertEqual(code, 125, "the last look after it ended")

    def test_reconcile_destroys_sessions_below_the_disk_floor(self):
        self.session()
        with patch.object(self.ts, "free_disk_mb", lambda: 100):
            done = self.ts.reconcile()["reconciled"]
        self.assertTrue(any(item.startswith("destroyed ts-0123456789ab") for item in done), done)
        data = self.ts.find_session("ts-0123456789ab")
        self.assertEqual(data["status"], "destroyed")

    def test_the_workers_run_in_a_bounded_scope_on_systemd(self):
        real = self.ts.worker_scope.__wrapped__ if hasattr(self.ts.worker_scope, "__wrapped__") else None
        self.assertIsNone(real)
        module = load(self.root)
        with patch.object(module.os.path, "isdir", lambda path: path == "/run/systemd/system"), \
                patch.object(module.shutil, "which", lambda name: "/usr/bin/" + name), \
                patch.object(module.pwd, "getpwnam", lambda name: type("A", (), {"pw_uid": 4321, "pw_gid": 1234})()):
            scope = module.worker_scope("run")
            reading = module.worker_scope("list")
        self.assertEqual(reading, scope, "the read commands are bounded too")
        self.assertEqual(scope[:4], ["systemd-run", "--scope", "--quiet", "--collect"])
        self.assertIn("--slice=setup_server_tests.slice", scope)
        self.assertIn("MemoryMax=1536M", scope)
        self.assertIn("MemorySwapMax=0", scope)
        self.assertNotIn("--uid", " ".join(scope), "systemd-run --uid sets no supplementary groups on 249-257")
        self.assertEqual(scope[scope.index("setpriv"):], ["setpriv", "--reuid=4321", "--regid=1234", "--init-groups", "--"])

    # 6
    def test_a_cancel_keeps_the_slot_until_the_run_has_stopped(self):
        self.ts.PROC = Path("/proc")  # real processes: a killed one not yet reaped must count as stopped
        run, _ = self.ts.start_run({"target": "stage", "project": "alpha"}, "t", True, {})
        (self.root / "fakebin/containers.txt").write_text(run["id"] + "\n")
        worker = group(["sh", "-c", "trap '' TERM; sleep 60"])
        command = group(["sh", "-c", "trap '' TERM; sleep 60"])
        self.addCleanup(lambda: [process.kill() for process in (worker, command)])
        self.ts.update_run(run["id"], status="running", worker=self.ts.identity(worker.pid), suite_started=True)
        seen = []

        def cancel():
            seen.append(self.ts.cancel_run(run["id"], "cancelled by t", "t"))
        thread = threading.Thread(target=cancel)
        thread.start()
        time.sleep(1)
        self.ts.update_run(run["id"], command=self.ts.identity(command.pid))  # recorded after the cancel began
        self.assertEqual(self.ts.find_run(run["id"])["status"], "cancelling")
        self.refused(409, self.ts.start_run, {"target": "live", "project": "alpha"}, "t", True, {}, reason="run_active")
        self.assertEqual(json.loads((self.ts.RUNS / "active/external-alpha").read_text())["run"], run["id"])
        thread.join(60)
        for process in (worker, command):
            process.wait(5)
        final = seen[0]
        self.assertEqual((final["status"], final["executed"]), ("cancelled", True), "the suite had started")
        self.assertFalse((self.ts.RUNS / "active/external-alpha").exists())
        self.assertIn(f"rm --force --volumes {run['id']}", self.docker_calls())
        again, _ = self.ts.start_run({"target": "live", "project": "alpha"}, "t", True, {})
        self.assertNotEqual(again["id"], run["id"])

    def test_a_slot_is_freed_only_by_its_owner(self):
        self.ts.write_json(self.ts.RUNS / "active/external-alpha", {"run": "run-00000000000b"})
        self.ts.release_scope({"id": "run-00000000000a", "project": "alpha"})
        self.assertTrue((self.ts.RUNS / "active/external-alpha").exists())

    # 7
    def test_one_client_run_id_index_per_project_whoever_asks(self):
        first, created = self.ts.start_run({"target": "stage", "project": "alpha", "client_run_id": "k"}, "token:a", True, {})
        again, created_again = self.ts.start_run({"target": "stage", "project": "alpha", "client_run_id": "k"}, "token:b", True, {})
        self.assertEqual((again["id"], created, created_again), (first["id"], True, False))
        self.refused(409, self.ts.start_run, {"target": "live", "project": "alpha", "client_run_id": "k"}, "token:b", True, {},
                     reason="client_run_id_conflict")
        self.ts.cancel_by_key("alpha", "k", "token:c")
        self.refused(409, self.ts.start_run, {"target": "stage", "project": "alpha", "client_run_id": "k"}, "token:a", True, {},
                     reason="cancelled")

    def test_the_tombstone_is_checked_again_under_the_lock(self):
        def cancel_meanwhile(limits):
            self.ts.cancel_by_key("alpha", "late", "token:b")
        with patch.object(self.ts, "run_gates", cancel_meanwhile):
            self.refused(409, self.ts.start_run, {"target": "stage", "project": "alpha", "client_run_id": "late"}, "token:a",
                         True, {}, reason="cancelled")
        self.assertEqual(list(self.ts.RUNS.glob("run-*")), [])

    # 8
    def test_destroy_stops_a_creating_sessions_worker_and_command_once(self):
        self.ts.PROC = Path("/proc")
        worker = group(["sleep", "60"])
        command = group(["sleep", "60"])
        self.addCleanup(lambda: [process.kill() for process in (worker, command)])
        self.session(status="creating", worker=self.ts.identity(worker.pid), command=self.ts.identity(command.pid))
        self.ts.destroy("ts-0123456789ab", "t")
        for process in (worker, command):
            self.assertIsNotNone(process.wait(20), "stopped")
        self.assertEqual(self.ts.find_session("ts-0123456789ab")["status"], "destroying")
        cleaner = group(["sleep", "60"])
        self.addCleanup(cleaner.kill)
        self.ts.update_session("alpha", worker=self.ts.identity(cleaner.pid))
        self.ts.destroy("ts-0123456789ab", "t")
        self.assertEqual(len(self.detached), 1, "a destroy while one cleans starts no second cleaner")

    # 9
    def test_reconcile_keeps_a_live_sessions_local_project_and_downs_orphans(self):
        self.session()
        (self.root / "fakebin/ps.txt").write_text(
            f"a-1\talpha-local\t{self.ts.SESSIONS}/alpha/repo/app\nb-1\talpha-test\t{self.ts.SESSIONS}/alpha/repo/app\n"
            f"c-1\tgone-test\t{self.ts.SESSIONS}/gone/repo/app\nd-1\tprod\t/srv/elsewhere\n")
        self.ts.reconcile()
        downs = [line for line in self.docker_calls() if " down " in f" {line} "]
        self.assertEqual(downs, ["compose -p gone-test down --volumes --remove-orphans --timeout 10"])

    # 10
    def test_a_tree_that_cannot_be_removed_is_no_finished_teardown(self):
        self.session()
        (self.ts.SESSIONS / "alpha/repo").mkdir(parents=True)
        with patch.object(self.ts, "remove_tree", lambda path: None):
            self.assertFalse(self.ts.teardown(self.ts.find_session("ts-0123456789ab"), self.root / "log"))
        self.session(status="failed")
        with patch.object(self.ts, "remove_tree", lambda path: None):
            self.refused(503, self.ts.create, self.options(), reason="cleanup_pending")

    # 11
    def test_a_stage_snapshot_clones_nothing_and_runs_confined(self):
        image = self.root / "fakebin/image.txt"
        (self.root / "fakebin/docker").write_text(FAKE_DOCKER.replace('  "info --format")', '  "image ls") echo playwright-1:latest;;\n  "info --format")'))
        with patch.object(self.ts, "clone", lambda *a, **k: self.fail("a snapshot of a real site clones nothing")):
            shown, _ = self.ts.start_run({"target": "stage", "project": "alpha", "path": "/"}, "t", True, {}, kind="snapshot")
        command = next(line for line in self.docker_calls() if line.startswith("run --rm --name snap-"))
        for option in ("--read-only", "--cap-drop ALL", "--pids-limit 1024", "no-new-privileges:true", "--user"):
            self.assertIn(option, command)
        self.assertIn("BASE_URL=https://stage.alpha.example", command)
        for guard in ("['GET', 'HEAD'].includes(route.request().method())", "proxy.on('connect'", "request.url !== allowed",
                      "bypass: '<-loopback>'"):
            self.assertIn(guard, self.ts.SNAPSHOT_SCRIPT)
        self.assertFalse(image.exists())

    # 12
    def test_view_sees_only_numbers_of_a_run_with_storage_states(self):
        run, _ = self.ts.start_run({"target": "stage", "project": "alpha"}, "t", True, {"customer": "{}"})
        self.ts.update_run(run["id"], status="failed", tests=[{"title": "cookie=secret"}], errors=["Cookie: a"],
                           summary={"passed": 1, "failed": 1}, error="x", executed=True)
        data = self.ts.find_run(run["id"])
        shown = self.ts.public_run(data, full=False)
        self.assertEqual(sorted(shown), sorted(key for key in self.ts.VIEW_ONLY if key in shown))
        self.assertNotIn("tests", shown)
        self.assertIn("tests", self.ts.public_run(data, full=True))
        self.assertIn("tests", self.ts.public_run({**data, "roles": None}, full=False), "a run without states is shown")
        self.session(status="failed", error="clone of https://bot:token@git.example/x failed")
        self.assertEqual(self.ts.public_session(self.ts.find_session("ts-0123456789ab"))["error"],
                         "clone of https://***@git.example/x failed")

    # 13
    def test_a_two_repository_project_works(self):
        env = self.root / "work/beta-env"
        env.mkdir(parents=True)
        (env / "env").mkdir()
        (env / "Makefile").write_text("include $(SETUP_DIRECTORY)/docker/project.mk\n")
        (env / "env/common.env").write_text("PROJECT_NAME=beta\n")
        (env / "env/stage.env.example").write_text("DOMAIN=stage.beta.example\n")
        (env / "tests/playwright").mkdir(parents=True)
        (env / "tests/playwright/a.spec.ts").write_text(SPEC)
        (env / "config/playwright").mkdir(parents=True)
        (env / "config/playwright/c.cjs").write_text("1")
        code = self.root / "work/beta-code"
        code.mkdir()
        (code / "index.php").write_text("<?php echo 'beta';")
        (code / "link").symlink_to("/etc/passwd")
        for repository in (env, code):
            git(repository, "init", "-q", "-b", "master")
            git(repository, "add", "-A")
            git(repository, "commit", "-q", "-m", "init")
        origin = self.root / "origins/beta.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(env), str(origin)], check=True)
        root = self.ts.PROJECTS / "beta"
        root.mkdir()
        subprocess.run(["git", "clone", "-q", str(origin), str(root / "app")], check=True)
        subprocess.run(["git", "clone", "-q", str(code), str(root / "app/public")], check=True)
        self.assertEqual(self.ts.build_root("beta"), root)
        run, _ = self.ts.start_run({"target": "stage", "project": "beta"}, "t", True, {})
        self.assertEqual(run["url"], "https://stage.beta.example")
        made = self.ts.create(self.options(project="beta"))[0]
        with patch.object(self.ts, "make", lambda log, directory, target, *a, **k: budget(directory, target) or 0):
            self.ts.create_worker(made["id"])
        session = self.ts.find_session(made["id"])
        self.assertEqual(session["status"], "ready", (self.ts.SESSIONS / "beta/log").read_text())
        content = self.ts.SESSIONS / "beta/content"
        self.assertEqual((content / "public/index.php").read_text(), "<?php echo 'beta';")
        self.assertFalse(os.path.lexists(content / "public/link"), "a code clone is exported as regular files only")
        self.assertEqual((content / "tests/a.spec.ts").read_text(), SPEC)
        self.assertTrue((self.ts.SESSIONS / "beta/repo/app/Makefile").is_file())

    # 15
    def test_storage_states_have_their_own_bound(self):
        uuids = [UUID.replace("0f8f", f"{index:04x}") for index in range(5)]
        for uuid in uuids:
            (self.root / "handoff/stage" / f"{uuid}.json").write_text('{"x": "' + "a" * 900_000 + '"}')
        three = {"target": "stage", "project": "alpha", "storage_states": {f"r{index}": uuids[index] for index in range(3)}}
        self.assertEqual(len(self.ts.read_storage_states(three, "stage")), 3, "three of 0.9 MB fit")
        five = {**three, "storage_states": {f"r{index}": uuid for index, uuid in enumerate(uuids)}}
        self.refused(413, self.ts.read_storage_states, five, "stage")

    # 16
    def test_tombstones_are_bounded_per_project(self):
        self.config("TEST_TOMBSTONES=3\n")
        for index in range(3):
            self.ts.cancel_by_key("alpha", f"k{index}", "t")
        self.ts.cancel_by_key("alpha", "k0", "t")  # an existing one is no new tombstone
        self.refused(409, self.ts.cancel_by_key, "alpha", "k3", "t", reason="tombstones_full")

    # 17
    def test_deeply_nested_json_is_a_400(self):
        self.refused(400, self.ts.read_request, io.BytesIO(b"[" * 100000 + b"]" * 100000))

    # 18
    def test_a_session_has_a_cpu_budget(self):
        text = self.ts.session_env(self.ts.settings(), self.root / "content")
        total = sum(float(value) for value in re.findall(r"^[A-Z]+_CPUS=([0-9.]+)$", text, re.M))
        self.assertLessEqual(total, 7)
        self.assertIn("PHP_CPUS=1\n", text)

    # Guards that survived a mutation
    def test_git_runs_no_hook_fsmonitor_or_ext_transport(self):
        marker = self.root / "ran"
        hook = self.project / ".git/hooks/post-checkout"
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
        hook.chmod(0o755)
        monitor = self.root / "monitor.sh"
        monitor.write_text(f"#!/bin/sh\ntouch {marker}\n")
        monitor.chmod(0o755)
        git(self.project, "config", "core.fsmonitor", str(monitor))
        self.ts.git(self.project, "checkout", "-q", "-b", "other")
        self.ts.git(self.project, "status", check=False)
        self.ts.git(self.project, "fetch", f"ext::sh -c touch% {marker}", check=False)
        self.assertFalse(marker.exists())

    def test_a_run_needs_a_ready_session_of_its_project_and_no_env_ref(self):
        self.session()
        self.refused(400, self.ts.start_run, {"target": "test", "session": "ts-0123456789ab", "env_ref": "master"}, "t", True, {})
        self.build_root("gamma")
        self.refused(400, self.ts.start_run, {"target": "test", "project": "gamma", "session": "ts-0123456789ab"}, "t", True, {})
        run, _ = self.ts.start_run({"target": "test", "session": "ts-0123456789ab"}, "t", True, {})
        self.ts.update_session("alpha", status="destroying")
        with patch.object(self.ts, "make", lambda *a, **k: self.fail("no run in an ended session")):
            self.ts.run_worker(run["id"])
        self.assertEqual(self.ts.find_run(run["id"])["status"], "error")

    def test_a_runs_storage_states_are_removed_when_it_ends(self):
        run, _ = self.ts.start_run({"target": "stage", "project": "alpha"}, "t", True, {"customer": "{}"})
        self.assertTrue((self.ts.run_dir(run["id"]) / "auth/customer.json").is_file())
        self.ts.cancel_run(run["id"], "x")
        self.assertFalse((self.ts.run_dir(run["id"]) / "auth").exists())

    def test_the_patch_result_is_checked_again(self):
        directory = self.ts.run_dir("run-0000000000ef")
        directory.mkdir(parents=True)
        master = git(self.project, "rev-parse", "origin/master")
        specs = self.ts.spec_files(self.project, {}, master, master, self.root / "log")
        real = subprocess.run

        def planting_apply(command, **keywords):
            result = real(command, **keywords)
            if "apply" in command and "--numstat" not in command:
                (Path(keywords["cwd"]) / "tests/playwright/evil").symlink_to("/etc/passwd")
            return result
        with patch.object(self.ts.subprocess, "run", planting_apply):
            self.refused(400, self.ts.apply_patch, specs, fixture.FIX_PATCH, directory)

    def test_a_symlinked_directory_is_never_followed_when_reading_results(self):
        (self.root / "outside").mkdir()
        (self.root / "outside/snapshot.json").write_text("{}")
        results = self.root / "results"
        results.mkdir()
        (results / "ops").symlink_to(self.root / "outside")
        self.assertIsNone(self.ts.read_inside(results, "snapshot.json", 100, ("ops",)))

    def test_an_artifact_name_is_one_plain_name(self):
        run, _ = self.ts.start_run({"target": "stage", "project": "alpha"}, "t", True, {})
        for name in ("../run.json", "/etc/passwd", ".hidden"):
            options = self.ts.parser(internal=True).parse_args(["artifact", f"--id={run['id']}", f"--name={name}"])
            with self.subTest(name=name), contextlib.redirect_stdout(io.StringIO()):
                self.refused(400, self.ts.dispatch, options)


class RoundFourTest(Base):
    """Review round 3 (T1–T24): each test fails without its fix (the mutant it kills is in its name)."""

    def ready(self, name="alpha"):
        made = self.ts.create(self.options(project=name))[0]
        with patch.object(self.ts, "make", lambda log, directory, target, *a, **k: budget(directory, target) or 0):
            self.ts.create_worker(made["id"])
        session = self.ts.find_session(made["id"])
        self.assertEqual(session["status"], "ready", (self.ts.SESSIONS / name / "log").read_text())
        return session

    # T1
    def test_a_tag_stored_under_a_remote_branch_name_trusts_nothing(self):
        evil = self.push_branch("evil", {"app/Makefile": "all:\n\ttouch /tmp/pwned\n"})
        self.config("TEST_TRUSTED_REFS_ALPHA=release\n")
        git(self.project, "tag", "refs/remotes/origin/release", evil)  # stored as refs/tags/refs/remotes/origin/release
        self.assertIsNotNone(git(self.project, "rev-parse", "--verify", "refs/remotes/origin/release^{commit}"),
                             "Git's name lookup finds the tag")
        self.assertIsNone(self.ts.exact_commit(self.project, "refs/remotes/origin/release"))
        self.assertEqual(self.ts.trusted_commit(self.project, evil), (None, None))
        self.refused(400, self.ts.start_run, {"target": "stage", "project": "alpha", "env_ref": evil}, "t", True, {})

    # T2
    def test_the_engine_must_isolate_a_networks_gateway(self):
        (self.root / "fakebin/version.txt").write_text("27.5.1\n")
        self.refused(503, self.ts.create, self.options(), reason="gate_unknown")
        (self.root / "fakebin/version.txt").write_text("28.2.0\n")
        (self.root / "fakebin/network.json").write_text(json.dumps([{"Name": "net1", "Internal": True, "Options": {},
                                                                     "IPAM": {"Config": [{"Gateway": "10.0.0.1"}]}}]))
        with self.assertRaisesRegex(RuntimeError, "isolated gateway"):
            self.ts.check_networks("alpha-x-test")

    # T3
    def test_a_stop_that_is_not_confirmed_keeps_the_run_cancelling(self):
        run, _ = self.ts.start_run({"target": "stage", "project": "alpha"}, "t", True, {})
        (self.root / "fakebin/containers.txt").write_text(run["id"] + "\n")
        (self.root / "fakebin/rm.fail").write_text("")
        shown = self.ts.cancel_run(run["id"], "x", "t")
        self.assertEqual(shown["status"], "cancelling", "a failed docker rm is no confirmation")
        self.refused(409, self.ts.start_run, {"target": "live", "project": "alpha"}, "t", True, {}, reason="run_active")
        (self.root / "fakebin/rm.fail").unlink()
        self.ts.update_run(run["id"], cancel_asked_at=int(time.time()) - 600)
        self.ts.reconcile()
        self.assertEqual(self.ts.find_run(run["id"])["status"], "cancelled")
        self.assertFalse((self.ts.RUNS / "active/external-alpha").exists())

    def test_a_crashed_run_goes_through_cancelling_and_keeps_its_slot_until_stopped(self):
        run, _ = self.ts.start_run({"target": "stage", "project": "alpha"}, "t", True, {})
        self.ts.update_run(run["id"], status="running", worker=999999999, created_at=int(time.time()) - 600, suite_started=True)
        (self.root / "fakebin/containers.txt").write_text(run["id"] + "\n")
        (self.root / "fakebin/rm.fail").write_text("")
        self.ts.reconcile()
        self.assertEqual(self.ts.find_run(run["id"])["status"], "cancelling")
        self.assertTrue((self.ts.RUNS / "active/external-alpha").exists(), "the slot stays while its container is there")
        (self.root / "fakebin/rm.fail").unlink()
        self.ts.update_run(run["id"], cancel_asked_at=int(time.time()) - 600)
        self.ts.reconcile()
        final = self.ts.find_run(run["id"])
        self.assertEqual((final["status"], final["executed"]), ("error", True), "launched: tests may have run")

    def test_every_git_and_make_process_of_a_worker_is_recorded(self):
        seen = []
        self.ts.RECORDER["record"] = seen.append
        self.addCleanup(self.ts.RECORDER.clear)
        self.ts.run_logged(self.root / "log", ["true"])
        self.assertEqual([item is not None for item in seen], [True, False], "recorded while it runs, cleared after")

    # T4
    def test_a_teardown_removes_only_the_sessions_own_containers(self):
        (self.root / "fakebin/ps.txt").write_text(f"ts-ffffffffffff\t{self.root}/elsewhere\n")
        self.assertFalse(self.ts.compose_down("alpha-0123456789ab-test", self.root / "log", "ts-0123456789ab",
                                              self.ts.SESSIONS / "alpha"))
        self.assertFalse(any(" down " in f" {line} " for line in self.docker_calls()), "nothing of another stack is removed")
        (self.root / "fakebin/ps.txt").write_text("ts-0123456789ab\t/x\n")
        self.assertTrue(self.ts.compose_down("alpha-0123456789ab-test", self.root / "log", "ts-0123456789ab",
                                             self.ts.SESSIONS / "alpha"))
        session = self.ready()
        self.assertEqual(session["compose_project"], f"alpha-{session['id'][3:]}-test")
        local_env = (self.ts.SESSIONS / "alpha/repo/app/env/test.env").read_text()
        self.assertIn(f"PROJECT_NAME=alpha-{session['id'][3:]}\n", local_env)
        self.assertIn(f"TEST_SESSION_ID={session['id']}\n", local_env)

    # T5
    def test_a_patch_that_copies_is_refused_and_half_made_runs_are_removed(self):
        directory = self.ts.run_dir("run-0000000000c1")
        directory.mkdir(parents=True)
        copy = ("diff --git a/tests/playwright/fixme.spec.ts b/tests/playwright/copy.spec.ts\nsimilarity index 100%\n"
                "copy from tests/playwright/fixme.spec.ts\ncopy to tests/playwright/copy.spec.ts\n")
        self.refused(400, self.ts.patch_paths, copy, directory)
        rename_out = ("diff --git a/public/x.php b/tests/playwright/x.spec.ts\nsimilarity index 100%\n"
                      "rename from public/x.php\nrename to tests/playwright/x.spec.ts\n")
        self.refused(400, self.ts.patch_paths, rename_out, directory)
        old = time.time() - 3600
        os.utime(directory, (old, old))
        self.assertIn("removed half-made run-0000000000c1", self.ts.reconcile()["reconciled"])

    # T6
    def test_a_report_cannot_make_a_huge_record(self):
        results = self.root / "results"
        results.mkdir()
        long = "x" * 8000
        report = {"suites": [{"title": long, "file": long, "specs": [{"title": long, "tests": [{"status": "unexpected",
                  "results": [{"status": "failed", "error": {"message": long, "location": {"file": long, "line": 1}}}]}]}
                  for _ in range(500)]}]}
        (results / "report.json").write_text(json.dumps(report))
        directory = self.root / "run"
        directory.mkdir()
        outcome = self.ts.outcome_of(1, results, directory, self.ts.settings(), launched=True)
        self.assertLessEqual(len(json.dumps(outcome)), self.ts.RECORD_LIMIT)
        self.assertTrue(all(len(test["test_id"]) <= 800 and len(test["file"]) <= 300 for test in outcome["tests"]))
        self.ts.write_json(self.ts.run_dir("run-0000000000c2") / "run.json", {"id": "run-0000000000c2", "status": "failed",
                                                                               "tests": outcome["tests"], "artifacts": []})
        listed = self.ts.runs()
        self.assertNotIn("tests", listed[0], "a list answers summaries")

    # T7
    def test_on_a_full_disk_the_teardown_comes_before_the_record(self):
        self.session()
        torn = []
        with patch.object(self.ts, "free_disk_mb", lambda: 10), \
                patch.object(self.ts, "update_session", side_effect=OSError(28, "No space left on device")), \
                patch.object(self.ts, "teardown", lambda data, log: torn.append(data["id"]) or True):
            self.ts.reconcile()
        self.assertEqual(torn, ["ts-0123456789ab"], "torn down although its record could not be written")

    # T9
    def test_the_final_model_must_fit_the_budget_and_the_slot_comes_first(self):
        app = self.root / "app"
        budget(app, "test-init", memory=99999)
        with self.assertRaisesRegex(RuntimeError, "over the budget"):
            self.ts.check_budget(app, self.ts.settings())
        session = self.ready()
        nested = []

        def second_request(*arguments):
            nested.append(self.refused(409, self.ts.start_run, {"target": "test", "session": session["id"]}, "t", True, {},
                                       reason="run_active"))
            raise self.ts.Refusal(400, "stop here")
        with patch.object(self.ts, "apply_patch", second_request):
            self.refused(400, self.ts.start_run, {"target": "test", "session": session["id"], "patch": fixture.FIX_PATCH},
                         "t", True, {})
        self.assertEqual(len(nested), 1, "the slot was taken before the patch was prepared")
        self.assertEqual(list(self.ts.RUNS.glob("run-*")), [], "a refused preparation leaves nothing")

    # T11
    def test_a_shop_ref_is_fetched_into_the_session_never_into_the_build_root(self):
        code = self.root / "work/code"
        code.mkdir(parents=True)
        (code / "index.php").write_text("<?php 1;")
        git(code, "init", "-q", "-b", "master")
        git(code, "add", "-A")
        git(code, "commit", "-q", "-m", "c")
        origin = self.root / "origins/code.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(code), str(origin)], check=True)
        subprocess.run(["git", "clone", "-q", str(origin), str(self.project / "app/public")], check=True)
        git(code, "checkout", "-q", "-b", "feat")
        (code / "index.php").write_text("<?php 2;")
        git(code, "commit", "-q", "-am", "feat")
        git(code, "push", "-q", str(origin), "feat")
        feat = git(code, "rev-parse", "HEAD")
        content = self.ts.SESSIONS / "alpha/content"
        commit, _, source = self.ts.shop_code(self.project, self.project, "feat", content, self.root / "log", {}, None)
        self.assertEqual((commit, source), (feat, "branch"))
        self.assertEqual((content / "public/index.php").read_text(), "<?php 2;")
        missing = subprocess.run(["git", "-C", str(self.project / "app/public"), "cat-file", "-e", feat], capture_output=True)
        self.assertNotEqual(missing.returncode, 0, "the build root's objects are not touched")

    # T12
    def test_sessions_need_their_own_host_or_an_explicit_acceptance(self):
        (self.root / "etc/test-sessions.env").write_text("")
        self.refused(503, self.ts.create, self.options(), reason="host_unconfigured")
        (self.root / "etc/test-sessions.env").write_text("TEST_DOCKER_HOST=ssh://bad host\nTEST_SESSIONS_SHARE_PRODUCTION_HOST=accept-risk\n")
        answer = self.refused(503, self.ts.create, self.options(), reason="host_unconfigured")
        self.assertIn("not a valid", answer["error"])
        (self.root / "etc/test-sessions.env").write_text("TEST_DOCKER_HOST=ssh://tests@vm:2222\nTEST_DOCKER_ID=OTHER\n")
        self.refused(503, self.ts.create, self.options(), reason="host_unconfigured")
        self.assertEqual(self.ts.docker_host(), "ssh://tests@vm:2222")
        self.config("")
        self.assertEqual(self.ts.create(self.options())[0]["status"], "creating")

    # T14
    def test_session_history_and_repeats_follow_the_contract(self):
        first = self.ts.create(self.options(client_run_id="order-1"))[0]
        again, created = self.ts.create(self.options(client_run_id="order-1"))
        self.assertEqual((again["id"], created), (first["id"], False), "the same client_run_id: the session it made")
        self.ts.update_session("alpha", first["id"], status="destroying")
        self.refused(503, self.ts.create, self.options(), reason="cleanup_pending")
        self.ts.update_session("alpha", first["id"], status="destroyed", ended_at=int(time.time()))
        second = self.ts.create(self.options())[0]
        self.assertEqual(self.ts.find_session(first["id"])["status"], "destroyed", "the old id answers 24 h, not 404")
        self.assertNotEqual(second["id"], first["id"])
        self.ts.update_session("alpha", second["id"], status="failed", ended_at=int(time.time()), expires_at=1)
        self.ts.reconcile()
        self.assertEqual(self.ts.find_session(second["id"])["status"], "failed", "failed is final, never expired")

    def test_a_run_rechecks_its_session_under_the_lock(self):
        session = self.ready()

        def ended(limits):
            self.ts.update_session("alpha", session["id"], status="destroying")
        with patch.object(self.ts, "run_gates", ended):
            self.refused(409, self.ts.start_run, {"target": "test", "session": session["id"]}, "t", True, {},
                         reason="session_not_ready")
        self.assertEqual(list(self.ts.RUNS.glob("run-*")), [])

    # T15
    def test_a_cancel_of_existing_work_is_kept_whatever_the_bound(self):
        run, _ = self.ts.start_run({"target": "stage", "project": "alpha", "client_run_id": "live-1"}, "t", True, {})
        self.config("TEST_TOMBSTONES=1\n")
        self.ts.cancel_by_key("alpha", "unknown-1", "t")
        answer = self.ts.cancel_by_key("alpha", "live-1", "t")
        self.assertEqual(answer["run"]["id"], run["id"])
        self.refused(409, self.ts.cancel_by_key, "alpha", "unknown-2", "t", reason="tombstones_full")

    # T16
    def test_a_launched_suite_without_a_report_may_have_run(self):
        directory = self.root / "run"
        directory.mkdir()
        results = self.root / "results"
        results.mkdir()
        self.assertTrue(self.ts.outcome_of(1, results, directory, self.ts.settings(), launched=True)["executed"])
        self.assertFalse(self.ts.outcome_of(1, results, directory, self.ts.settings(), launched=False)["executed"])

    # T17
    def test_a_session_of_an_older_format_is_ended_before_a_run(self):
        self.session(format=None)
        self.refused(409, self.ts.start_run, {"target": "test", "session": "ts-0123456789ab"}, "t", True, {},
                     reason="session_ended")
        self.assertEqual(self.ts.find_session("ts-0123456789ab")["status"], "destroying")

    # T18
    def test_a_snapshot_without_an_image_creates_nothing_and_reads_no_state(self):
        self.refused(503, self.ts.start_run, {"target": "stage", "project": "alpha", "path": "/"}, "t", True, {"a": "{}"},
                     kind="snapshot", reason="gate_unknown")
        self.assertEqual(list(self.ts.RUNS.glob("snap-*")), [])
        self.assertIn("routeWebSocket(/.*/, (socket) => { result.blocked += 1; socket.close(); })", self.ts.SNAPSHOT_SCRIPT)

    # T19
    def test_the_work_log_keeps_no_query_string(self):
        log = self.root / "log"
        self.ts.run_logged(log, ["echo", "cmd=node /e2e/x.cjs /checkout?token=SECRET-1 https://a.example/p?k=SECRET-2"])
        text = log.read_text()
        self.assertNotIn("SECRET-1", text.splitlines()[0])
        self.assertNotIn("SECRET-2", text.splitlines()[0])
        self.assertIn("/checkout?…", text)
        shown = self.ts.public_run({"id": "snap-000000000001", "status": "done", "path": "/a?t=SECRET-3",
                                    "snapshot": {"requested": "/a?t=SECRET-3", "url": "https://h/a?t=SECRET-3"}})
        self.assertNotIn("SECRET-3", json.dumps(shown))

    # T20
    def test_a_branch_name_gives_the_remotes_branch_never_a_stale_copy_or_a_tag(self):
        old = self.push_branch("feature", {"app/tests/playwright/x.spec.ts": "// A\n"})
        git(self.work, "checkout", "-q", "feature")
        (self.work / "app/tests/playwright/x.spec.ts").write_text("// B\n")
        git(self.work, "commit", "-q", "-am", "B")
        git(self.work, "push", "-q", "origin", "feature")
        new = git(self.work, "rev-parse", "HEAD")
        git(self.work, "checkout", "-q", "master")
        git(self.project, "tag", "feature", old)
        origin = git(self.project, "config", "--get", "remote.origin.url")
        self.assertEqual(self.ts.fetch_ref(self.project, "feature", origin, self.root / "log"), (new, "branch"))
        commit, source = self.ts.fetch_ref(self.project, "feature", str(self.root / "gone.git"), self.root / "log")
        self.assertEqual(commit, old)
        self.assertIn("could not be asked", source, "the stale copy is said")

    # T21
    def test_the_session_says_what_it_used_and_keeps_its_reason(self):
        made = self.ts.create(self.options())[0]
        self.assertRegex(made["tests_commit"], r"^[0-9a-f]{40}$", "known before the 202 when the specs are the trusted commit's")
        self.ts.update_session("alpha", made["id"], status="destroyed", ended_at=int(time.time()))
        session = self.ready()
        self.assertEqual((session["shop_ref_used"], session["shop_commit"]), ("master", session["env_commit"]))
        self.ts.update_session("alpha", session["id"], status="destroying", error="free disk 10 MB is below TEST_DISK_FLOOR_MB")
        self.ts.destroy_worker(session["id"])
        ended = self.ts.find_session(session["id"])
        self.assertEqual(ended["status"], "destroyed")
        self.assertIn("TEST_DISK_FLOOR_MB", ended["error"], "the reason stays")
        shutil.rmtree(self.project / "app/env")
        self.refused(503, self.ts.docker_directory, self.project, reason="project_unconfigured")

    # T22: guards a mutation removed without a failing test
    def test_teardown_downs_the_sessions_local_project_too(self):
        session = self.ready()
        self.ts.destroy(session["id"], "t", wait=True)
        self.assertIn(f"compose -p alpha-{session['id'][3:]}-local down --volumes --remove-orphans --timeout 20",
                      self.docker_calls())

    def test_one_cleaner_at_a_time(self):
        self.session(status="destroying")
        lock = open(self.ts.SESSIONS / ".alpha.destroy.lock", "a")
        self.addCleanup(lock.close)
        import fcntl
        fcntl.flock(lock, fcntl.LOCK_EX)
        with patch.object(self.ts, "teardown", lambda *a: self.fail("a second cleaner ran")):
            self.ts.destroy_worker("ts-0123456789ab")

    def test_the_export_has_a_disk_gate(self):
        master = git(self.project, "rev-parse", "origin/master")

        def full(needed, what=None):
            raise self.ts.Refusal(409, "disk", "disk")
        with patch.object(self.ts, "disk_gate", full):
            self.refused(409, self.ts.export_tree, self.project, {}, master, "app", self.ts.SESSIONS / "alpha/content/public",
                         self.root / "log", reason="disk")

    def test_teardown_is_not_done_while_content_remains(self):
        self.session()
        (self.ts.SESSIONS / "alpha/content").mkdir(parents=True)
        real = self.ts.remove_tree
        with patch.object(self.ts, "remove_tree", lambda path: None if Path(path).name == "content" else real(path)):
            self.assertFalse(self.ts.teardown(self.ts.find_session("ts-0123456789ab"), self.root / "log"))

    def test_an_oversized_session_tree_is_destroyed(self):
        self.session()
        (self.ts.SESSIONS / "alpha/big").write_bytes(b"x" * 3 * 1024 * 1024)
        self.config("TEST_SESSION_TREE_MB=1\n")
        done = self.ts.reconcile()["reconciled"]
        self.assertTrue(any("TEST_SESSION_TREE_MB" in item for item in done), done)

    def test_reconcile_finishes_a_crashed_creating_session(self):
        self.session(status="creating", worker=999999999, created_at=int(time.time()) - 600)
        self.assertIn("failed ts-0123456789ab (crashed)", self.ts.reconcile()["reconciled"])
        self.assertEqual(self.ts.find_session("ts-0123456789ab")["status"], "failed")

    def test_a_stage_runs_data_is_removed_when_it_ends(self):
        run, _ = self.ts.start_run({"target": "stage", "project": "alpha"}, "t", True, {})
        (self.ts.run_dir(run["id"]) / "data/trace").mkdir(parents=True)
        self.ts.cancel_run(run["id"], "x")
        self.assertFalse((self.ts.run_dir(run["id"]) / "data").exists())

    def test_the_slot_is_not_freed_while_cancelling(self):
        run, _ = self.ts.start_run({"target": "stage", "project": "alpha"}, "t", True, {})
        self.ts.update_run(run["id"], status="cancelling")
        self.ts.finish_run(run["id"])
        self.assertTrue((self.ts.RUNS / "active/external-alpha").exists())

    # T23
    def test_one_bad_run_does_not_stop_reconcile(self):
        for index in range(2):
            self.ts.write_json(self.ts.run_dir(f"run-00000000000{index}") / "run.json",
                               {"id": f"run-00000000000{index}", "status": "passed", "finished_at": 1})
        real = self.ts.reconcile_run

        def failing(path, limits, containers):
            if path.parent.name.endswith("0"):
                raise OSError("broken")
            return real(path, limits, containers)
        with patch.object(self.ts, "reconcile_run", failing):
            done = self.ts.reconcile()["reconciled"]
        self.assertIn("run-000000000000: broken", done)
        self.assertIn("removed run-000000000001", done)

    # T24
    def test_the_run_contract(self):
        session = self.ready()
        first, created = self.ts.start_run({"target": "test", "session": session["id"], "client_run_id": "c-1",
                                            "unknown_field": 1, "grep": "home"}, "t", True, {})
        self.assertTrue(created)
        self.assertEqual(set(first) & {"id", "status", "target", "project", "session", "env_commit"},
                         {"id", "status", "target", "project", "session", "env_commit"})
        again, created_again = self.ts.start_run({"target": "test", "session": session["id"], "client_run_id": "c-1"},
                                                 "t", True, {})
        self.assertEqual((again["id"], created_again), (first["id"], False))
        self.ts.update_run(first["id"], status="cancelling")
        self.assertEqual(self.ts.cancel_run(first["id"], "x")["status"], "cancelled", "a cancelling run is finished, never 409")
        self.assertEqual(self.ts.cancel_run(first["id"], "x")["status"], "cancelled", "a final run comes back as it is")
        (self.ts.run_dir(first["id"]) / "artifacts").mkdir(exist_ok=True)
        (self.ts.run_dir(first["id"]) / "artifacts/001-a.png").write_bytes(b"png")
        self.ts.destroy(session["id"], "t", wait=True)
        self.assertEqual(self.ts.find_run(first["id"])["status"], "cancelled", "a run outlives its session")
        self.assertTrue((self.ts.run_dir(first["id"]) / "artifacts/001-a.png").exists())
        for status in ("queued", "running", "cancelling", "passed", "failed", "error", "cancelled", "done"):
            self.assertIn(status, self.ts.TERMINAL + self.ts.ACTIVE)


if __name__ == "__main__":
    unittest.main()
