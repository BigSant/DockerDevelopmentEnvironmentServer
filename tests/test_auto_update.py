"""Automatic updates against real local Git remotes, including failures and interrupted transactions."""

import fcntl
import getpass
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]


class AutoUpdateTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.sources, self.paths, self.before = {}, {}, {}
        for name in ('server', 'setup'):
            remote, source, local = (self.root / (name + suffix) for suffix in ('.git', '-source', ''))
            self.command('git', 'init', '--bare', str(remote))
            self.command('git', 'init', '-b', 'master', str(source))
            self.git(source, 'config', 'user.name', 'Update test')
            self.git(source, 'config', 'user.email', 'test@example.invalid')
            self.git(source, 'remote', 'add', 'origin', str(remote))
            (source / '.gitignore').write_text('__pycache__/\n')
            (source / 'content').write_text('original\n')
            (source / 'tests').mkdir()
            if name == 'server':
                (source / 'bin').mkdir()
                shutil.copyfile(ROOT / 'bin/auto-update', source / 'bin/auto-update')
                (source / 'tests/test_health.py').write_text('import unittest\nclass Health(unittest.TestCase):\n def test_ok(self): self.assertTrue(True)\n')
            else:
                (source / 'VERSION').write_text('1.0.0\n')
                (source / 'tests/run').write_text('print("setup health ok")\n')
            self.git(source, 'add', '.')
            self.git(source, 'commit', '-qm', 'Initial')
            if name == 'setup':
                self.git(source, 'tag', 'v1.0.0')
            self.git(source, 'push', '--tags', 'origin', 'master', 'HEAD:refs/heads/deploy/local')
            self.git(remote, 'symbolic-ref', 'HEAD', 'refs/heads/master')
            self.command('git', 'clone', '-q', str(remote), str(local))
            self.git(local, 'config', 'user.name', 'Update test')
            self.git(local, 'config', 'user.email', 'test@example.invalid')
            self.sources[name], self.paths[name] = source, local
            self.before[name] = self.git(local, 'rev-parse', 'HEAD')
        self.state = self.root / 'state'

    def command(self, *arguments):
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def git(self, path, *arguments):
        return self.command('git', '-C', str(path), *arguments)

    def advance(self, name, health=None, publish=True, tag=True):
        source = self.sources[name]
        (source / 'content').write_text('updated\n')
        if name == 'setup':
            (source / 'VERSION').write_text('1.0.1\n')
        if health is not None:
            (source / 'tests/test_health.py').write_text(health)
        self.git(source, 'add', '.')
        self.git(source, 'commit', '-qm', 'Updated')
        if name == 'setup' and tag:
            self.git(source, 'tag', 'v1.0.1')
        refs = ['master'] + (['HEAD:refs/heads/deploy/local'] if publish else [])
        self.git(source, 'push', '--tags', 'origin', *refs)
        return self.git(source, 'rev-parse', 'HEAD')

    def invoke(self, *arguments):
        return subprocess.run([sys.executable, str(ROOT / 'bin/auto-update'),
                               '--server-dir', str(self.paths['server']), '--setup-dir', str(self.paths['setup']),
                               '--state-dir', str(self.state), *arguments], capture_output=True, text=True, timeout=45)

    def heads(self):
        return {name: self.git(path, 'rev-parse', 'HEAD') for name, path in self.paths.items()}

    def status(self):
        return json.loads((self.state / 'status.json').read_text())

    def test_success_fast_forwards_both_master_branches_and_is_idempotent(self):
        targets = {name: self.advance(name) for name in self.paths}
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.heads(), targets)
        self.assertEqual(self.status()['result'], 'updated')
        self.assertEqual((self.state / 'status.json').stat().st_mode & 0o777, 0o600)
        for path in self.paths.values():
            self.assertEqual(self.git(path, 'branch', '--show-current'), 'master')
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('Checking setup-server', result.stdout)

    def test_unpublished_master_is_not_installed_and_check_does_not_move_checkouts(self):
        for name in self.paths:
            self.advance(name, publish=False)
        self.assertEqual(self.invoke().returncode, 0)
        self.assertEqual(self.heads(), self.before)
        for source in self.sources.values():
            self.git(source, 'push', 'origin', 'HEAD:refs/heads/deploy/local')
        result = self.invoke('--check')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.heads(), self.before)

    def test_missing_channel_waits_without_changing_either_checkout(self):
        self.git(self.sources['setup'], 'push', 'origin', ':refs/heads/deploy/local')
        self.advance('server')
        self.assertEqual(self.invoke().returncode, 0)
        self.assertEqual(self.status()['result'], 'waiting_for_ci')
        self.assertEqual(self.heads(), self.before)

    def test_untracked_files_dirty_work_and_other_branches_are_preserved(self):
        self.advance('server')
        server = self.paths['server']
        for name in ('new-work', 'content'):
            with self.subTest(name=name):
                path = server / name
                path.write_text('my work\n')
                result = self.invoke()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('local changes', result.stderr)
                self.assertEqual(path.read_text(), 'my work\n')
                self.assertEqual(self.heads(), self.before)
                if name == 'content':
                    self.git(server, 'restore', 'content')
                else:
                    path.unlink()
        self.git(server, 'checkout', '-qb', 'working')
        self.assertIn('not master', self.invoke().stderr)
        self.assertEqual(self.git(server, 'branch', '--show-current'), 'working')

    def test_local_commits_and_approved_commits_outside_master_are_refused(self):
        server = self.paths['server']
        (server / 'content').write_text('my commit\n')
        self.git(server, 'commit', '-qam', 'Local only')
        head = self.git(server, 'rev-parse', 'HEAD')
        self.assertIn('local commits', self.invoke().stderr)
        self.assertEqual(self.git(server, 'rev-parse', 'HEAD'), head)
        self.git(server, 'reset', '--hard', self.before['server'])
        source = self.sources['server']
        self.git(source, 'checkout', '-qb', 'unmerged')
        (source / 'content').write_text('not merged\n')
        self.git(source, 'commit', '-qam', 'Unmerged')
        self.git(source, 'push', 'origin', 'HEAD:refs/heads/deploy/local')
        self.assertIn('not on origin/master', self.invoke().stderr)
        self.assertEqual(self.heads(), self.before)

    def test_setup_must_match_its_release_tag(self):
        self.advance('server')
        self.advance('setup', tag=False)
        self.assertNotEqual(self.invoke().returncode, 0)
        self.assertEqual(self.heads(), self.before)

    def test_failed_health_returns_both_and_waits_for_a_new_version(self):
        self.advance('server', health='import unittest\nclass Health(unittest.TestCase):\n def test_ok(self): self.fail("broken release")\n')
        self.advance('setup')
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('both checkouts returned', result.stderr)
        self.assertEqual(self.heads(), self.before)
        self.assertEqual(self.status()['result'], 'rolled_back')
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('previously failed', result.stdout)
        self.assertNotIn('Checking setup-server', result.stdout)

    def test_interrupted_pair_is_recovered_before_another_update(self):
        targets = {name: self.advance(name) for name in self.paths}
        server = self.paths['server']
        self.git(server, 'fetch', 'origin')
        self.git(server, 'merge', '--ff-only', targets['server'])
        self.state.mkdir()
        (self.state / 'status.json').write_text(json.dumps({'result': 'updating', 'before': self.before,
            'targets': targets, 'paths': {name: str(path) for name, path in self.paths.items()}}))
        result = self.invoke()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('Recovered', result.stdout)
        self.assertEqual(self.heads(), self.before)
        self.assertEqual(self.status()['result'], 'rolled_back')
        self.assertEqual(self.invoke('--retry').returncode, 0)
        self.assertEqual(self.heads(), targets)

    def test_edits_during_health_checks_are_never_discarded_by_rollback(self):
        self.advance('server', health='from pathlib import Path\nimport unittest\n'
                     'class Health(unittest.TestCase):\n def test_ok(self): Path("content").write_text("work during update")\n')
        self.advance('setup')
        result = self.invoke()
        self.assertIn('manual recovery needed', result.stderr)
        self.assertEqual((self.paths['server'] / 'content').read_text(), 'work during update')
        self.assertEqual(self.status()['result'], 'updating')

    def test_a_second_process_does_not_overlap_an_update(self):
        self.state.mkdir()
        with (self.state / 'update.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.invoke()
        self.assertEqual(result.returncode, 0)
        self.assertIn('already running', result.stdout)

    def test_installer_check_is_portable_and_does_not_install_anything(self):
        units = self.root / 'units'
        environment = {**os.environ, 'SETUP_SERVER_CHECKOUT': str(self.paths['server']),
                       'SETUP_DIRECTORY': str(self.paths['setup']), 'SETUP_SERVER_USER': getpass.getuser(),
                       'SETUP_SERVER_ROOT': str(self.root / 'runtime'), 'SETUP_SERVER_SYSTEMD': str(units)}
        result = subprocess.run(['bash', str(ROOT / 'bin/install-auto-update'), '--check'], env=environment,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Persistent=true', result.stdout)
        self.assertIn('User=' + getpass.getuser(), result.stdout)
        self.assertIn('--setup-dir ' + str(self.paths['setup']), result.stdout)
        self.assertFalse(units.exists())
        self.assertFalse((self.root / 'runtime').exists())
        tools = self.root / 'missing-php-bin'
        tools.mkdir()
        for name in ('python3', 'git', 'make', 'docker', 'systemctl'):
            (tools / name).write_text('#!/bin/sh\nexit 0\n')
            (tools / name).chmod(0o755)
        environment['PATH'] = str(tools)
        result = subprocess.run(['/bin/bash', str(ROOT / 'bin/install-auto-update'), '--check'], env=environment,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 65)
        self.assertIn('Required for local health checks: php', result.stderr)
        self.assertFalse(units.exists())

    def test_promotion_publishes_only_the_tested_master(self):
        source = self.sources['server']
        tested = self.advance('server', publish=False)
        workflow = (ROOT / '.github/workflows/tests.yml').read_text()
        step = workflow.split('      - name: Publish only the tested master commit\n', 1)[1]
        script = textwrap.dedent(step.split('        run: |\n', 1)[1])
        environment = {**os.environ, 'TESTED': self.before['server']}
        result = subprocess.run(['bash', '-c', script], cwd=source, env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git(self.root / 'server.git', 'rev-parse', 'deploy/local'), self.before['server'])
        environment['TESTED'] = tested
        result = subprocess.run(['bash', '-c', script], cwd=source, env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git(self.root / 'server.git', 'rev-parse', 'deploy/local'), tested)
