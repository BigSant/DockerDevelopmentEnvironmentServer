"""A publish arriving during a real Git update must be applied before the workflow succeeds."""
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader('apply_local_update', str(ROOT / 'bin/apply-local-update'))
spec = importlib.util.spec_from_loader(loader.name, loader)
apply = importlib.util.module_from_spec(spec)
loader.exec_module(apply)


class ApplyLocalUpdateTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'auto-update').mkdir()
        self.paths, self.sources, self.targets = {}, {}, {}
        for side in ('server', 'setup'):
            source, checkout = self.root / (side + '-source'), self.root / side
            self.command('git', 'init', '-b', 'master', str(source))
            self.command('git', '-C', str(source), 'config', 'user.name', 'CI')
            self.command('git', '-C', str(source), 'config', 'user.email', 'ci@example.invalid')
            (source / 'code').write_text('old')
            self.command('git', '-C', str(source), 'add', '.')
            self.command('git', '-C', str(source), 'commit', '-m', 'Old')
            self.command('git', '-C', str(source), 'branch', 'deploy/local')
            self.command('git', 'clone', str(source), str(checkout))
            (source / 'code').write_text('new')
            self.command('git', '-C', str(source), 'commit', '-am', 'New')
            self.command('git', '-C', str(source), 'branch', '-f', 'deploy/local')
            self.targets[side] = self.command('git', '-C', str(source), 'rev-parse', 'HEAD')
            self.paths[side], self.sources[side] = checkout, source
        self.real_run, self.calls = subprocess.run, 0

    def command(self, *args):
        return subprocess.check_output(args, stderr=subprocess.DEVNULL, text=True).strip()

    def journal(self, result='up_to_date'):
        heads = {side: self.command('git', '-C', str(path), 'rev-parse', 'HEAD') for side, path in self.paths.items()}
        (self.root / 'auto-update/status.json').write_text(json.dumps({'result': result,
            'paths': {key: str(value) for key, value in self.paths.items()}, 'targets': heads}))

    def update(self, args, **kwargs):
        if args[:1] != ['sudo']:
            return self.real_run(args, **kwargs)
        self.assertEqual(args, ['sudo', '-n', 'systemctl', 'start', 'setup-server-auto-update.service'])
        self.calls += 1
        if self.calls == 2:
            for path in self.paths.values():
                self.command('git', '-C', str(path), 'fetch', 'origin')
                self.command('git', '-C', str(path), 'merge', '--ff-only', 'origin/deploy/local')
        self.journal()
        return subprocess.CompletedProcess(args, 0)

    def test_an_old_inflight_update_is_followed_by_the_new_publish(self):
        with patch.object(apply.subprocess, 'run', side_effect=self.update):
            heads = apply.apply(self.root, 'server', self.targets['server'])
        self.assertEqual(self.calls, 2)
        self.assertEqual(heads, self.targets)

    def test_failed_health_is_not_retried_as_an_old_update(self):
        def failed(args, **kwargs):
            if args[:1] != ['sudo']:
                return self.real_run(args, **kwargs)
            self.calls += 1
            self.journal('rolled_back')
            return subprocess.CompletedProcess(args, 0)
        with patch.object(apply.subprocess, 'run', side_effect=failed):
            with self.assertRaisesRegex(RuntimeError, 'did not succeed'):
                apply.apply(self.root, 'server', self.targets['server'])
        self.assertEqual(self.calls, 1)

    def test_dirty_checkout_is_preserved_and_not_retried(self):
        (self.paths['server'] / 'code').write_text('user edits')
        with patch.object(apply.subprocess, 'run', side_effect=self.update):
            with self.assertRaisesRegex(RuntimeError, 'local edits'):
                apply.apply(self.root, 'server', self.targets['server'])
        self.assertEqual(self.calls, 1)
        self.assertEqual((self.paths['server'] / 'code').read_text(), 'user edits')

    def test_stale_updates_have_a_bounded_retry_count(self):
        def stale(args, **kwargs):
            if args[:1] != ['sudo']:
                return self.real_run(args, **kwargs)
            self.calls += 1
            self.journal()
            return subprocess.CompletedProcess(args, 0)
        with patch.object(apply.subprocess, 'run', side_effect=stale):
            with self.assertRaisesRegex(RuntimeError, 'three completed'):
                apply.apply(self.root, 'server', self.targets['server'])
        self.assertEqual(self.calls, 3)
