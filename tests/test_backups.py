"""Nightly database backups of the worker: rotation, per-project isolation, --check and the units.

Nothing here touches a real database: `make` is a stub that writes the backup file it is asked for.
B level (opt-in, SETUP_SERVER_UBUNTU_IMAGE=ubuntu:22.04): install-backups for real inside an Ubuntu
container, systemd-analyze verify of its units, and the sudo path the "backup now" pipeline uses.
"""

from datetime import datetime, timedelta
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import subprocess
import unittest

from test_scripts import DOCKER_RUN, ROOT, ScriptTest, UBUNTU


def load_backup_projects():
    loader = importlib.machinery.SourceFileLoader("backup_projects", str(ROOT / "bin/backup-projects"))
    spec = importlib.util.spec_from_loader("backup_projects", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def nightly(day, environment="prod", time="033000"):
    return f"worker-{environment}-{day:%Y%m%d}T{time}Z.sql.gz"


# Make stub: writes the requested backup (with what it was given), fails for build roots whose name has "broken".
MAKE_STUB = r'''
directory= file= environment=
previous=
for argument; do
    [ "$previous" = -C ] && directory=$argument
    case "$argument" in file=*) file=${argument#file=} ;; ENV=*) environment=${argument#ENV=} ;; esac
    previous=$argument
done
echo "$directory $*" >> "${MAKE_LOG:-/dev/null}"
case "$directory" in *broken*) echo "Database backup failed; check service/permissions. No backup published." >&2; exit 2 ;; esac
mkdir -p "$(dirname "$file")"
printf '%s secret=%s setup=%s\n' "$environment" "${DATABASE_ROOT_PASSWORD:-}" "${SETUP_DIRECTORY:-}" | gzip > "$file"
echo "Database backup: $file"
'''


# Docker stub: `inspect` finds the secret stores named in STORES (running), any other container is missing.
DOCKER_STUB = r'''
eval "last=\${$#}"
echo "$*" >> "${DOCKER_LOG:-/dev/null}"
case " ${STORES-} " in *" $last "*) echo true ;; *) echo "Error: No such container: $last" >&2; exit 1 ;; esac
'''


class RotationTest(unittest.TestCase):
    """Keep the newest backup of each of the 7 latest days, then of 4 more ISO weeks; delete the rest."""

    def setUp(self):
        self.module = load_backup_projects()

    def test_env_files_are_read_as_setup_reads_them(self):
        module = load_backup_projects()
        text = ('# comment\nPROJECT_NAME=alpha  # inline\nexport DOMAIN = alpha.local\n'
                'APP_ENVIRONMENT="\n  ALPHA_TOKEN ERP_API_KEY\n  HTPASSWD_WEB\n"\n'
                "CACHE_CLEAR_COMMAND=\"sh -c 'kill -USR2 1'\"\nHASH='$apr1$x$y'\nEMPTY=\nESCAPED=\"a\\\"b\\nc\"\n")
        values = module.parse_env(text, "common.env")
        self.assertEqual(values["PROJECT_NAME"], "alpha")
        self.assertEqual(values["DOMAIN"], "alpha.local")
        self.assertEqual(values["APP_ENVIRONMENT"].split(), ["ALPHA_TOKEN", "ERP_API_KEY", "HTPASSWD_WEB"])
        self.assertEqual(values["CACHE_CLEAR_COMMAND"], "sh -c 'kill -USR2 1'")
        self.assertEqual(values["HASH"], "$apr1$x$y")
        self.assertEqual(values["EMPTY"], "")
        self.assertEqual(values["ESCAPED"], 'a"b\nc')
        for broken, line in (("just words\n", 1), ("A=1\nB=\"never closed\nC=2\n", 2)):
            with self.subTest(text=broken), self.assertRaisesRegex(module.Refused, f"line {line} is not NAME=value"):
                module.parse_env(broken, "common.env")

    def test_sixty_nights_keep_seven_daily_and_four_weekly(self):
        last = datetime(2026, 10, 9)  # a Friday, ISO week 41
        names = [nightly(last - timedelta(days=n)) for n in range(60)]
        keep, remove = self.module.rotation(names)
        daily = [nightly(datetime(2026, 10, d)) for d in (9, 8, 7, 6, 5, 4, 3)]
        # Weeks 41 and 40 are already kept by the dailies; the weeklies are the Sundays before.
        weekly = [nightly(datetime(2026, 9, d)) for d in (27, 20, 13, 6)]
        self.assertEqual(keep, daily + weekly)
        self.assertEqual(sorted(remove), sorted(set(names) - set(keep)))
        self.assertEqual(len(remove), 49)

    def test_the_newest_backup_of_a_day_is_the_daily_one(self):
        day = datetime(2026, 10, 9)
        names = [nightly(day), nightly(day, time="141500"), nightly(day - timedelta(days=1))]
        keep, remove = self.module.rotation(names)
        self.assertEqual(keep, [nightly(day, time="141500"), nightly(day - timedelta(days=1))])
        self.assertEqual(remove, [nightly(day)])

    def test_a_gap_does_not_cost_the_last_good_backups(self):
        # Backups failed for two weeks: the last seven that exist stay, whatever their age.
        names = [nightly(datetime(2026, 9, 20) - timedelta(days=n)) for n in range(10)]
        keep, _ = self.module.rotation(names)
        self.assertEqual(keep[:7], names[:7])
        self.assertEqual(self.module.rotation(names[:3]), (names[:3], []))


class BackupFixture(ScriptTest):
    """Build roots under a temporary worker root, with make and docker stubs."""

    def setUp(self):
        super().setUp()
        self.projects = self.root / "srv/projects"
        self.stub("make", MAKE_STUB)
        self.stub("docker", DOCKER_STUB)
        self.make_log = self.root / "make.log"
        self.docker_log = self.root / "docker.log"
        os.environ["MAKE_LOG"] = str(self.make_log)
        os.environ["DOCKER_LOG"] = str(self.docker_log)
        os.environ["SETUP_SERVER_BACKUP_TIME"] = "20261009T013000"
        os.environ["SETUP_SERVER_CONFIG"] = str(self.root / "etc")
        os.environ["STORES"] = "sample-prod-secrets sample-stage-secrets"
        for key in ("MAKE_LOG", "DOCKER_LOG", "SETUP_SERVER_BACKUP_TIME", "SETUP_SERVER_CONFIG", "STORES"):
            self.addCleanup(os.environ.pop, key)
        for key in ("DATABASE_ROOT_PASSWORD", "CI"):
            os.environ.pop(key, None)

    def project(self, name, *environments, layout="app"):
        directory = self.projects / name / layout
        (directory / "env").mkdir(parents=True)
        (directory / "Makefile").write_text("include $(SETUP_DIRECTORY)/docker/project.mk\n")
        (directory / "env/common.env").write_text("PROJECT_NAME=sample\n")
        for environment in environments:
            (directory / f"env/{environment}.env.example").write_text("DOMAIN=example.test\n")
            (directory / f"env/{environment}.env").write_text("DOMAIN=example.test\n")
        return directory

    def seed(self, directory, environment, nights):
        backups = directory / ".generated/backups"
        backups.mkdir(parents=True, exist_ok=True)
        for n in range(1, nights + 1):
            (backups / nightly(datetime(2026, 10, 9) - timedelta(days=n), environment, "013000")).write_text("old")
        return backups

    def tree(self):
        return sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*")
                      if path.name not in ("make.log", "docker.log"))

    def setup_checkout(self, files=True):
        """A setup checkout whose project.mk has (or, an older release, lacks) make files-backup."""
        setup = self.root / ("setup" if files else "old-setup")
        (setup / "docker").mkdir(parents=True, exist_ok=True)
        (setup / "docker/project.mk").write_text("db-backup:\n\t@true\n" + ("files-backup:\n\t@true\n" if files else ""))
        os.environ["SETUP_DIRECTORY"] = str(setup)
        self.addCleanup(os.environ.pop, "SETUP_DIRECTORY", None)
        return setup


