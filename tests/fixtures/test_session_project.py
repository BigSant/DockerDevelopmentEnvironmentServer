"""A minimal generic project for the test sessions and runs (bin/test-session), as create-project writes one.

    python3 tests/fixtures/test_session_project.py PARENT SETUP [--lock]

writes PARENT/autotest-demo: an environment repository (git, one commit) with app/ (the setup project: generic PHP,
SERVICES=playwright, a schema snapshot and the data set dev) and a TypeScript Playwright project in
app/tests/playwright with one dependency (ms) and tests tagged for every target:

    home.spec.ts      untagged: ENV=test only                 (uses ms, the dependency)
    targets.spec.ts   @stage, @stage-write, @live, @live @deploy, @test @stage
    fixme.spec.ts     fails until a patch fixes it (FIX_PATCH below is that patch)

--lock makes package-lock.json with npm in the Playwright image (needs Docker and the npm registry); --lock-file FILE
takes that of an earlier build; without either the lock file of a previous build in PARENT is kept, or none is written.
"""

import argparse
import json
from pathlib import Path
import subprocess
import sys

NAME = 'autotest-demo'
PLAYWRIGHT = '1.61.1'
SCHEMA = ('CREATE TABLE `app_note` (\n  `id` int NOT NULL AUTO_INCREMENT,\n  `body` varchar(255) NOT NULL,\n'
          '  PRIMARY KEY (`id`)\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;\n')
INDEX = '''<?php
header('Content-Type: text/html; charset=utf-8');
?><!doctype html>
<html lang="en"><head><title>Fixture shop</title><style>h1 { color: #123; }</style>
<script>window.fixture = true;</script></head>
<body><h1>Fixture shop</h1><p id="status">ready</p>
<nav><a href="/about.php">About</a></nav><form><label>Search <input name="q"></label><button>Go</button></form></body></html>
'''
CONFIG = '''const path = require('node:path');
const { defineConfig } = require('@playwright/test');

const dataDirectory = process.env.PLAYWRIGHT_DATA_DIR;

module.exports = defineConfig({
  testDir: process.env.PLAYWRIGHT_TEST_DIR,
  outputDir: path.join(dataDirectory, 'test-results'),
  retries: 0,
  workers: 1,
  reporter: [['list'], ['html', { outputFolder: path.join(dataDirectory, 'report'), open: 'never' }]],
  use: { baseURL: process.env.BASE_URL, ignoreHTTPSErrors: true, trace: 'retain-on-failure', screenshot: 'only-on-failure' },
});
'''
SPECS = {
    'home.spec.ts': '''import { test, expect } from '@playwright/test';
import ms from 'ms';

test('home page shows the shop', async ({ page }) => {
  expect(ms('1m')).toBe(60000);
  await page.goto('/');
  await expect(page.locator('h1')).toHaveText('Fixture shop');
});
''',
    'targets.spec.ts': '''import { test, expect } from '@playwright/test';

test('stage reads the home page @stage', async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('h1')).toHaveText('Fixture shop');
});

test('stage writes an order @stage-write', async ({ page }) => {
  await page.goto('/');
});

test('live reads the home page @live', async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('h1')).toBeVisible();
});

test('live deploy check @live @deploy', async ({ page }) => {
  const response = await page.goto('/');
  expect(response && response.status()).toBe(200);
});

test('test and stage both @test @stage', async ({ page }) => {
  await page.goto('/');
});
''',
    'fixme.spec.ts': '''import { test, expect } from '@playwright/test';

test('status says ready', async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('#status')).toHaveText('broken', { timeout: 2000 });
});
''',
}
# The patch that makes fixme.spec.ts pass, relative to the project directory (app/), as `git diff --relative` writes it.
FIX_PATCH = '''diff --git a/tests/playwright/fixme.spec.ts b/tests/playwright/fixme.spec.ts
--- a/tests/playwright/fixme.spec.ts
+++ b/tests/playwright/fixme.spec.ts
@@ -2,5 +2,5 @@ import { test, expect } from '@playwright/test';

 test('status says ready', async ({ page }) => {
   await page.goto('/');
-  await expect(page.locator('#status')).toHaveText('broken', { timeout: 2000 });
+  await expect(page.locator('#status')).toHaveText('ready', { timeout: 2000 });
 });
'''
# A patch outside the allowed paths: refused.
OUTSIDE_PATCH = '''diff --git a/public/index.php b/public/index.php
--- a/public/index.php
+++ b/public/index.php
@@ -1,2 +1,2 @@
-<?php
+<?php // changed
 header('Content-Type: text/html; charset=utf-8');
'''
PACKAGE = {'name': 'autotest-demo-e2e', 'private': True, 'version': '1.0.0',
           'dependencies': {'ms': '2.1.3'}, 'devDependencies': {'@playwright/test': PLAYWRIGHT}}


