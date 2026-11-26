"""bin/test-session: the tag rule, the caller's patterns and patches, the gates, the storage-state seam, the report
digest, idempotent runs, expiry and reconcile.

In-process over a temporary worker tree (every path of the module points there; nothing of an installed server is
read): no Docker, no make. The parts that need them (a session's clone, make test-prepare, Playwright) are what
tests/fixtures/test_session_project.py exercises on a machine with Docker (README: Test environments).
"""

import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests/fixtures"))
import test_session_project as fixture  # noqa: E402

UUID = "0f8fad5b-d9cb-469f-a165-70867728950e"


def load(root):
    loader = importlib.machinery.SourceFileLoader("test_session", str(ROOT / "bin/test-session"))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader("test_session", loader))
    with patch.dict(os.environ, {"SETUP_SERVER_ROOT": str(root / "srv"), "SETUP_SERVER_CONFIG": str(root / "etc"),
                                 "SETUP_SERVER_PROC": str(root / "proc"), "SETUP_SERVER_HANDOFF": str(root / "handoff")}):
        loader.exec_module(module)
    return module


def git(directory, *arguments):
    subprocess.run(["git", "-C", str(directory), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *arguments],
                   check=True, capture_output=True)


class TestSessionTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ("srv/projects", "etc", "proc", "handoff/prod"):
            (self.root / name).mkdir(parents=True)
        (self.root / "proc/meminfo").write_text("MemTotal: 16000000 kB\nMemAvailable: 5000000 kB\n")
        self.ts = load(self.root)
        self.assertTrue(str(self.ts.ROOT).startswith(str(self.root)))
        self.detached = []
        detach = patch.object(self.ts, "detach", lambda *arguments: self.detached.append(arguments) or 4242)
        detach.start()
        self.addCleanup(detach.stop)
        self.project = self.build_root("alpha")

    def build_root(self, name):
        app = self.ts.PROJECTS / name / "app"
        (app / "env").mkdir(parents=True)
        (app / "Makefile").write_text("include $(SETUP_DIRECTORY)/docker/project.mk\n")
        (app / "env/common.env").write_text(f"PROJECT_NAME={name}\n")
        (app / "env/stage.env.example").write_text("DOMAIN=stage.alpha.example www.stage.alpha.example\n")
        (app / "env/prod.env.example").write_text("DOMAIN=alpha.example\n")
        git(app.parent, "init", "-q")
        git(app.parent, "add", "-A")
        git(app.parent, "commit", "-q", "-m", "init")
        return app.parent

    def refused(self, status, call, *arguments, reason=None, **keywords):
        with self.assertRaises(self.ts.Refusal) as caught:
            call(*arguments, **keywords)
        self.assertEqual(caught.exception.answer["status"], status, caught.exception.answer)
        if reason:
            self.assertEqual(caught.exception.answer.get("reason"), reason)
        return caught.exception.answer

    def session(self, status="ready", **fields):
        directory = self.ts.SESSIONS / "alpha"
        moment = int(time.time())
        data = {"id": "ts-0123456789ab", "project": "alpha", "status": status, "db": "dataset:dev", "created_at": moment,
                "expires_at": moment + 7200, "domain": "alpha.test.local", **fields}
        self.ts.write_json(directory / "session.json", data)
        return data

    # --- The tag rule and the caller's narrowing ---
    @staticmethod
    def matches(grep, invert, title):
        """Playwright tests grep against "<project> <file> <titles> <tags>"; Python's lookaheads agree with JavaScript's."""
        return (not grep or re.search(grep, title) is not None) and (not invert or re.search(invert, title) is None)

    def test_each_target_runs_only_its_tags_and_the_caller_only_narrows(self):
        titles = {"plain": "chromium home.spec.ts home page", "stage": "chromium t.spec.ts reads @stage",
                  "write": "chromium t.spec.ts writes @stage-write", "live": "chromium t.spec.ts reads @live",
                  "deploy": "chromium t.spec.ts check @live @deploy", "both": "chromium t.spec.ts both @test @stage"}
        cases = {("test", ()): {"plain", "both"}, ("stage", ()): {"stage", "both"},
                 ("stage", ("stage_write_safe",)): {"stage", "write", "both"}, ("live", ()): {"live", "deploy"},
                 ("live", ("deploy_check",)): {"deploy"}}
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
        for pattern in ("x)|(.*", "[^]))|.*(?:(]", "[]))|(", "(", "a\nb", "x" * 1001, "(?P<"):
            with self.subTest(pattern=pattern):
                self.refused(400, self.ts.scan_pattern, pattern, "grep")
        for pattern in ("cart|checkout", r"\(literal\)", "[()]", "@smoke(?![\\w-])"):
            self.assertEqual(self.ts.scan_pattern(pattern, "grep"), pattern)

    def test_files_are_spec_paths_inside_the_tests(self):
        self.assertEqual(self.ts.check_files(["cart.spec.ts", "checkout/pay.spec.ts:12"]), ["cart.spec.ts", "checkout/pay.spec.ts:12"])
        for files in (["../x.spec.ts"], ["/etc/passwd"], ["-x"], "cart.spec.ts", ["a b"], ["x"] * 101):
            with self.subTest(files=files):
                self.refused(400, self.ts.check_files, files)

    # --- Patches ---
    def test_a_patch_may_change_only_the_specs_and_their_configuration(self):
        self.assertEqual(self.ts.patch_paths(fixture.FIX_PATCH), ["tests/playwright/fixme.spec.ts"])
        self.assertIn("outside tests/playwright/", self.refused(400, self.ts.patch_paths, fixture.OUTSIDE_PATCH)["error"])
        renamed = ("diff --git a/tests/playwright/a.ts b/public/a.ts\nsimilarity index 100%\n"
                   "rename from tests/playwright/a.ts\nrename to public/a.ts\n")
        self.refused(400, self.ts.patch_paths, renamed)
        for text in (fixture.FIX_PATCH.replace("fixme.spec.ts", "package.json"),
                     fixture.FIX_PATCH.replace("tests/playwright/fixme.spec.ts", "tests/playwright/../../public/x.php"),
                     "diff --git a/tests/playwright/x.png b/tests/playwright/x.png\nGIT binary patch\nliteral 1\n",
                     "diff --git a/tests/playwright/l b/tests/playwright/l\nnew file mode 120000\n", "not a diff\n"):
            with self.subTest(text=text[:60]):
                self.refused(400, self.ts.patch_paths, text)

    def test_a_patch_resets_and_applies_across_a_submodule(self):
        app = self.root / "work/app"
        (app / "config/playwright").mkdir(parents=True)
        (app / "config/playwright/playwright.config.cjs").write_text("module.exports = {};\n")
        git(app.parent, "init", "-q")
        git(app.parent, "add", "-A")
        git(app.parent, "commit", "-q", "-m", "env")
        tests = app / "tests/playwright"
        tests.mkdir(parents=True)
        (tests / "fixme.spec.ts").write_text(fixture.SPECS["fixme.spec.ts"])
        git(tests, "init", "-q")  # the specs as their own repository, as a submodule's work tree is
        git(tests, "add", "-A")
        git(tests, "commit", "-q", "-m", "specs")
        (tests / "node_modules").mkdir()
        patch_file = self.root / "fix.diff"
        patch_file.write_text(fixture.FIX_PATCH)
        self.ts.apply_patch(app, patch_file)
        self.assertIn("'ready'", (tests / "fixme.spec.ts").read_text())
        (tests / "stray.spec.ts").write_text("// left by a patch\n")
        (app / "config/playwright/playwright.config.cjs").write_text("changed\n")
        self.ts.reset_tests(app)
        self.assertIn("'broken'", (tests / "fixme.spec.ts").read_text())
        self.assertFalse((tests / "stray.spec.ts").exists())
        self.assertTrue((tests / "node_modules").is_dir(), "the installed dependencies stay")
        self.assertEqual((app / "config/playwright/playwright.config.cjs").read_text(), "module.exports = {};\n")
        (tests / "fixme.spec.ts").write_text("something else\n")
        self.assertIn("does not apply", self.refused(400, self.ts.apply_patch, app, patch_file, check_only=True)["error"])

    # --- Sessions: gates and lifetime ---
    def test_a_session_is_refused_with_its_reason(self):
        create = lambda **keywords: self.ts.create(type("Options", (), {"project": "alpha", "db": "dataset:dev", "env_ref": None,
                                                                         "shop_ref": None, "ttl": None, "by": "token:agent",
                                                                         **keywords})())
        with patch.object(self.ts, "require_setup", lambda: None):
            self.assertIn("not supported yet", self.refused(400, create, db="backup:prod")["error"])
            self.refused(400, create, db="dev")
            self.refused(404, create, project="nope")
            self.refused(400, create, ttl=361)
            (self.root / "etc/test-sessions.env").write_text("TEST_SESSION_MEMORY_MB=3500\n")
            answer = self.refused(409, create, reason="memory")
            self.assertEqual((answer["needed_mb"], answer["available_mb"]), (5548, 4882))
            (self.root / "etc/test-sessions.env").write_text("TEST_SESSION_DISK_MB=999999999\n")
            self.refused(409, create, reason="disk")
            (self.root / "etc/test-sessions.env").unlink()
            lock = self.project / "app/.generated/release/stage/deploy.lock"
            lock.parent.mkdir(parents=True)
            lock.write_text("")
            details = lock.stat()
            (self.root / "proc/locks").write_text(
                f"1: FLOCK  ADVISORY  WRITE 1234 {os.major(details.st_dev):02x}:{os.minor(details.st_dev):02x}:{details.st_ino} 0 EOF\n")
            self.refused(409, create, reason="deploy_locked")
            (self.root / "proc/locks").write_text("")
            made = create()
            self.assertEqual((made["status"], made["project"], made["created_by"]), ("creating", "alpha", "token:agent"))
            self.assertRegex(made["id"], r"^ts-[0-9a-f]{12}$")
            self.assertEqual(self.detached, [("create-worker", "--id", made["id"])])
            self.refused(409, create, reason="session_exists")

    def test_extend_never_passes_six_hours_from_creation(self):
        data = self.session(created_at=int(time.time()) - 5 * 3600, expires_at=int(time.time()) + 600)
        shown = self.ts.extend(data["id"], 120)
        self.assertEqual(shown["expires_at"], shown["max_expires_at"])
        self.refused(400, self.ts.extend, data["id"], 0)
        self.session(status="destroyed")
        self.refused(409, self.ts.extend, data["id"], 10)

    def test_reconcile_expires_sessions_and_ends_crashed_and_old_runs(self):
        self.session(expires_at=int(time.time()) - 1)
        self.ts.write_json(self.ts.run_dir("run-000000000001") / "run.json",
                           {"id": "run-000000000001", "kind": "run", "status": "running", "worker": 999999999,
                            "created_at": int(time.time()) - 600})
        self.ts.write_json(self.ts.run_dir("run-000000000002") / "run.json",
                           {"id": "run-000000000002", "kind": "run", "status": "passed", "finished_at": int(time.time()) - 8 * 86400})
        with patch.object(self.ts.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", "")):
            done = self.ts.reconcile()["reconciled"]
        self.assertIn("expired ts-0123456789ab", done)
        self.assertEqual(self.detached[-1], ("destroy-worker", "--id", "ts-0123456789ab", "--reason", "expired"))
        self.assertEqual(self.ts.find_session("ts-0123456789ab")["status"], "destroying")
        self.assertEqual(self.ts.find_run("run-000000000001")["status"], "error")
        self.assertFalse(self.ts.run_dir("run-000000000002").exists())

    # --- Runs ---
    def test_a_second_post_with_the_same_client_run_id_returns_the_first_run(self):
        first, created = self.ts.start_run({"target": "stage", "project": "alpha", "client_run_id": "ci-1"}, "token:a", True, {})
        self.assertTrue(created)
        self.assertEqual((first["url"], first["environment"], first["status"]), ("https://stage.alpha.example", "stage", "queued"))
        again, created = self.ts.start_run({"target": "stage", "project": "alpha", "client_run_id": "ci-1"}, "token:a", True, {})
        self.assertEqual((again["id"], created), (first["id"], False))
        answer = self.refused(409, self.ts.start_run, {"target": "live", "project": "alpha"}, "token:b", True, {}, reason="run_active")
        self.assertEqual(answer["run"], first["id"])
        self.assertEqual(len(self.detached), 1)
        (self.root / "etc/test-sessions.env").write_text("TEST_URL_ALPHA_PROD=http://10.0.0.5:8080/\n")
        self.ts.update_run(first["id"], status="passed")
        live, _ = self.ts.start_run({"target": "live", "project": "alpha", "patch": fixture.FIX_PATCH}, "token:b", True, {})
        self.assertEqual((live["url"], live["environment"]), ("http://10.0.0.5:8080", "prod"))
        self.assertEqual(live["patch_sha256"], __import__("hashlib").sha256(fixture.FIX_PATCH.encode()).hexdigest())
        self.assertNotIn("patch", live)
        for request, status in (({"target": "prod", "project": "alpha"}, 400), ({"target": "test", "project": "alpha"}, 400),
                                ({"target": "stage", "project": "alpha", "session": "ts-0123456789ab"}, 400),
                                ({"target": "stage", "project": "alpha", "patch": "x" * (1024 * 1024 + 1)}, 413),
                                ({"target": "stage", "project": "alpha", "client_run_id": "bad id"}, 400)):
            with self.subTest(request=str(request)[:60]):
                self.refused(status, self.ts.start_run, request, "token:c", True, {})

    def test_a_session_run_needs_a_ready_session(self):
        self.session(status="creating")
        self.refused(409, self.ts.start_run, {"target": "test", "session": "ts-0123456789ab"}, "t", True, {}, reason="session_not_ready")
        self.session()
        run, _ = self.ts.start_run({"target": "test", "session": "ts-0123456789ab"}, "t", True, {})
        self.assertEqual((run["project"], run["session"], run["url"]), ("alpha", "ts-0123456789ab", "https://alpha.test.local"))
        self.assertEqual(self.ts.find_session("ts-0123456789ab")["active_run"], run["id"])
        self.refused(400, self.ts.start_run, {"target": "test", "session": "ts-0123456789ab", "path": "/", "role": "admin"},
                     "t", True, {}, kind="snapshot")

    def test_storage_states_are_vaults_fresh_handoff_files(self):
        path = self.root / "handoff/prod" / f"{UUID}.json"
        path.write_text('{"cookies": [], "origins": []}')
        self.assertEqual(self.ts.storage_states({"storage_states": {"customer": UUID}}, "prod"), {"customer": str(path)})
        self.refused(409, self.ts.storage_states, {"storage_states": {"customer": UUID.replace("0f", "1f")}}, "prod",
                     reason="storage_state_missing")
        old = time.time() - 3600
        os.utime(path, (old, old))
        self.refused(409, self.ts.storage_states, {"storage_states": {"customer": UUID}}, "prod", reason="storage_state_expired")
        for given in ({"Customer": UUID}, {"customer": "../../etc/passwd"}, ["customer"]):
            self.refused(400, self.ts.storage_states, {"storage_states": given}, "prod")

    def test_the_report_becomes_a_summary_and_the_tests_that_did_not_pass(self):
        results = self.root / "results"
        (results / "test-results/cart").mkdir(parents=True)
        (results / "test-results/cart/trace.zip").write_bytes(b"zip")
        (results / "test-results/cart/big.webm").write_bytes(b"v" * (2 * 1024 * 1024))
        failed = {"status": "failed", "error": {"message": "\x1b[31mExpected\x1b[39m ready", "location": {"file": "/e2e/tests/cart.spec.ts", "line": 7}},
                  "attachments": [{"name": "trace", "path": "/e2e/data/test-results/cart/trace.zip", "contentType": "application/zip"},
                                  {"name": "video", "path": "/e2e/data/test-results/cart/big.webm", "contentType": "video/webm"},
                                  {"name": "outside", "path": "/etc/passwd"}],
                  "steps": [{"title": "open cart", "steps": [{"title": "click Pay", "error": {"message": "x"}}]}]}
        report = {"stats": {"expected": 4, "unexpected": 1, "flaky": 1, "skipped": 1, "duration": 12345},
                  "suites": [{"title": "cart.spec.ts", "file": "cart.spec.ts", "specs": [], "suites": [
                      {"title": "Cart", "file": "cart.spec.ts", "specs": [
                          {"title": "pays @stage", "file": "cart.spec.ts", "tests": [{"status": "unexpected", "projectName": "chromium",
                                                                                      "results": [failed, failed]}]},
                          {"title": "flakes", "file": "cart.spec.ts", "tests": [{"status": "flaky", "results": [{"status": "failed"}, {"status": "passed"}]}]},
                          {"title": "skips", "file": "cart.spec.ts", "tests": [{"status": "skipped", "results": []}]},
                          {"title": "passes", "file": "cart.spec.ts", "tests": [{"status": "expected", "results": [{"status": "passed"}]}]}]}]}]}
        summary, tests, artifacts = self.ts.digest(report, results, self.root / "artifacts", 1)
        self.assertEqual(summary, {"passed": 4, "failed": 1, "flaky": 1, "skipped": 1, "duration_s": 12.3})
        self.assertEqual([(test["test_id"], test["status"], test["attempts"]) for test in tests],
                         [("cart.spec.ts › Cart › pays @stage", "failed", 2), ("cart.spec.ts › Cart › flakes", "flaky", 2),
                          ("cart.spec.ts › Cart › skips", "skipped", 0)])
        self.assertEqual((tests[0]["error"], tests[0]["step"], tests[0]["file"], tests[0]["title"]),
                         ("Expected ready", "click Pay", "cart.spec.ts", "Cart › pays @stage"))
        self.assertEqual([item["name"] for item in artifacts], ["001-trace.zip", "002-trace.zip"], "the 2 MB video is over the 1 MB budget")
        self.assertEqual(tests[0]["artifacts"], ["001-trace.zip", "002-trace.zip"])
        self.assertEqual((self.root / "artifacts/001-trace.zip").read_bytes(), b"zip")

    def test_the_environment_address_comes_from_its_domain(self):
        self.assertEqual(self.ts.environment_url(self.project, "stage"), "https://stage.alpha.example")
        self.refused(404, self.ts.environment_url, self.project, "demo")


if __name__ == "__main__":
    unittest.main()