class BackupProjectsTest(BackupFixture):
    def test_every_deployed_environment_is_backed_up_and_rotated(self):
        alpha = self.project("alpha_shop", "prod")
        beta = self.project("beta_docker", "stage", "prod", layout="app/docker")
        self.project("not_deployed")
        backups = self.seed(alpha, "prod", 40)
        # Pre-deploy backups of setup's own naming and stray files are never touched.
        (backups / "alpha-prod-20260101T000000000000.sql.gz").write_text("deploy")
        (backups / "worker-prod-notes.txt").write_text("x")
        result = self.run_script("backup-projects")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        kept = sorted(path.name for path in backups.glob("worker-prod-*.sql.gz"))
        self.assertEqual(len(kept), 11, kept)
        self.assertIn("worker-prod-20261009T013000Z.sql.gz", kept)
        self.assertTrue((backups / "alpha-prod-20260101T000000000000.sql.gz").exists())
        self.assertTrue((backups / "worker-prod-notes.txt").exists())
        for environment in ("stage", "prod"):
            self.assertTrue((beta / f".generated/backups/worker-{environment}-20261009T013000Z.sql.gz").is_file())
        self.assertIn("not_deployed: no env/<env>.env (not deployed from here)", result.stdout)
        self.assertIn("removed 30", result.stdout)
        self.assertIn("Backups: 3 ok, 0 failed", result.stdout)

    def test_a_monorepo_build_root_is_backed_up_from_its_app(self):
        # bin/add-project --monorepo: the repository is the build root, a target of its own per environment.
        gamma = self.project("gamma", "stage", "prod")
        (self.projects / "gamma/.git").mkdir()
        for environment in ("stage", "prod"):
            (self.projects / f"gamma/target.{environment}.env").write_text(
                f"DEPLOY_CONTEXT=default\nDEPLOY_SSH=\nDEPLOY_PATH=/home/tomas/Projects/runtime/gamma/{environment}\n")
        result = self.run_script("backup-projects")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for environment in ("stage", "prod"):
            self.assertTrue((gamma / f".generated/backups/worker-{environment}-20261009T013000Z.sql.gz").is_file())
        self.assertIn("Backups: 2 ok, 0 failed", result.stdout)

    def test_an_environment_the_project_lists_in_stage_environments_is_backed_up_like_stage(self):
        os.environ["STORES"] += " sample-demo-secrets"
        shop = self.project("shop", "prod", "demo", "old")
        (shop / "env/common.env").write_text('PROJECT_NAME=sample\nSTAGE_ENVIRONMENTS="demo, test bad_Name"\n')
        self.seed(shop, "demo", 40)
        result = self.run_script("backup-projects")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        made = sorted(path.name for path in (shop / ".generated/backups").glob("worker-*-20261009T013000Z.sql.gz"))
        self.assertEqual(made, ["worker-demo-20261009T013000Z.sql.gz", "worker-prod-20261009T013000Z.sql.gz"],
                         "old has an env file but is not listed; test is reserved")
        self.assertEqual(len(list((shop / ".generated/backups").glob("worker-demo-*"))), 11, "rotated as any series")
        one = self.run_script("backup-projects", "shop", "demo")
        self.assertEqual(one.returncode, 0, one.stdout + one.stderr)
        self.assertIn("ENV=demo", self.make_log.read_text())
        self.assertEqual(self.run_script("backup-projects", "shop", "old").returncode, 65)

    def test_one_failing_project_does_not_stop_the_others_and_keeps_its_backups(self):
        broken = self.project("a_broken", "prod")
        healthy = self.project("b_healthy", "prod")
        broken_backups = self.seed(broken, "prod", 40)
        self.seed(healthy, "prod", 40)
        result = self.run_script("backup-projects")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Database backup failed", result.stderr)
        self.assertIn("Backups: 1 ok, 1 failed: a_broken/prod", result.stdout)
        self.assertEqual(len(list(broken_backups.glob("worker-prod-*"))), 40, "no rotation after a failure")
        self.assertEqual(len(list((healthy / ".generated/backups").glob("worker-prod-*"))), 11)

    def test_check_changes_nothing(self):
        directory = self.project("alpha_shop", "prod")
        self.seed(directory, "prod", 40)
        before = self.tree()
        result = self.run_script("backup-projects", "--check")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("would back up alpha_shop prod into", result.stdout)
        self.assertIn("would delete 30:", result.stdout)
        self.assertEqual(self.tree(), before)
        self.assertFalse(self.make_log.exists(), "--check runs no backup")

    def test_one_project_by_build_root_for_the_backup_now_pipeline(self):
        self.project("alpha_shop", "stage", "prod")
        self.project("other", "prod")
        result = self.run_script("backup-projects", str(self.projects / "alpha_shop"), "prod")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.make_log.read_text().count("db-backup"), 1)
        self.assertIn("ENV=prod", self.make_log.read_text())
        self.assertIn("Backups: 1 ok, 0 failed", result.stdout)
        for arguments, message in ((["missing"], "No build root"), (["Bad.Name"], "NAME must be"),
                                   (["../etc"], "not a build root under"),
                                   ([str(self.root)], f"not a build root under {self.projects}"),
                                   (["alpha_shop", "local"], "ENV must be stage, prod or one of"),
                                   (["alpha_shop", "demo"], "ENV must be stage, prod or one of"),
                                   (["other", "stage"], "other has no env/stage.env")):
            with self.subTest(arguments=arguments):
                refused = self.run_script("backup-projects", *arguments)
                self.assertEqual(refused.returncode, 65, refused.stdout + refused.stderr)
                self.assertIn(message, refused.stderr)

    def test_the_secrets_come_from_the_stack_store_and_a_missing_store_is_named(self):
        import gzip
        directory = self.project("alpha_shop", "prod")
        check = self.run_script("backup-projects", "--check")
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
        self.assertIn("secrets: from sample-prod-secrets (running)", check.stdout)
        result = self.run_script("backup-projects")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        backup = directory / ".generated/backups/worker-prod-20261009T013000Z.sql.gz"
        self.assertIn("prod secret= setup=", gzip.decompress(backup.read_bytes()).decode(),
                      "no secret is handed to make: setup reads the store itself")
        # Not deployed with a store yet: named, nothing runs.
        os.environ["STORES"] = ""
        os.environ["SETUP_SERVER_BACKUP_TIME"] = "20261010T013000"
        missing = self.run_script("backup-projects")
        self.assertEqual(missing.returncode, 1)
        self.assertIn("no secret store sample-prod-secrets: deploy alpha_shop prod once with setup 2.5.0", missing.stderr)
        self.assertNotIn("20261010", self.make_log.read_text())
        self.assertEqual(self.run_script("backup-projects", "--check").returncode, 1)
        # A deployment step's own variables need no store.
        step = self.run_script("backup-projects", "alpha_shop", "prod", DATABASE_ROOT_PASSWORD="from-the-step", CI="true")
        self.assertEqual(step.returncode, 0, step.stdout + step.stderr)
        backup = directory / ".generated/backups/worker-prod-20261010T013000Z.sql.gz"
        self.assertIn("secret=from-the-step", gzip.decompress(backup.read_bytes()).decode())
        self.assertNotIn("from-the-step", step.stdout + step.stderr, "secrets are never printed")

    def test_the_store_is_looked_up_through_the_deploy_context(self):
        directory = self.project("beta_docker", "prod")
        (directory / "env/prod.env").write_text("DEPLOY_CONTEXT=shop-live\n")
        self.assertEqual(self.run_script("backup-projects", "--check").returncode, 0)
        self.assertIn("--context shop-live inspect --type container", self.docker_log.read_text())

    def test_unknown_options_are_a_usage_error(self):
        self.assertEqual(self.run_script("backup-projects", "--bogus").returncode, 64)

    def test_full_backups_are_a_series_of_their_own_with_two_kept(self):
        directory = self.project("alpha_shop", "prod")
        backups = self.seed(directory, "prod", 3)
        for day in (1, 8, 15):
            (backups / f"full-prod-202609{day:02d}T043000Z.sql.gz").write_text("old full")
        result = self.run_script("backup-projects", "--full", "alpha_shop", "prod")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("full=1", self.make_log.read_text())
        self.assertEqual(sorted(path.name for path in backups.glob("full-prod-*")),
                         ["full-prod-20260915T043000Z.sql.gz", "full-prod-20261009T013000Z.sql.gz"])
        self.assertEqual(len(list(backups.glob("worker-prod-*"))), 3, "the nightly series is not touched")
        nightly = self.run_script("backup-projects", "alpha_shop", "prod")
        self.assertEqual(nightly.returncode, 0)
        self.assertEqual(len(list(backups.glob("full-prod-*"))), 2, "the nightly run does not rotate the full series")
        check = self.run_script("backup-projects", "--check", "--full")
        self.assertIn("into", check.stdout)
        self.assertIn("full-prod-20261009T013000Z.sql.gz", check.stdout)

    def test_a_fresh_backup_is_made_once_and_a_recent_one_is_handed_out(self):
        directory = self.project("alpha_shop", "prod")
        result = self.run_script("backup-projects", "--fresh", "--progress", "alpha_shop", "prod")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        backups = directory / ".generated/backups"
        made = backups / "fresh-prod-20261009T013000Z.sql.gz"
        self.assertTrue(made.is_file())
        self.assertIn(f"progress backup {made.stat().st_size} {made.stat().st_size}", result.stderr)
        self.assertEqual(result.stdout, "", "with --progress everything goes to stderr")
        calls = self.make_log.read_text().count("db-backup")
        again = self.run_script("backup-projects", "--fresh", "alpha_shop", "prod")
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertIn("used instead of a new fresh backup", again.stdout)
        self.assertEqual(self.make_log.read_text().count("db-backup"), calls, "no second backup within 30 minutes")
        # A full fresh backup is not answered by a registry one.
        full = self.run_script("backup-projects", "--fresh", "--full", "alpha_shop", "prod")
        self.assertEqual(full.returncode, 0, full.stdout + full.stderr)
        self.assertTrue((backups / "fresh-full-prod-20261009T013000Z.sql.gz").is_file())
        self.assertTrue((backups / ".lock-prod").is_file())

    def test_files_backups_follow_backup_file_paths_and_need_no_secret_store(self):
        self.setup_checkout()
        os.environ["STORES"] = ""  # a files backup reads the target, not the database
        shop = self.project("shop", "stage", "prod")
        (shop / "env/common.env").write_text('PROJECT_NAME=sample\nBACKUP_FILE_PATHS="modules/*/translations/ config/local.php"\n')
        (shop / "env/stage.env").write_text("DOMAIN=example.test\nBACKUP_FILE_PATHS=\n")  # an env file wins
        other = self.project("other", "prod")
        backups = shop / ".generated/backups"
        backups.mkdir(parents=True)
        for n in range(1, 41):
            (backups / f"files-prod-{datetime(2026, 10, 9) - timedelta(days=n):%Y%m%d}T013000Z.tar.gz").write_text("old")
        (backups / nightly(datetime(2026, 10, 8))).write_text("db")
        check = self.run_script("backup-projects", "--check", "--files")
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
        self.assertIn(f"would back up shop prod into {backups}/files-prod-20261009T013000Z.tar.gz", check.stdout)
        self.assertIn("paths: modules/*/translations/ config/local.php", check.stdout)
        self.assertNotIn("secrets", check.stdout + check.stderr)
        self.assertFalse(self.make_log.exists(), "--check runs no backup")
        result = self.run_script("backup-projects", "--files")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.make_log.read_text()
        self.assertIn(f"files-backup ENV=prod file={backups}/files-prod-20261009T013000Z.tar.gz", calls)
        self.assertNotIn("ENV=stage", calls)
        self.assertNotIn("db-backup", calls)
        self.assertIn("other: no BACKUP_FILE_PATHS of a deployed environment; skipped", result.stdout)
        self.assertEqual(len(list(backups.glob("files-prod-*.tar.gz"))), 11, "7 daily + 4 weekly, as the nightly series")
        self.assertTrue((backups / nightly(datetime(2026, 10, 8))).exists(), "the database series is not touched")
        self.assertTrue((backups / ".lock-files-prod").is_file(), "a lock of its own: it does not wait for a database backup")
        self.assertIn("Backups: 1 ok, 0 failed", result.stdout)
        refused = self.run_script("backup-projects", "--files", "other")
        self.assertEqual(refused.returncode, 65)
        self.assertIn("other sets no BACKUP_FILE_PATHS", refused.stderr)
        self.assertEqual(self.run_script("backup-projects", "--files", "--full").returncode, 64)
        # A PrestaShop project needs no BACKUP_FILE_PATHS: setup's profile backs its translations up.
        (other / "env/common.env").write_text("PROJECT_NAME=other\nPROFILE=prestashop\n")
        profile = self.run_script("backup-projects", "--check", "--files", "other")
        self.assertEqual(profile.returncode, 0, profile.stdout + profile.stderr)
        self.assertIn("would back up other prod", profile.stdout)
        self.assertIn("paths: modules/*/translations/ themes/*/modules/*/translations/ mails/*/lang.php "
                      "themes/*/mails/*/lang.php translations/*-*/", profile.stdout)
        # A developer's fresh one (bin/backup-pull fresh … files) joins the series; a recent one is handed out.
        os.environ["SETUP_SERVER_BACKUP_TIME"] = "20261009T120000"
        hour_ago = datetime.now().timestamp() - 3600  # older than the 30 minutes a fresh one reuses
        for path in backups.glob("files-prod-*"):
            os.utime(path, (hour_ago, hour_ago))
        fresh = self.run_script("backup-projects", "--files", "--fresh", "--progress", "shop", "prod")
        self.assertEqual(fresh.returncode, 0, fresh.stdout + fresh.stderr)
        self.assertTrue((backups / "files-prod-20261009T120000Z.tar.gz").is_file())
        self.assertEqual(fresh.stdout, "", "with --progress everything goes to stderr")
        again = self.run_script("backup-projects", "--files", "--fresh", "shop", "prod")
        self.assertIn("used instead of a new fresh-files backup", again.stdout)
        self.assertEqual(self.make_log.read_text().count("files-backup"), 2)

    def test_a_setup_without_make_files_backup_is_named_and_nothing_runs(self):
        self.setup_checkout(files=False)
        shop = self.project("shop", "prod")
        (shop / "env/common.env").write_text("BACKUP_FILE_PATHS=modules/*/translations/\n")
        check = self.run_script("backup-projects", "--check", "--files")
        self.assertEqual(check.returncode, 1, check.stdout + check.stderr)
        self.assertIn("setup has no make files-backup; update it", check.stderr)
        result = self.run_script("backup-projects", "--files")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("shop prod: setup has no make files-backup", result.stderr)
        self.assertFalse(self.make_log.exists())