def git(root, *arguments):
    subprocess.run(['git', '-C', str(root), '-c', 'user.name=fixture', '-c', 'user.email=fixture@example.invalid',
                    *arguments], check=True, capture_output=True)


def build(parent, setup, lock=False, lock_file=None, services='playwright'):
    """The fixture under parent; returns its root (the environment repository)."""
    sys.path[:0] = [str(setup), str(Path(setup) / 'docker')]
    import contextlib
    import io
    from create_project import complete_options, scaffold
    root = Path(parent).resolve() / NAME
    kept_lock = root / 'app/tests/playwright/package-lock.json'
    previous = kept_lock.read_bytes() if kept_lock.is_file() else None
    if root.exists():
        subprocess.run(['rm', '-rf', str(root)], check=True)
    with contextlib.redirect_stdout(io.StringIO()):
        app = scaffold(NAME, parent, complete_options({'profile': 'generic', 'php_version': '8.4', 'database': 'mysql',
                                                      'database_version': '8.4', 'services': services}))
    common = app / 'env/common.env'
    # The versions of setup's own PHP 8.4 test images, so a machine that ran setup's tests builds nothing new.
    common.write_text(common.read_text() + 'XDEBUG_VERSION=3.5.3\nCOMPOSER_VERSION=2.10.3\nSCHEMA_DIRECTORY=app/database/schema\n'
                      'PLAYWRIGHT_COMMAND=npx playwright test --config=/e2e/config/playwright.config.cjs\n')
    schema = app / 'database/schema'
    schema.mkdir(parents=True)
    (schema / 'table-app_note.sql').write_text(SCHEMA)
    (schema / 'schema.manifest.json').write_text(json.dumps(
        {'format': 1, 'dialect': 'mysql', 'tables': [{'name': 'app_note', 'file': 'table-app_note.sql'}]}) + '\n')
    (app / 'database/data/dev').mkdir(parents=True)
    (app / 'database/data/dev/content.sql').write_text("INSERT INTO `app_note` (`id`, `body`) VALUES\n(1,'hello');\n")
    (app / 'public/index.php').write_text(INDEX)
    (app / 'public/about.php').write_text('<!doctype html><title>About</title><h1>About the fixture</h1>\n')
    (app / 'config/playwright').mkdir(parents=True, exist_ok=True)
    (app / 'config/playwright/playwright.config.cjs').write_text(CONFIG)
    tests = app / 'tests/playwright'
    tests.mkdir(parents=True)
    for name, text in SPECS.items():
        (tests / name).write_text(text)
    (tests / 'package.json').write_text(json.dumps(PACKAGE, indent=2) + '\n')
    (tests / '.gitignore').write_text('node_modules/\n')
    if lock:
        image = f'mcr.microsoft.com/playwright:v{PLAYWRIGHT}-jammy'
        subprocess.run(['docker', 'run', '--rm', '--user', f'{__import__("os").getuid()}:{__import__("os").getgid()}',
                        '-e', 'HOME=/tmp', '-v', f'{tests}:/work', '-w', '/work', image,
                        'npm', 'install', '--package-lock-only', '--no-audit', '--no-fund'], check=True, capture_output=True)
    elif lock_file or previous:
        kept_lock.write_bytes(Path(lock_file).read_bytes() if lock_file else previous)
    git(root, 'init', '-q', '-b', 'master')
    git(root, 'add', '-A')
    git(root, 'commit', '-q', '-m', 'Fixture project for test sessions')
    return root


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('parent')
    parser.add_argument('setup')
    parser.add_argument('--lock', action='store_true')
    parser.add_argument('--lock-file', help='a package-lock.json of an earlier build to use')
    options = parser.parse_args()
    print(build(options.parent, options.setup, options.lock, options.lock_file))
