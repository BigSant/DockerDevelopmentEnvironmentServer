# setup-server — pointers

- Tests: `python3 -m unittest discover -s tests`; one module from `tests/` (they import each other): `cd tests && python3 -m unittest test_ui`. CI runs on the local self-hosted worker, using the Ubuntu 22.04 / Python 3.10 Docker runtime in `tests/fixtures/ci.Dockerfile`; keep Python 3.10 compatibility (no 3.12-only syntax).
- README.md (Lithuanian) is the documentation of record; a behaviour change updates its section. Commits: an English sentence as subject, the why in the body.
- Installed copies are single root-owned files with this host's paths baked in by `sed` (bin/install-*); helpers they share are duplicated on purpose and kept identical by `SharedHelpersTest` (tests/test_metrics.py).
- `bin/update` refreshes the copies but never the sudo rule: a change to bin/install-ui's `ROOT_COMMANDS` needs `sudo bin/install-ui` on the server.
- Web UI (bin/web-ui): its CSP allows no script and no inline `style=`; pages are static HTML, CSS in `STYLE` (/ui.css). The look follows setup's board (setup repo: docker/runtime/cron/ui/index.php). Preview it with the launch config `web-ui` (tests/preview_ui.py: the test fixture behind an auto-login proxy on 127.0.0.1:9178).