class BackupPolicyTest(BackupFixture):
    """How often the timers back up each series of a project environment: backup-policy.conf, --set."""

    def policy(self, text):
        path = self.root / "etc/backup-policy.conf"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def backup(self, directory, name):
        backups = directory / ".generated/backups"
        backups.mkdir(parents=True, exist_ok=True)
        (backups / name).write_text("old")
        return backups

    def made(self, *files):
        """The backups of this run (SETUP_SERVER_BACKUP_TIME) under the projects."""
        return sorted(f"{path.parents[3].name}/{path.name}" for path in self.projects.glob("*/app/.generated/backups/*")
                      if "20261009T013000Z" in path.name)

    def test_the_nightly_run_follows_each_environments_policy_and_makes_the_daily_fulls(self):
        for name in ("alpha", "beta", "gamma", "delta", "epsilon"):
            self.project(name, "prod")
        # delta's newest DB short backup is 3 days old (weekly: not due), epsilon's 7 days (due).
        self.backup(self.projects / "delta/app", "worker-prod-20261006T013000Z.sql.gz")
        self.backup(self.projects / "epsilon/app", "worker-prod-20261002T013000Z.sql.gz")
        # A fresh one does not count for the due rule.
        self.backup(self.projects / "epsilon/app", "fresh-prod-20261008T120000Z.sql.gz")
        self.policy("# notes\nbeta prod db-short=off db-full=daily\ngamma prod db-short=off db-full=off files=off  # archived\n"
                    "delta prod db-short=weekly\nepsilon prod db-short=weekly bogus=1 db-full=yearly\nremoved_project prod db-short=off\n")
        check = self.run_script("backup-projects", "--check")
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
        self.assertIn("==> beta prod: skipped: db-short off\n", check.stdout)
        self.assertIn("==> gamma prod: skipped: db-short off\n", check.stdout)
        self.assertIn("==> delta prod: not due: db-short weekly, newest 3 d ago\n", check.stdout)
        self.assertIn("would back up beta prod into", check.stdout)
        self.assertIn("full-prod-20261009T013000Z.sql.gz", check.stdout)
        self.assertFalse(self.make_log.exists(), "--check runs no backup")
        result = self.run_script("backup-projects")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.made(), ["alpha/worker-prod-20261009T013000Z.sql.gz", "beta/full-prod-20261009T013000Z.sql.gz",
                                       "epsilon/worker-prod-20261009T013000Z.sql.gz"])
        self.assertIn("full=1", [line for line in self.make_log.read_text().splitlines() if "/beta/" in line][0])
        self.assertIn("Backups: 3 ok, 0 failed", result.stdout)
        # Nothing due anywhere: said, and no failure.
        self.policy("alpha prod db-short=off\nbeta prod db-short=off\ngamma prod db-short=off\ndelta prod db-short=off\nepsilon prod db-short=off\n")
        none = self.run_script("backup-projects")
        self.assertEqual(none.returncode, 0, none.stdout + none.stderr)
        self.assertIn("Nothing to back up in this run", none.stdout)

    def test_the_full_series_of_db_full_daily_keeps_seven_daily_and_four_weekly(self):
        beta = self.project("beta", "prod")
        alpha = self.project("alpha", "prod")
        for directory in (alpha, beta):
            for n in range(1, 41):
                self.backup(directory, f"full-prod-{datetime(2026, 10, 9) - timedelta(days=n):%Y%m%d}T013000Z.sql.gz")
        self.policy("beta prod db-short=off db-full=daily\n")
        check = self.run_script("backup-projects", "--check")
        self.assertIn("would delete 30:", check.stdout)
        result = self.run_script("backup-projects")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(list((beta / ".generated/backups").glob("full-prod-*"))), 11)
        self.assertEqual(len(list((alpha / ".generated/backups").glob("full-prod-*"))), 40, "the nightly run makes no weekly full")
        # "Backup now" of a full one keeps the series' rotation; the default keeps the newest 2.
        os.environ["SETUP_SERVER_BACKUP_TIME"] = "20261009T150000"
        self.assertEqual(self.run_script("backup-projects", "--full", "beta", "prod").returncode, 0)
        self.assertEqual(len(list((beta / ".generated/backups").glob("full-prod-*"))), 11)
        self.assertEqual(self.run_script("backup-projects", "--full", "alpha", "prod").returncode, 0)
        self.assertEqual(len(list((alpha / ".generated/backups").glob("full-prod-*"))), 2)

    def test_the_weekly_run_backs_up_weekly_fulls_and_monthly_ones_when_due(self):
        for name in ("alpha", "beta", "gamma", "delta", "epsilon"):
            self.project(name, "prod")
        self.backup(self.projects / "alpha/app", "full-prod-20261008T013000Z.sql.gz")  # weekly: every weekly run
        self.backup(self.projects / "delta/app", "full-prod-20260929T013000Z.sql.gz")  # 10 days: not due
        self.backup(self.projects / "epsilon/app", "full-prod-20260909T013000Z.sql.gz")  # 30 days: due
        self.policy("beta prod db-full=daily\ngamma prod db-full=off\ndelta prod db-full=monthly\nepsilon prod db-full=monthly\n")
        result = self.run_script("backup-projects", "--full")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("==> beta prod: skipped: db-full daily (the nightly run makes it)\n", result.stdout)
        self.assertIn("==> gamma prod: skipped: db-full off\n", result.stdout)
        self.assertIn("==> delta prod: not due: db-full monthly, newest 10 d ago\n", result.stdout)
        self.assertEqual(self.made(), ["alpha/full-prod-20261009T013000Z.sql.gz", "epsilon/full-prod-20261009T013000Z.sql.gz"])

    def test_the_files_run_follows_the_files_policy(self):
        self.setup_checkout()
        for name in ("alpha", "beta", "gamma"):
            (self.project(name, "prod") / "env/common.env").write_text("BACKUP_FILE_PATHS=modules/*/translations/\n")
        self.backup(self.projects / "beta/app", "files-prod-20261007T013000Z.tar.gz")  # 2 days: not due
        self.policy("beta prod files=weekly\ngamma prod files=off\n")
        result = self.run_script("backup-projects", "--files")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("==> beta prod: not due: files weekly, newest 2 d ago\n", result.stdout)
        self.assertIn("==> gamma prod: skipped: files off\n", result.stdout)
        self.assertEqual(self.made(), ["alpha/files-prod-20261009T013000Z.tar.gz"])

    def test_backup_now_and_fresh_backups_ignore_the_policy(self):
        self.project("gamma", "prod")
        self.policy("gamma prod db-short=off db-full=off files=off\n")
        # The fresh one first: a registry backup younger than 30 minutes would be handed out instead.
        self.assertEqual(self.run_script("backup-projects", "--fresh", "gamma", "prod").returncode, 0)
        self.assertEqual(self.run_script("backup-projects", "gamma", "prod").returncode, 0)
        self.assertEqual(self.run_script("backup-projects", "--full", "gamma").returncode, 0)
        self.assertEqual(self.made(), ["gamma/fresh-prod-20261009T013000Z.sql.gz", "gamma/full-prod-20261009T013000Z.sql.gz",
                                       "gamma/worker-prod-20261009T013000Z.sql.gz"])

    def test_set_merges_writes_atomically_and_keeps_the_other_lines(self):
        alpha = self.project("alpha", "prod")
        self.project("beta", "stage", "prod")
        path = self.policy("# my notes\nbeta prod db-short=off db-full=daily  # DB full only\nremoved_project prod db-short=off\n")
        check = self.run_script("backup-projects", "--set", "alpha", "prod", "db-short=off", "db-full=daily", "--check")
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
        self.assertEqual(check.stdout, "alpha prod: db-short daily → off, db-full weekly → daily\n")
        self.assertEqual(path.read_text(), "# my notes\nbeta prod db-short=off db-full=daily  # DB full only\nremoved_project prod db-short=off\n")
        result = self.run_script("backup-projects", "--set", "alpha", "prod", "db-short=off", "db-full=daily")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, f"alpha prod: db-short daily → off, db-full weekly → daily\n    written to {path}\n")
        self.assertEqual(path.stat().st_mode & 0o777, 0o644)
        self.assertEqual(sorted(item.name for item in path.parent.iterdir()), [".backup-policy.conf.lock", "backup-policy.conf"],
                         "no temporary file is left")
        # Series not given keep their value; SERIES=default drops back to the default; a default line goes.
        self.assertEqual(self.run_script("backup-projects", "--set", "alpha", "prod", "files=weekly").returncode, 0)
        self.assertEqual(self.run_script("backup-projects", "--set", "beta", "prod", "db-short=default").returncode, 0)
        self.assertEqual(path.read_text(), "# my notes\nbeta prod db-full=daily  # DB full only\nremoved_project prod db-short=off\n"
                                           "alpha prod db-short=off db-full=daily files=weekly\n")
        again = self.run_script("backup-projects", "--set", "alpha", "prod", "db-short=off")
        self.assertEqual(again.stdout, "alpha prod: no change\n")
        self.assertEqual(self.run_script("backup-projects", "--set", "beta", "prod", "db-full=weekly").returncode, 0)
        self.assertEqual(path.read_text(), "# my notes\nremoved_project prod db-short=off\nalpha prod db-short=off db-full=daily files=weekly\n")
        # An environment of the project's STAGE_ENVIRONMENTS or with a target file of its own.
        (alpha / "env/common.env").write_text('STAGE_ENVIRONMENTS="demo"\n')
        (self.projects / "alpha/target.preview.env").write_text("DEPLOY_CONTEXT=default\n")
        for environment in ("demo", "preview"):
            with self.subTest(environment=environment):
                self.assertEqual(self.run_script("backup-projects", "--set", "alpha", environment, "db-short=weekly").returncode, 0)
        # A new file starts with its header.
        path.unlink()
        self.assertEqual(self.run_script("backup-projects", "--set", "beta", "stage", "db-full=off").returncode, 0)
        self.assertEqual(path.read_text().splitlines(),
                         ["# bin/backup-projects --set: how often each series of a project environment is backed up "
                          "(README: DB backups).", "beta stage db-full=off"])

    def test_set_refuses_unknown_projects_environments_and_bad_items(self):
        self.project("alpha", "prod")
        for arguments, code, message in ((["missing", "prod", "db-short=off"], 65, "No build root"),
                                         (["alpha", "demo", "db-short=off"], 65, "alpha has no environment demo"),
                                         (["alpha", "local", "db-short=off"], 65, "alpha has no environment local"),
                                         (["Bad.Name", "prod", "db-short=off"], 65, "NAME must be"),
                                         (["alpha", "prod", "db-short=monthly"], 64, "db-short=daily|weekly|off|default"),
                                         (["alpha", "prod", "bogus=daily"], 64, "SERIES=FREQUENCY"),
                                         (["alpha", "prod", "db=off"], 64, "SERIES=FREQUENCY"),
                                         (["alpha", "prod", "full=daily"], 64, "SERIES=FREQUENCY"),
                                         (["alpha", "prod", "db-short"], 64, "SERIES=FREQUENCY"),
                                         (["alpha", "prod"], 64, "Usage"),
                                         (["alpha", "prod", "db-short=off", "--full"], 64, "Usage")):
            with self.subTest(arguments=arguments):
                result = self.run_script("backup-projects", "--set", *arguments)
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                self.assertIn(message, result.stderr)
        self.assertFalse((self.root / "etc/backup-policy.conf").exists())
        if os.getuid() != 0:
            # Without a test configuration directory it writes the real root-owned file: only as root.
            env = {key: value for key, value in os.environ.items() if key != "SETUP_SERVER_CONFIG"}
            refused = subprocess.run([str(ROOT / "bin/backup-projects"), "--set", "alpha", "prod", "db-short=off"], capture_output=True,
                                     text=True, timeout=60, env={**env, "SETUP_SERVER_ROOT": str(self.root / "srv")})
            self.assertEqual(refused.returncode, 77, refused.stdout + refused.stderr)
            self.assertIn("Run as root", refused.stderr)

    def test_the_shared_reader_takes_the_defaults_for_what_a_line_does_not_set(self):
        module = load_backup_projects()
        config = self.root / "etc"
        self.assertEqual(module.backup_policy(config, "alpha", "prod"), {"db-short": "daily", "db-full": "weekly", "files": "daily"})
        self.policy("alpha prod db-short=off db-full=monthly files=hourly full=daily # db-short=weekly\nalpha prodx db-short=weekly\nalpha\n")
        self.assertEqual(module.backup_policy(config, "alpha", "prod"), {"db-short": "off", "db-full": "monthly", "files": "daily"})
        self.assertEqual(module.backup_policy(config, "alpha", "stage"), module.BACKUP_DEFAULT)


class InstallBackupsTest(ScriptTest):
    def setUp(self):
        super().setUp()
        os.environ["SETUP_SERVER_SYSTEMD"] = str(self.root / "systemd")
        os.environ["SETUP_SERVER_SBIN"] = str(self.root / "sbin")
        os.environ["SETUP_SERVER_SUDOERS"] = str(self.root / "sudoers.d")
        for key in ("SETUP_SERVER_SYSTEMD", "SETUP_SERVER_SBIN", "SETUP_SERVER_SUDOERS"):
            self.addCleanup(os.environ.pop, key)

    def test_check_shows_the_units_and_changes_nothing(self):
        result = self.run_script("install-backups", "--check", "--at", "02:15")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("OnCalendar=*-*-* 02:15:00", result.stdout)
        self.assertIn("Persistent=true", result.stdout)
        self.assertIn(f"would write {self.root}/systemd/setup-server-backup.timer", result.stdout)
        self.assertIn(f"setup-test-account-missing ALL=(root) NOPASSWD: {self.root}/sbin/setup-server-backup",
                      result.stdout)
        for name in ("systemd", "sbin", "sudoers.d", "srv"):
            self.assertFalse((self.root / name).exists(), "--check changes nothing")

    def test_the_sudo_rule_allows_only_the_backup_now_form(self):
        rule = f"setup-test-account-missing ALL=(root) NOPASSWD: {self.root}/sbin/setup-server-backup "
        projects = f"{self.root}/srv/projects/"
        # sudo 1.9.10+ matches the arguments with a regular expression: optionally --full, the build root,
        # optionally stage/prod.
        self.stub("sudo", 'echo "Sudo version 1.9.15p5"\n')
        exact = self.run_script("install-backups", "--check")
        self.assertIn(rule + f"^(--full )?{projects}[a-z0-9][a-z0-9_-]*( [a-z][a-z0-9]*)?$\n", exact.stdout)
        # Older sudo (Ubuntu 22.04: 1.9.9) has only wildcards: the first argument must be a build root (or
        # --full and then a build root), the command refuses anything else after it.
        self.stub("sudo", 'echo "Sudo version 1.9.9"\n')
        wildcard = self.run_script("install-backups", "--check")
        self.assertIn(rule + f"{projects}[a-z0-9]*, {self.root}/sbin/setup-server-backup --full {projects}[a-z0-9]*\n",
                      wildcard.stdout)

    def test_the_weekly_full_timer_runs_the_full_backup_and_keeps_its_day(self):
        result = self.run_script("install-backups", "--check", "--full-at", "Sat 05:15")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"ExecStart={self.root}/sbin/setup-server-backup --full\n", result.stdout)
        self.assertIn("OnCalendar=Sat *-*-* 05:15:00", result.stdout)
        self.assertIn("would enable setup-server-backup.timer, setup-server-backup-full.timer and "
                      "setup-server-backup-files.timer", result.stdout)
        default = self.run_script("install-backups", "--check")
        self.assertIn("OnCalendar=Sun *-*-* 04:30:00", default.stdout)
        # A rerun without --full-at keeps the installed day and time.
        (self.root / "systemd").mkdir()
        (self.root / "systemd/setup-server-backup-full.timer").write_text("[Timer]\nOnCalendar=Wed *-*-* 01:05:00\n")
        self.assertIn("OnCalendar=Wed *-*-* 01:05:00", self.run_script("install-backups", "--check").stdout)
        for bad in ("Sunday 04:30", "Sun 4:30", "04:30", "Sun 04:30; rm"):
            with self.subTest(bad=bad):
                self.assertEqual(self.run_script("install-backups", "--check", "--full-at", bad).returncode, 64)

    def test_the_files_timer_runs_the_files_backup_at_the_nightly_time_unless_told_otherwise(self):
        result = self.run_script("install-backups", "--check", "--at", "02:15")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"ExecStart={self.root}/sbin/setup-server-backup --files\n", result.stdout)
        files_timer = result.stdout.split("setup-server-backup-files.timer")[1]
        self.assertIn("OnCalendar=*-*-* 02:15:00", files_timer, "by default with the nightly database backup")
        self.assertIn("Tonight's files run:", result.stdout)
        own = self.run_script("install-backups", "--check", "--files-at", "23:40")
        self.assertIn("OnCalendar=*-*-* 23:40:00", own.stdout.split("setup-server-backup-files.timer")[1])
        # A rerun without --files-at keeps the installed time.
        (self.root / "systemd").mkdir()
        (self.root / "systemd/setup-server-backup-files.timer").write_text("[Timer]\nOnCalendar=*-*-* 01:05:00\n")
        kept = self.run_script("install-backups", "--check")
        self.assertIn("OnCalendar=*-*-* 01:05:00", kept.stdout.split("setup-server-backup-files.timer")[1])
        for bad in ("1:05", "24:00", "01:05; rm"):
            with self.subTest(bad=bad):
                self.assertEqual(self.run_script("install-backups", "--check", "--files-at", bad).returncode, 64)

    def test_a_rerun_keeps_the_installed_time_and_refresh_only_touches_an_installed_copy(self):
        (self.root / "systemd").mkdir()
        (self.root / "systemd/setup-server-backup.timer").write_text("[Timer]\nOnCalendar=*-*-* 02:15:00\n")
        result = self.run_script("install-backups", "--check")
        self.assertIn("OnCalendar=*-*-* 02:15:00", result.stdout)
        refresh = self.run_script("install-backups", "--check", "--refresh")
        self.assertEqual(refresh.returncode, 0, refresh.stdout + refresh.stderr)
        self.assertIn("is not installed; nothing to refresh", refresh.stdout)

    def test_installing_needs_root_and_a_valid_time(self):
        if os.getuid() != 0:
            self.assertEqual(self.run_script("install-backups").returncode, 77)
        self.assertEqual(self.run_script("install-backups", "--check", "--at", "25:00").returncode, 64)


@unittest.skipUnless(UBUNTU, "Set SETUP_SERVER_UBUNTU_IMAGE (e.g. ubuntu:22.04) to check on a real Ubuntu")
class RealUbuntuBackupTest(unittest.TestCase):
    def test_installed_units_verify_and_the_runner_account_backs_up_one_project_through_sudo(self):
        # The stubs travel inside the script: Docker Desktop shares no temporary directories.
        script = """set -eu
mkdir -p /stubs
cat > /stubs/make <<'STUB'
#!/bin/sh""" + MAKE_STUB + """STUB
printf '#!/bin/sh\\nexit 0\\n' > /stubs/systemctl
printf '#!/bin/sh\\necho true\\n' > /stubs/docker
chmod +x /stubs/make /stubs/systemctl /stubs/docker
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null && apt-get install -y -qq systemd sudo python3 make >/dev/null
useradd --system --create-home --home-dir /srv/setup-server --shell /bin/bash setup
# systemctl is a stub (no systemd in the container); the units are verified below.
PATH=/stubs:$PATH /setup-server/bin/install-backups
PATH=/stubs:$PATH /setup-server/bin/install-backups | grep -c unchanged
# verify only warns (exit 0) about a bad setting: any output is a finding.
systemd-analyze verify /etc/systemd/system/setup-server-backup.service /etc/systemd/system/setup-server-backup.timer \\
    /etc/systemd/system/setup-server-backup-full.service /etc/systemd/system/setup-server-backup-full.timer >/tmp/verify.log 2>&1 || true
cat /tmp/verify.log; [ ! -s /tmp/verify.log ]
echo UNITS_VERIFIED
visudo -cf /etc/sudoers.d/setup-server-backup && echo SUDOERS_VALID
# What the rule lets the runner account start (sudo -l: allowed or not, nothing runs).
build_root=/srv/setup-server/projects/alpha_shop
for arguments in "$build_root" "$build_root prod" "--full $build_root" "--full $build_root prod" "" "--check" "/etc" \\
        "--job alpha_shop prod" "/srv/setup-server/projects/../../etc" "$build_root --check" "--fresh $build_root" \\
        "--full --check"; do
    if sudo -u setup sudo -n -l /usr/local/sbin/setup-server-backup $arguments >/dev/null 2>&1; then
        echo "SUDO ALLOWED [$arguments]"
    else
        echo "SUDO REFUSED [$arguments]"
    fi
done
grep -q ' \^' /etc/sudoers.d/setup-server-backup && echo SUDO_REGEX
cp /stubs/make /stubs/docker /usr/local/bin/
app=/srv/setup-server/projects/alpha_shop/app
mkdir -p $app/env && touch $app/Makefile $app/env/prod.env && chown -R setup:setup /srv/setup-server/projects
cd /srv/setup-server
# The "backup now" pipeline step, as the runner account with Bitbucket's DOCKER_HOST.
sudo -u setup env DOCKER_HOST=tcp://localhost:2375 sh -c \\
    'unset DOCKER_HOST; BUILD_ROOT=/srv/setup-server/projects/alpha_shop; sudo -n /usr/local/sbin/setup-server-backup "$BUILD_ROOT"'
stat -c '%U %a %n' $app/.generated/backups/worker-prod-*.sql.gz
zcat $app/.generated/backups/worker-prod-*.sql.gz
/usr/local/sbin/setup-server-backup --check
"""
        result = subprocess.run([*DOCKER_RUN, "-v", f"{ROOT}:/setup-server:ro", UBUNTU, "bash", "-c", script], capture_output=True, text=True, timeout=900)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("\n6\n", result.stdout, "a second install changes nothing")
        self.assertIn("UNITS_VERIFIED", output)
        self.assertIn("SUDOERS_VALID", output)
        build_root = "/srv/setup-server/projects/alpha_shop"
        for arguments in (build_root, f"{build_root} prod", f"--full {build_root}", f"--full {build_root} prod"):
            self.assertIn(f"SUDO ALLOWED [{arguments}]", result.stdout)
        refused = ["", "--check", "/etc", "--job alpha_shop prod", "/srv/setup-server/projects/../../etc",
                   f"--fresh {build_root}", "--full --check"]
        if "SUDO_REGEX" in result.stdout:  # sudo 1.9.10+: nothing after the build root but stage or prod
            refused.append(f"{build_root} --check")
        for arguments in refused:
            self.assertIn(f"SUDO REFUSED [{arguments}]", result.stdout)
        self.assertRegex(result.stdout, r"setup 600 /srv/setup-server/projects/alpha_shop/app/\.generated/backups/worker-prod-")
        self.assertIn("prod secret= setup=/srv/setup-server/setup", result.stdout)
        self.assertIn("Backups: 1 ok, 0 failed", result.stdout)
        self.assertIn("secrets: from alpha_shop-prod-secrets (running)", result.stdout)


if __name__ == "__main__":
    unittest.main()
