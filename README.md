# Server OPS – preparing the build server and targets

**Server OPS** (**OPS** for short) is the name of the system: CI/CD, SSH access, backups and server status in one
place; it used to be called setup-server. The technical names stay the old ones so that changing them does not require
a migration on every server: the clone directory `~/setup-server`, the services `setup-server-*`, the paths `/etc/setup-server`,
`/usr/local/lib/setup-server`, `/srv/setup-server`, the variables `SETUP_SERVER_*`.

Scripts that prepare an Ubuntu server for setup v2 projects. This server is at the same time the
**build server** (Bitbucket Pipelines and/or GitHub Actions self-hosted runners, building images and
releases) and the **target** (the project containers run there). Deploys are controlled in Bitbucket or GitHub,
while the work is done by the runner on this server, so no cloud build minutes are used. Both runners can
run on the same server together.

The first server is the VPN server (Ubuntu Desktop), the first project is alpha.local.

## What it sets up

| Script | What it does |
| --- | --- |
| `bin/prepare-host` | Packages (`git`, `make`, `rsync`, `python3`, OpenJDK 25 for the runner), Docker with Compose (if missing – from download.docker.com; an existing Docker is left unchanged), the `setup` account with the Docker group, the `/srv/setup-server` directories, the git key, the shared setup checkout. |
| `bin/install-runner` | The Bitbucket Linux Shell runner as the systemd service `setup-server-runner` (account `setup`); with `--provider github` – the GitHub Actions runner, service `setup-server-github-runner@<scope>` (one per organization or repo). |
| `bin/add-project` | The project build directory `/srv/setup-server/projects/<repo name>`: the environment repo `app/`, a separate code repo `app/public/`; monorepo (`--monorepo`) – the repo itself is the build directory. A GitHub repo – with its own deploy key. |
| `bin/remove-project` | Removes a project build directory; DB backups and the deploy history – only with `--with-backups`; it does not delete a directory used by a running container. |
| `bin/update` | Updates this Server OPS checkout and the shared setup checkout to the given tag, checks health, and on failure returns both to their previous commits. It does not restart the runner service. |
| `bin/install-auto-update` | Installs a one-minute systemd timer for the administrator's two existing `master` clones: only after a successful GitHub CI, without touching local work. It also works on a manually prepared CachyOS; it does not need `prepare-host`. |
| `bin/install-backups` | DB backups: nightly (`setup-server-backup.timer`, according to the table registry) and weekly full (`setup-server-backup-full.timer`, the data of all tables); a nightly file backup (`setup-server-backup-files.timer`, the project's `BACKUP_FILE_PATHS` paths from the target); the command `/usr/local/sbin/setup-server-backup` (a copy of `bin/backup-projects`) and a sudo rule "backup now" for the pipeline. How often each project environment is backed up (or DB full only, or not at all): `bin/backup-projects --set`, the backup policy (step 6). |
| `bin/install-ssh` | SSH from the VPN: `openssh-server` (Ubuntu Desktop does not have it), started and starting after a reboot, settings `/etc/ssh/sshd_config.d/10-setup-server.conf` – from the given networks and from this server only with a key, no password; with ufw enabled – an allow rule for those networks. Needed for `make db-pull` and the web UI tunnel. |
| `bin/add-backup-key` | A developer's SSH key for downloading DB backups (`make db-pull`) – only for the allowed project environments (`--pull PROJECT:ENV=db`, `=fresh` – also to order a new backup, `make db-pull fresh=1`; `--pull-all` – all projects): only from the given network (VPN), only the command `/usr/local/sbin/setup-server-backup-pull` (a copy of `bin/backup-pull`), no shell and no Docker. With `--grant PROJECT:ENV=LEVEL` – also access to that project's target through this server (`make ssh`), via `/usr/local/sbin/setup-server-access` (a copy of `bin/server-access`). |
| `bin/tool-access` | Who opens the projects' browser tools (pma, cron, mailpit, backup, metrics, the project's `PROXY_TOOLS`) and from which addresses: a key name with a password set by the administrator and the allowed project environments, this server's addresses. A change on the target takes effect immediately (setup 3.6.0+ `make tool-access`). See "Tool access". |
| `bin/install-metrics` | The worker's Prometheus `/metrics`: systemd service `setup-server-metrics` (account `setup`, read-only), the command `/usr/local/sbin/setup-server-metrics` (a copy of `bin/worker-metrics`), settings `/etc/setup-server/metrics.env`. |
| `bin/install-ui` | The web UI in a browser (only `127.0.0.1`, through an SSH tunnel): service `setup-server-ui` (a separate account `setup-ui`), the command `/usr/local/sbin/setup-server-ui` (a copy of `bin/web-ui`), command copies in `/usr/local/lib/setup-server/bin/`, a sudo rule, settings `/etc/setup-server/ui.env`. |
| `bin/move-bitbucket-ssh` | Moves the build directories' Bitbucket remotes from `bitbucket.org` to `ssh.bitbucket.org` (Bitbucket SSH address since 2026-11-12); it moves them only when the new address responds. See "Bitbucket SSH address". |
| `bin/server-status` | Server status in JSON (read-only): services, timers, versions, disk, projects, containers. The web UI uses it; it can also be run by hand. |

All scripts are idempotent and can be repeated. With `--check` they only show what they would change
and change nothing. Run them with `--check` first.

```text
~/setup-server/                 this repo, the administrator's clone (the scripts are run from it)
/srv/setup-server/              home of the setup account
├── setup/                      the shared setup checkout (on servers – on a release tag)
├── projects/<name>/app/        the project build directory (BUILD_ROOT = projects/<name>)
│   └── .generated/backups/     DB backups: worker-<env>-<UTC time>Z.sql.gz – nightly and "backup now",
│                               full-<env>-…Z – weekly full, fresh(-full)-<env>-…Z – developers' "fresh";
│                               files-<env>-…Z.tar.gz – file backups (BACKUP_FILE_PATHS), .files-<env>/ – their mirror
├── runner/current/             the Bitbucket runner version; runner/temp – its work directory
├── github-runner/<scope>/      the GitHub runner (organization `bigsant`, repo `bigsant.<repo>`); _work – work directory
├── ui-actions.log              the web UI action log (setup-ui, 0640)
├── backup-pulls.log, access.log   developer key requests: backups and access to targets
├── access-sessions/<key>/      what was captured in interactive sessions (input, 0600; kept 180 days)
├── .ssh/id_ed25519             the git key (read-only): the setup repo and Bitbucket projects
└── .ssh/github-<owner>.<repo>  a GitHub project deploy key (read-only); host aliases – .ssh/config
/etc/setup-server/runner.env    the Bitbucket runner's OAuth credentials (root, 0600)
/etc/setup-server/metrics.env   the worker's /metrics address, allowed addresses and an optional token (root, 0600)
/etc/setup-server/ui.env        the web UI's first administrator login (password hash) and Bitbucket tokens (root, 0600)
/usr/local/lib/setup-server/bin/   command copies the web UI runs via sudo (root)
```

## Steps

1. **On the server**, clone this repo and check the machine:

   ```bash
   git clone git@github.com:BigSant/DockerDevelopmentEnvironmentServer.git ~/setup-server
   cd ~/setup-server
   bin/prepare-host --check
   ```

   The preview shows the Ubuntu version, RAM, free space, missing packages, whether Docker is present and
   whether Compose is new enough, and also what the script would create. The runner needs **≥ 8 GB RAM**
   and **OpenJDK 25**.

2. **Prepare** (needs sudo):

   ```bash
   sudo bin/prepare-host --setup-ref v2.0.0
   ```

   At the end the public git key is shown. Add it **read-only** to the setup repo (GitHub,
   deploy key) and to every Bitbucket project repo (Access keys); a GitHub project repo gets its own
   key (see step 4). Then run the script once
   more: it will clone setup. The `bitbucket.org`, `ssh.bitbucket.org` and `github.com` SSH host keys are written only when
   the scanned key's fingerprint matches the one published by the service (they are recorded in the script with links to
   the GitHub and Atlassian pages); otherwise the key is not written and "Needs attention" is shown.

3. **Runner.** Bitbucket: *Repository (or Workspace) settings → Runners → Add runner → Linux
   Shell*, add the label `setup.build`. **Do not run** the dialog's commands – the script will ask for
   the values in them one by one:

   ```bash
   sudo bin/install-runner --check   # asks for the values, changes nothing
   sudo bin/install-runner           # asks for the same values, installs
   ```

   It asks for: the download link (the whole `curl …` line can be pasted), `accountUuid`,
   `repositoryUuid` (for a workspace runner – Enter), `runnerUuid`, `OAuthClientId`, `OAuthClientSecret`
   (not shown while typing). You can also paste `--accountUuid {…}` – only the value is taken; an empty value is
   an error. The archive is taken only from `product-downloads.atlassian.com` (another one can be given with `--tarball`).
   The whole dialog text can also be fed through stdin (`sudo bin/install-runner < dialog.txt`).

   The OAuth credentials are written only to `/etc/setup-server/runner.env` (root, 0600); do not copy the dialog text
   anywhere else. While the runner is running, `OAuthClientId` and `OAuthClientSecret` on this server
   are readable (that is how Atlassian built it):

   - **on the command line**: systemd passes them to `start.sh` as arguments, and it passes them to the `java` process as
     `-D…` parameters; `ps` and `/proc/<PID>/cmdline` show them to **all** accounts on the server;
   - **in the environment** (`RUNNER_ARGS` from the root file): `/proc/<PID>/environ` is readable by the `setup` account, and so by
     all of its processes, **including pipeline steps**.

   `start.sh` (6.x) also accepts the values from environment variables (`OAUTH_CLIENT_SECRET` etc.), it has no configuration
   file, but it still passes them to the `java` process on the command line, so that would hide nothing –
   the argument method is kept. Because of this, do not let accounts onto this server that must not see these credentials.

   After a minute the runner must be **ONLINE** in Bitbucket. The worker runs **one** Bitbucket runner (the service
   `setup-server-runner`): the same script with another runner's values **replaces** it. So choose a **workspace**
   runner (*Workspace settings → Pipelines → Workspace runners → Add runner*, Linux Shell, label `setup.build`;
   `repositoryUuid` – Enter): all repos of that workspace use it, while a repository runner serves only its own. After a replacement the old one stays
   OFFLINE – remove it. Bitbucket has no public API for creating a runner (only an internal, unsupported one).

   **GitHub Actions runner** (for projects on GitHub; it runs alongside the Bitbucket runner and does not touch it).
   GitHub: *organization* (recommended: one runner for all of the organization's repos) or *repo
   Settings → Actions → Runners → New self-hosted runner*, Linux. An organization runner needs
   organization owner rights, a repo one – repo administrator rights. **Do not run** the page's commands:

   ```bash
   sudo bin/install-runner --provider github --check   # asks for the URL and token, changes nothing
   sudo bin/install-runner --provider github           # asks for the same values, installs
   ```

   It asks for: the organization or repo URL (the `--url` value, e.g. `https://github.com/BigSant` or
   `https://github.com/BigSant/<repo>`) and the registration token (the `--token` value, not shown while typing).
   The whole page text can also be fed through stdin (`sudo bin/install-runner --provider github < page.txt`) –
   only `--url` and `--token` are taken from it. The token **is valid for an hour** and registers once: when it expires –
   refresh the page and take a new one. The API also issues it (`POST /orgs/{org}/actions/runners/registration-token`,
   `admin:org`; `POST /repos/{owner}/{repo}/actions/runners/registration-token`).

   - **Archive** – the version pinned in the script (2.338.0) from the `github.com/actions/runner` releases; it is installed only
     if the SHA-256 matches (recorded in the script, the same as on the release page). Another version – `--tarball URL --sha256 HASH`.
     A newer version offered on the page is only shown: the runner updates itself.
   - **Labels.** GitHub itself adds `self-hosted`, `Linux`, `X64`, the script – `setup.build`. Workflow jobs:
     `runs-on: [self-hosted, linux, setup.build]` (the runner must have all the listed labels). `--no-default-labels`
     is not used: `self-hosted` would then be just a custom label, and `X64` does no harm.
   - **Name** on GitHub – the server name (`hostname -s`), another one – `--name`. It registers with `--replace`: when the
     server is reinstalled, the same name replaces the old registration.
   - **Where.** Each scope (organization or repo) is a separate directory `/srv/setup-server/github-runner/<scope>`
     (`bigsant`, `bigsant.<repo>`) and a separate service `setup-server-github-runner@<scope>` (one systemd template
     `setup-server-github-runner@.service`). For another organization or repo – the same script with another URL.
     The service runs as the `setup` account with the same `SETUP_SERVER_ROOT` and `SETUP_DIRECTORY` as Bitbucket's.
   - **Several runners for one scope.** One runner runs one job at a time; GitHub hands a queued job
     to any free runner with those labels. For parallel work –
     `--instance N --name <another name>` (N from 2 to 9): scope `<scope>.N`, directory
     `github-runner/<scope>.N`, service `setup-server-github-runner@<scope>.N`. The name is required and must differ:
     with the same name `--replace` would take over the first runner's registration, so an already registered name
     is rejected here. Metrics, status and the UI see the new directories by themselves.
   - **Run again** with an already registered scope (`.runner` exists) – it neither downloads nor registers,
     and does not ask for a token: it writes the unit and starts the service if it is not running. It does not restart a running one; if the
     unit changed it shows `sudo systemctl restart …` – run it when no job is running.
   - **Token** is never written or shown: `config.sh` (it refuses to run as root, so it is run as the
     `setup` account) gets it through the environment variable `ACTIONS_RUNNER_INPUT_TOKEN`, not on the command line.
     Once registered, the runner keeps its credentials in its own directory (0750; `.runner`, `.credentials`, the private key
     `.credentials_rsaparams` – 0600): the `setup` account reads them, and so do the jobs (like the Bitbucket OAuth credentials).
     There is no root file: the runner runs as the `setup` account and needs them itself.
   - **Updating.** The runner updates itself (GitHub's default; a runner not updated within 30 days of a new version
     no longer gets jobs from GitHub): it replaces `bin/` in its directory, and the service runs `bin/runsvc.sh`, so after a restart –
     the new version. The main process (`runsvc.sh`) stays the same, so the `bin/update` check is not affected.
   - **Why not GitHub's `svc.sh install setup`:** its unit name depends on the registration
     (`actions.runner.<…>.service`), and the pipelines need `SETUP_SERVER_ROOT`/`SETUP_DIRECTORY`. The own unit
     repeats the GitHub template (`KillMode=process`, `KillSignal=SIGTERM`, `TimeoutStopSec=5min` – a running job
     finishes cleanly) and adds `Restart=on-failure`.
   - **Why not `--ephemeral`:** such a runner does one job and unregisters, the next one needs a new
     registration (a token from the API with a PAT or a GitHub App). The build directories persist between jobs anyway.
   - **Access.** An organization runner goes into the *Default* runner group; by default the group's runners
     are used only by **private** repos. Do not allow a self-hosted runner for public repos: GitHub warns that anyone can
     open a pull request and run their code on this server.

   After a minute the runner must be **Idle** on GitHub. To check: `systemctl status setup-server-github-runner@bigsant`,
   `journalctl -u setup-server-github-runner@bigsant -n 50`. To remove – `sudo bin/install-runner --provider github
   --remove --scope <scope> [--check]` (or *Runners* in the UI): stops and disables the service, deletes
   `/srv/setup-server/github-runner/<scope>`. GitHub's *Runners → runner → Remove* command fed through stdin
   (`./config.sh remove --token …`, the token is valid for an hour) also unregisters the runner on GitHub; without it GitHub shows it as
   *Offline* until you remove it there. The command does not remove a runner that is running a job (its `Runner.Worker` process) (`75`);
   if GitHub refuses to unregister – the runner stays stopped, but nothing is deleted. The Bitbucket runner –
   `sudo bin/install-runner --remove`: the service, its unit, `runner.env` and `/srv/setup-server/runner`
   (remove it in Bitbucket yourself).

4. **Project:**

   ```bash
   sudo bin/add-project --target local git@ssh.bitbucket.org:example-team/alpha_shop.git
   sudo bin/add-project --target prod=ssh://deploy@shop.example/home/deploy/shop git@ssh.bitbucket.org:example-team/beta_docker.git git@ssh.bitbucket.org:example-team/beta.git
   ```

   The build directory is named after the repo (`/srv/setup-server/projects/alpha_shop`), so
   the pipeline finds it by itself: `BUILD_ROOT=${BUILD_ROOT:-$SETUP_SERVER_ROOT/projects/$BITBUCKET_REPO_SLUG}`
   (`SETUP_SERVER_ROOT` is passed by the runner service, `BITBUCKET_REPO_SLUG` – by Bitbucket). The repository
   variable `BUILD_ROOT` is needed only when another name is given (`add-project NAME URL`). Put the passwords
   into Deployment variables (Secured). Every pipeline step must have
   `runs-on: [self.hosted, linux.shell, setup.build]`, otherwise it would run in the Bitbucket cloud and use
   minutes. The step script must start with `unset DOCKER_HOST`: Bitbucket sets
   `DOCKER_HOST=tcp://localhost:2375` for its cloud docker service, while on a shell runner Docker is the server's
   own (a unix socket) – otherwise `docker` and `make release/deploy` do not find the daemon.

   **Where it deploys (the target)** is the server's knowledge, not the project's. `add-project` writes it into the build directory's
   `target.env` (setup 2.6.0+ adds it to `env/<env>.env` during every deploy), and the project's
   `env/*.env.example` no longer mentions `DEPLOY_CONTEXT`, `DEPLOY_SSH`, `DEPLOY_PATH`, `APP_UID`, `APP_GID` (setup
   rejects such an example):

   ```bash
   sudo bin/add-project --target local git@ssh.bitbucket.org:example-team/alpha_shop.git   # this server
   sudo bin/add-project --target prod=ssh://deploy@shop.example/home/deploy/shop git@ssh.bitbucket.org:example-team/beta_docker.git
   ```

   - **The target is always given:** a new project without `--target` is not created (nothing is cloned).
   - **`local`** – this server (`DEPLOY_CONTEXT=default`), the project root is the build directory,
     the containers run as the `setup` account (its `APP_UID`/`APP_GID`).
   - **`local:/PATH`** – this server, but the project root is in another directory (e.g. `local:/home/name/Projects/alpha`),
     not inside `SETUP_SERVER_ROOT` (checked after resolving symlinks).
     The script creates a missing directory for the `setup` account (0750); an existing one must be writable by it. Files and containers –
     belong to the `setup` account. The deploy copies the code there, and setup 2.7.0+ also the environment files the containers mount
     (`config/` etc.). If the files must belong to the directory owner, use `ssh://OWNER@localhost/PATH`
     (then the copying is done and the containers run under that user; it needs the `setup` key and the `docker` group).
   - **`ssh://USER@SERVER/PATH`** – another server: the `setup` account connects with its key
     (`/srv/setup-server/.ssh/id_ed25519`; if it cannot – the script shows the key that has to be put into
     `USER`'s `authorized_keys`, and stops), checks Docker and whether `PATH` is writable, takes over the user's
     `id -u`/`id -g` and creates the Docker context `setup-<name>[-<env>]`. A new host key is written
     on the first connection (it is shown).
   - **`files:ssh://USER@SERVER/PATH`** – a server **without Docker** (a client's server): this worker still builds the release
     in its Docker build container (and may test it there), but a deploy copies only the code there (rsync over SSH, the
     account's key, as for `ssh://`); nothing runs in containers there. The script logs in as `USER`, checks that `PATH`
     exists (or creates it) and is writable and that `rsync` is installed there – no Docker check, no Docker context, no
     `APP_UID`/`APP_GID` (the files belong to `USER`). It writes:

     ```
     DEPLOY_MODE=files
     DEPLOY_SSH=USER@SERVER
     DEPLOY_PATH=/PATH
     ```

     **`files:local:/PATH`** – the same into a directory of this server (`DEPLOY_MODE=files` and `DEPLOY_PATH` only; created for the
     `setup` account like `local:/PATH`, not the build directory; `rsync` must be installed here), handy for testing. A files
     target has no database managed here: no DB backups, no secret store (see "DB backups"); its files backups (`BACKUP_FILE_PATHS`)
     work as for any target. Changing a target from `ssh://` to `files:ssh://` removes the stale Docker context `setup-<name>[-<env>]`
     (`--check` says it would); registering `ssh://` again creates it as before. What setup skips for such a target is in its own
     documentation.
   - **`stage=…` / `prod=…` / `demo=…`** – one environment only (`target.<env>.env`, wins over the shared `target.env`),
     when stage and prod are on different servers, or for an extra environment (see "Extra environments"). `--target` can be repeated.
   - **Stage and prod on the same server – each in its own directory:** `--target stage=local:/PATH/stage --target
     prod=local:/PATH/prod`. With a single `local` both environments use the build directory, and its containers mount the environment
     files (`config/` etc.) directly from it: every stage deploy switches the build directory to its own commit, so
     prod would pick up the stage commit's files. With separate directories each environment gets its own copy of the deployed commit.
   - Run again without `--target` it does not change the target (neither `target.env` nor `target.<env>.env`); with `--target` –
     it overwrites it. `remove-project` removes both the build directory (with `target*.env`) and the project's Docker contexts.
   - **One environment's own target goes** with `sudo bin/add-project [--check] --remove-target ENV --name NAME` (or
     `--remove-target ENV NAME`; no URLs, not with `--target`/`--shared-key`/`--monorepo`): it deletes `target.<env>.env`
     (and the Docker context `setup-<name>-<env>` of a remote one), and the environment then deploys to the shared `target.env`.
     The shared `target.env` is never removed this way, and the command refuses when there is no shared `target.env` (the environment
     would have no target – register another with `--target ENV=…` first), when the environment has no file of its own, or when it is
     a symlink. `--check` shows the file with its `DEPLOY_*` values and the shared target that takes over; the UI's project page offers it too.

   **GitHub project:**

   ```bash
   sudo bin/add-project --target local git@github.com:BigSant/<environment-repo>.git [git@github.com:BigSant/<code-repo>.git]
   ```

   A GitHub deploy key works for **only one** repo, and the account key `id_ed25519` is already the setup repo's deploy key. So
   every GitHub SSH URL gets its own key `/srv/setup-server/.ssh/github-<owner>.<repo>` and a host alias
   `github.com-<owner>.<repo>` in the account's `.ssh/config` (`IdentitiesOnly yes`, the same `github.com`
   host key is checked), and the clone goes through it. The first time the script creates the key, shows it and the link
   `https://github.com/<owner>/<repo>/settings/keys` and stops (code 1): add it (*Add deploy key*,
   "Allow write access" **not enabled**) and run the same command again. If there is a "machine user" account whose
   key (the account's `id_ed25519`) reads all repos – `--shared-key`: the clone uses it, through the given URL.

   The build directory is the repo name in lowercase, so the workflow finds it by itself:
   `BUILD_ROOT=${BUILD_ROOT:-$SETUP_SERVER_ROOT/projects/<repo name in lowercase>}` (in bash:
   `repository=${GITHUB_REPOSITORY#*/}` and `${repository,,}`; `SETUP_SERVER_ROOT` is passed by the runner service).
   Under another name – the Actions variable `BUILD_ROOT`. Passwords – GitHub environment secrets. The job must have
   `runs-on: [self-hosted, linux, setup.build]`. `unset DOCKER_HOST` is not needed: GitHub does not set it.

   **A repo name with a dot** (`gamma.example`) is not a valid NAME (only `a-z`, `0-9`, `_`, `-`): the script stops and
   suggests a name. Give it as `--name gamma` (or `gamma` before the URL). The build directory then does not match the repo
   name: the workflow writes it itself (`${SETUP_SERVER_ROOT}/projects/gamma`) or gets it through the Actions variable `BUILD_ROOT`.

   **A monorepo GitHub project** – one repo containing both the DDE environment `app/` and the code `app/public/` (e.g.
   `example-team/gamma.example`). On this server (`SETUP_SERVER_ROOT=/home/tomas/Projects/runtime/setup-server`, account
   `tomas`):

   ```bash
   sudo SETUP_SERVER_ROOT=/home/tomas/Projects/runtime/setup-server SETUP_SERVER_USER=tomas \
       bin/add-project --check --monorepo \
       --target stage=local:/home/tomas/Projects/runtime/gamma/stage \
       --target prod=local:/home/tomas/Projects/runtime/gamma/prod \
       --name gamma git@github.com:example-team/gamma.example.git
   # then the same without --check: the first time it stops at the deploy key (see above), after adding it – once more
   ```

   - `--monorepo` clones the repo **as the build directory** (`projects/gamma/`), not into `app/` (otherwise it would be `app/app/`).
     The repo must have `app/Makefile`; any other repo is rejected and nothing is left behind. No code repo URL is given – the code is already in
     `app/public/`. `--check` shows the clone and the targets without changing anything.
   - **The target – its own `local:/PATH` (or `ssh://`) for each environment.** `local` (the build directory itself) is
     rejected with `--monorepo`: the build directory is a repo clone that every deploy switches to its own commit.
     So with `--monorepo`:
     - every `--target` has the environment in front (`stage=`, `prod=`); a shared `target.env` is not accepted;
     - the paths are compared the way the deploy will reach them: symlinks are resolved (a part of the path that does not exist yet – from
       the nearest existing parent directory). A directory inside the build directory or inside `SETUP_SERVER_ROOT` (also through a symlink) is
       rejected;
     - two environments cannot deploy to the same place or inside each other – this is also checked against already written
       `target.<env>.env`.

     The script creates missing directories for the `SETUP_SERVER_USER` account (0750), only after checking all targets.
   - `target.stage.env` and `target.prod.env` lie in the clone's root; they are written into the clone's `.git/info/exclude`, so
     `git status` stays clean. A repo whose root tracks (`git`) `target.env` or `target.*.env` is rejected: a checkout
     would overwrite them with the repo's tracked files.
   - Run again, the clone is kept (the same `origin` and `app/Makefile` are checked). A build directory with
     separate repos (`app/.git`) is not turned into a monorepo (first `bin/remove-project`), and a monorepo clone is not
     accepted without `--monorepo`.
   - Then in the project: `app/env/stage.env.example`, `app/env/prod.env.example` (without `DEPLOY_*`), GitHub environments
     `staging`/`production` with secrets and a runner for this repo (`bin/install-runner --provider github`, scope
     `example-team.gamma.example`). Backups, metrics, the overview and `remove-project` read a monorepo build directory like any
     other (the environment – `app/`).

   **example-app deployments** (example-app's `docs/development-delivery-contract.md`) go only through GitHub: example-app runs the project's
   `deploy.yml` (`workflow_dispatch` with `operation` `deploy|status`, `environment` `stage|prod`, `sha`, `attempt_id`,
   `request_id`, `expected_live`, `health_paths`; name `example-app <operation> <environment> <request_id>`), the job on this
   server's runner runs `make -C <build directory>/app pipeline-deploy`, and returns the status as the artifact
   `example-app-<request_id>` (`dde-deploy-status/v1`, setup's `make deploy-status`). Server OPS gives it only the build
   directory, the targets and the runner; example-app gets no server, SSH or Docker access.

5. **Removing a project:**

   ```bash
   ls /srv/setup-server/projects/
   sudo bin/remove-project --check alpha_shop    # shows the size, backups, deploy history
   sudo bin/remove-project alpha_shop
   ```

   If the directory holds DB backups or deploy history (`app/.generated` or in the older layout
   `app/docker/.generated`), `--with-backups` is needed – then the backups and the rollback are lost. The script does not delete
   a directory used by a running container.

6. **DB backups: nightly and weekly full** (the worker, not Bitbucket):

   ```bash
   sudo bin/install-backups --check          # units and what it would back up tonight; changes nothing
   sudo bin/install-backups                  # installs; --at 03:30 and --full-at "Sun 04:30" – the times (defaults)
   ```

   | Backup | Timer | What it contains | Files | Kept |
   | --- | --- | --- | --- | --- |
   | Nightly | `setup-server-backup.timer`, daily `--at` | according to the table registry: the structure of all tables, the data of `import` tables | `worker-<env>-…Z.sql.gz` | 7 days + 4 weeks |
   | Weekly full | `setup-server-backup-full.timer`, once a week `--full-at` | the data of all tables (`make db-backup full=1`) | `full-<env>-…Z.sql.gz` | the 2 newest |
   | "Fresh" | a developer's `make db-pull fresh=1 [full=1]` (see item 7) | registry or full | `fresh-<env>-…Z`, `fresh-full-<env>-…Z` | the 2 newest of each |

   Each series is rotated separately, rotation does not touch other files. One backup at a time is made for a project and
   environment (lock `.generated/backups/.lock-<env>`): if the nightly one is still running, the weekly or "fresh" one
   waits for it. A full backup may take up to 8 h, the others – up to 4. In a terminal (`sudo setup-server-backup --full
   alpha_shop prod`) a bar is shown: the bytes written, compared with the size of the previous backup of the same kind.

   **Which projects and environments.** Every environment of `/srv/setup-server/projects/<name>` is backed up
   for which the environment repo (`app/`, in the older layout `app/docker/`) has `env/stage.env`, `env/prod.env` or
   an extra environment's (`STAGE_ENVIRONMENTS`, see "Extra environments") `env/<env>.env`.
   This file is created by the project's deploy pipeline (`cp env/prod.env.example env/prod.env`), so what is backed up is
   what is deployed from this server. `local` and `test` are not backed up. The backup is setup's `make db-backup
   ENV=<env>`: stage/prod according to the table registry, with `DATABASE_EXTRA_SCHEMAS` – all schemas in one dump
   (alpha.local). Only the database is backed up: `data/` directories (search indexes and the like) are not backed up,
   and which schemas and tables go into the dump is decided by the project's `DATABASE_EXTRA_SCHEMAS` and the registry.

   **A files deploy target has no database here.** An environment whose target is `files:…` (`DEPLOY_MODE=files` in its own
   `target.<env>.env`, else in `target.env`) has no DB series: the nightly, full and fresh runs say `==> NAME ENV: skipped: files
   deploy target (no database here)` and exit 0 (a "backup now" of only such environments too), no secret store is looked
   for, and `bin/worker-metrics` expects no DB backups of it (its DB policy intervals read 0). Its files backups are made as
   for any other environment.

   **Passwords live only in Bitbucket.** The server has no copy of them. The timer runs without Bitbucket, so
   `make db-backup` (account `setup`) takes them from the project's stack: setup 2.5.0+ keeps in every stage/prod
   environment a service `<PROJECT_NAME>-<env>-secrets`, which every deploy fills with the Bitbucket
   Deployment variables values (setup `doc/lt/09-aplinkos.md`, the section on commands on the server without a pipeline).
   After a password is changed in Bitbucket, the backup uses the new one after the next deploy – nothing else needs changing.
   A project not yet deployed with setup 2.5.0+ (no store) will not be backed up: `--check` and
   the log name it (`no secret store …`). With `DEPLOY_CONTEXT` the store is looked up through it (the `setup`
   account's Docker contexts). The old `/etc/setup-server/projects/<name>/<env>.env` is no longer used –
   `--check` (with sudo) reminds you to delete it.

   **Backup policy: how often, DB full only, or none.** The table above is the default. Per project environment three
   series can each get a frequency of their own (both DB series are database backups; they differ in what they hold):

   | Series | What | Frequencies | Default |
   | --- | --- | --- | --- |
   | `db-short` | the DB short backup (by the table registry: every table's structure, the data of the import tables), nightly, `worker-<env>-…Z.sql.gz` | `daily` `weekly` `off` | `daily` |
   | `db-full` | the DB full backup (every table's data), `full-<env>-…Z.sql.gz` | `daily` `weekly` `monthly` `off` | `weekly` |
   | `files` | the files backup, `files-<env>-…Z.tar.gz` (see "File backups") | `daily` `weekly` `off` | `daily` |

   ```bash
   sudo /usr/local/lib/setup-server/bin/backup-projects --set alpha_shop prod db-short=off db-full=daily --check   # shows the change
   sudo /usr/local/lib/setup-server/bin/backup-projects --set alpha_shop prod db-short=off db-full=daily           # DB full only, every night
   sudo /usr/local/lib/setup-server/bin/backup-projects --set beta stage db-short=off db-full=off files=off          # no backups
   sudo /usr/local/lib/setup-server/bin/backup-projects --set beta stage db-short=default db-full=default           # back to the default
   ```

   (`bin/backup-projects` of a clone works the same way; the web UI's project page sets it too.) Series not given keep
   their value, `SERIES=default` drops back to the default. It prints `alpha_shop prod: db-short daily → off, db-full
   weekly → daily` (or `no change`) and, without `--check`, `written to /etc/setup-server/backup-policy.conf`. An unknown project
   or an environment the project does not have (stage, prod, its `STAGE_ENVIRONMENTS`, a `target.<env>.env`) is
   refused (exit 65), a bad series or frequency is a usage error (exit 64). The policy lives in one root-owned file,
   `/etc/setup-server/backup-policy.conf` (0644), not in a build root: a pipeline cannot switch its backups off. The
   directory is 0711 (others may pass through, not list it; its secrets stay 0600 files): the metrics and the status page
   read the policy as the `setup` account. The installers create it so and `--set` adds the pass-through to an older
   0700 one. One line
   per environment that differs from the default, `#` comments, e.g. `alpha_shop prod db-short=off db-full=daily`; `--set`
   rewrites the file atomically, keeps the other lines and comments, and drops a line that is back at the default.
   Unknown or invalid items are ignored (the default applies: backing up rather than skipping), lines of removed
   projects are ignored.

   Only the timers follow it. A series is backed up when it is due: `daily` – every run; `weekly` – when the newest
   backup of that series (a fresh one does not count) is older than 6.5 days or there is none; `monthly` (every 4 weeks)
   – older than 27.5 days; `off` – never. The nightly run makes the DB short backup when due and, for `db-full=daily`,
   also the DB full one (so "DB full only, daily" needs no other timer); the weekly run backs up `db-full=weekly`,
   `db-full=monthly` when due, and skips `daily` (`skipped: db-full daily (the nightly run makes it)`) and `off`; the
   files run follows `files`. With `db-full=daily` the full series is rotated like the nightly one (7 days + 4 weeks)
   instead of keeping the 2 newest. `--check` shows per environment what the run would skip: `==> alpha_shop prod:
   skipped: db-short off`, `==> beta prod: not due: db-full monthly, newest 3 d ago`. **"Backup now"** (a project given: the pipeline, the web UI) and **fresh** backups
   (`bin/backup-pull`) ignore the policy – a person asked for them. Switching a series off deletes nothing at once: its
   kept backups stay until a later backup of that series rotates them.

   **Rotation.** For each project and environment, the newest backup of each of the last 7 days
   that have a backup is kept, plus the newest backups of 4 more weeks (ISO week) – up to 11 in total, roughly
   5 weeks back. Older ones are deleted only after a successful new backup; on failure nothing is deleted.
   Only `worker-<env>-…Z.sql.gz` files are touched: deploy backups (`<project>-<env>-….sql.gz`) and other
   files stay (setup's `make db-backup-prune` cleans those). Backups are kept only on this server's disk.

   **Errors.** One project's error does not stop the others; at the end `==> Backups: N ok, M failed: project/env`,
   and the service ends with an error (`failed`). To check:

   ```bash
   systemctl list-timers 'setup-server-backup*'          # when the next nightly and weekly runs are
   sudo systemctl start setup-server-backup.service     # nightly now (waits for it to finish)
   sudo systemctl start setup-server-backup-full.service  # weekly full now
   systemctl status setup-server-backup.service         # status of the last run
   journalctl -u setup-server-backup.service -n 100     # what was backed up, what failed (-full – weekly)
   sudo setup-server-backup --check                     # what it would back up (and what the policy skips), whether each project has a secret store
   sudo setup-server-backup --check --full              # the same for the weekly full backup
   ```

   **"Backup now"** – a Bitbucket custom pipeline run by the runner on this server. It calls the same
   command through `sudo`, so it uses the same secret store and rotation, and creates no entry in
   the Deployments history; the backup policy does not apply to it. The sudo rule allows the `setup` account only this form:
   `/usr/local/sbin/setup-server-backup [--full] /srv/setup-server/projects/<name> [stage|prod]` (sudo 1.9.10+,
   e.g. Ubuntu 24.04, checks it with a regular expression). An older sudo (Ubuntu 22.04: 1.9.9) has only
   wildcards: the first argument must be the build directory (or `--full` and the build directory), and other
   arguments are rejected by the command itself.
   After updating Server OPS, repeat `sudo bin/install-backups` if `bin/update` shows a note that
   the sudo rule differs.

   ```yaml
   pipelines:
     custom:
       backup-now:            # "backup now"
         - variables:
             - name: FULL
               default: "no"
               allowed-values: ["no", "yes"]
         - step:
             name: Backup now
             runs-on: [self.hosted, linux.shell, setup.build]
             script:
               - unset DOCKER_HOST
               - BUILD_ROOT=${BUILD_ROOT:-${SETUP_SERVER_ROOT:-/srv/setup-server}/projects/$BITBUCKET_REPO_SLUG}
               - sudo -n /usr/local/sbin/setup-server-backup $([ "${FULL:-no}" = yes ] && echo --full) "$BUILD_ROOT"
   ```

   The second argument (`stage` or `prod`) limits the environment, `FULL=yes` – a full backup (into the `full-<env>` series).
   Code tree files that are not in git (e.g. translations) are kept by separate file backups – see "File backups".
   The installed copy of the command is updated by `bin/update` (see "Updating"). Run again,
   `sudo bin/install-backups` keeps the installed times unless `--at` and `--full-at` are given.

7. **Backup downloads for developers (optional).** On their computer, a developer uses the setup command
   `make db-pull` to download the newest project backup saved by this server (nightly, "backup now" or
   pre-deploy – whichever is newest; with `full=1` – the weekly full one) and sets up a working environment from it.
   With `fresh=1` the server makes the backup at that moment. LIVE is not reachable.

   First the server must accept SSH from the VPN. Ubuntu Desktop has no `openssh-server`, and then
   nothing listens on port 22 (`ssh: connect to host … port 22: Connection refused`):

   ```bash
   bin/install-ssh --check --from 10.8.0.0/24   # package, sshd settings, service, ufw; changes nothing
   sudo bin/install-ssh --from 10.8.0.0/24      # installs; later sudo bin/install-ssh is enough (the networks are kept)
   ```

   - **Settings** – `/etc/ssh/sshd_config.d/10-setup-server.conf` (written before the package is installed, so a new
     sshd never starts without it): from outside the `--from` networks and not from this server – key only
     (`PasswordAuthentication no`, `KbdInteractiveAuthentication no`). Inside the VPN the server's own settings apply.
     sshd takes the first value it reads, so the file name sorts before the others (`50-cloud-init.conf` and the like).
     If `sshd -t` does not accept it, the previous file is restored. The script does not change ports, `ListenAddress` or other files.
   - **Service** – on Ubuntu 22.10+ `ssh.socket` (sshd starts on the first connection), on older ones `ssh.service`;
     enabled so it also runs after a reboot. When the settings change, a running service is reloaded with `reload`
     (open sessions stay).
   - **It checks** whether sshd listens on all addresses or on the VPN address; if only on others (`ListenAddress`) – this is shown.
   - **ufw** – if enabled, `ufw allow proto tcp from 10.8.0.0/24 to any port 22` is added; a disabled one is left
     disabled (the script run again will add the rule once ufw is enabled).
   - At the end the server's ED25519 host key fingerprint is shown: when connecting for the first time, the developer
     compares it before answering `yes`.

   Then – the developer's key:

   ```bash
   sudo bin/add-backup-key ~/tomas.pub --name tomas --from 10.8.0.0/24 --pull shop=db --check   # shows what it would change
   sudo bin/add-backup-key ~/tomas.pub --name tomas --from 10.8.0.0/24 --pull shop=db           # shop stage and prod backups
   sudo bin/add-backup-key --name tomas --pull shop:prod=fresh --pull beta:stage=db         # an existing one – just the name
   sudo bin/add-backup-key --name qa --pull-all db                    # backups of all projects (including new ones)
   sudo bin/add-backup-key --list          # tomas  from 10.8.0.0/24  backups all=none beta:stage=db shop:prod=fresh
   sudo bin/add-backup-key --remove tomas                                        # revokes the permission
   ```

   **Backup permissions** – for each project environment separately, like access (step 8): `db` – download backups
   (registry and full, `make db-pull [full=1]`), `fresh` – also order a new one now (`make db-pull fresh=1`; loads
   the project's DB). The web UI calls them "download" and "download + fresh". `--pull PROJECT[:ENV]=LEVEL` (without `:ENV` – stage and prod, repeated or comma-separated), `--pull-all
   none|db|fresh` – all projects, including new ones; a project environment's permission can only raise it. A new key without them
   gets no backups. For an existing key, whatever is not given stays (the key, networks, the `--pull` and `--grant` lists, `--pull-all`);
   `--no-pulls`, `--no-grants` clear the list. Most convenient – the UI's *Access* → key → the "Backups" card (in bulk
   and per project). **Keys written before these permissions existed** (without `--pull-all`) keep pulling backups of all projects
   (`--allow-fresh` – fresh ones too) until they are changed for the first time: then this is written explicitly (`--pull-all db|fresh`).

   The key (the developer's `.pub` file, never the private one) is written into the `setup` account's
   `.ssh/authorized_keys` with restrictions: `from="10.8.0.0/24"` – connecting is possible only from the given network (VPN;
   `0.0.0.0/0` is rejected), `command="/usr/local/sbin/setup-server-backup-pull --key tomas --pull-all none --pull
   shop:prod=db …"` – the key runs only this command and only for allowed environments, `restrict` – no terminal, port or
   agent forwarding. The key gets no shell, Docker or other files. The same name again – the key is replaced;
   other `authorized_keys` entries are not changed. Login is by key only (the `setup` account has no password). A request for a
   disallowed environment is rejected: `this key may not download backups of PROJECT ENV`.

   The command understands:

   | Request | Response |
   | --- | --- |
   | `latest <project> <stage\|prod> [full\|files]` | the newest backup's name: without `full` – a registry backup (nightly, "fresh", pre-deploy), with `full` – a full one (weekly or "fresh" full), with `files` – a file backup (`files-<env>-…Z.tar.gz`, "File backups") |
   | `info <project> <stage\|prod> [full\|files]` | the same plus the size in bytes: `<file> <size>` (for download progress) |
   | `get <project> <stage\|prod> <file>` | the contents of that backup |
   | `fresh <project> <stage\|prod> [full\|files]` | only for a key with the `fresh` permission for that environment: runs `setup-server-backup --fresh [--full\|--files]` as the `setup` account, sends progress to stderr (`progress backup BYTES EXPECTED`), at the end – `<file> <size>` |

   File backups (setup `make files-pull`) are guarded by the same permissions: `db` – download, `fresh` – order a new one.
   "Fresh" loads the project's DB, so it is not done twice: if a backup of the same kind (registry, full or files)
   is younger than 30 min., that one is returned; if a backup is being made at that moment, it waits for it to finish (lock). It needs
   the installed `/usr/local/sbin/setup-server-backup` (`sudo bin/install-backups`). Every request (including rejected ones)
   is written to `/srv/setup-server/backup-pulls.log`: time, key name, address, request, result. The project
   name is the build directory name (`projects/<name>`, e.g. `alpha_shop`).

   The backups contain real customer data (stage/prod according to the table registry): allow them only to those who need them,
   and revoke the permission (`--remove`) when it is no longer needed. The installed copy of the command is updated by `bin/update`.

8. **Access to targets through this server (optional).** This server already reaches every project target:
   the deploy connects to `DEPLOY_SSH` with its key, and manages the containers through a Docker context. A developer can go
   the same way: their key is not needed on the LIVE server, and they need no direct access to LIVE. The same
   key as for backups gets permissions for each project and environment:

   ```bash
   sudo bin/add-backup-key ~/tomas.pub --name tomas --from 10.8.0.0/24 \
       --grant beta:prod=phpfpm-readonly --grant beta:stage=docker --check     # shows what it would change
   sudo bin/add-backup-key ~/tomas.pub --name tomas --from 10.8.0.0/24 \
       --grant beta:prod=phpfpm-readonly --grant beta:stage=docker             # writes it
   sudo bin/add-backup-key ~/tomas.pub --name tomas --from 10.8.0.0/24 --grant alpha_shop=full   # both environments
   sudo bin/add-backup-key --list          # tomas  from 10.8.0.0/24  access beta:prod=phpfpm-readonly …
   ```

   `--grant` can be repeated or comma-separated; without `:stage`/`:prod` – both environments. Run again with the same
   name, the whole entry is replaced: list all permissions (without `--grant` – only backups remain). For an existing key
   the key file and `--from` are not needed – the written key, networks and backup permissions stay: `sudo bin/add-backup-key --name
   tomas --grant beta=docker` changes only the access permissions. The project is the build directory name (`projects/<name>`);
   for a project that does not exist yet the permission is written with a note. Most convenient – the UI's *Access* page (see "Web UI").

   | Level | What it allows | What it does not allow |
   | --- | --- | --- |
   | `phpfpm-readonly` | a shell and commands in the environment's php-fpm container as the user `nobody` (65534) | writing to the project user's files. **Files only:** the DB password remains in the container environment and in the shop configuration (`parameters.php`), so the database can still be changed |
   | `phpfpm` | the same as the project user (`APP_UID`, the file owner) or `root` in the container | other containers, the target |
   | `docker` | the project's containers: `ps`, `logs`, `exec` (and `root`), `start`, `stop`, `restart`, `top`, `stats`, `inspect` | `docker run`, `exec --privileged`, images, volumes, other projects' containers and `exec` into a container with the Docker socket (`metrics-docker`, `metrics-logs`: that would be root on the server) |
   | `full` | a target shell: the `DEPLOY_SSH` user on the target, in the `DEPLOY_PATH` directory | – (as much as that user can). **For a `--target local` project this is the `setup` account on this server:** all projects, their secrets and Docker, i.e. root |

   Each level includes the lower ones. A key with permissions is written as
   `from="10.8.0.0/24",command="/usr/local/sbin/setup-server-access --key tomas --grant beta:prod=phpfpm-readonly …",restrict,pty`:
   only from the VPN, only this command, with a terminal (`pty`), but without port, agent and X11 forwarding, so neither `ProxyJump`
   nor a tunnel to LIVE works. Backup requests (`latest`, `info`, `get`, `fresh`) are passed by the command to
   `setup-server-backup-pull`, so `make db-pull` works as before.

   On the developer's computer – setup's `make ssh` (the same server and key as `make db-pull`) or directly:

   | Request (`ssh -t setup@10.8.0.14 …`) | What it does |
   | --- | --- |
   | `access` | where the key may connect: `PROJECT ENV LEVEL` lines |
   | `shell PROJECT ENV [root\|readonly]` | a shell in the php-fpm container (for a key with only `phpfpm-readonly` – always `readonly`) |
   | `exec PROJECT ENV [root\|readonly] -- COMMAND …` | one command there |
   | `host PROJECT ENV [-- COMMAND …]` | `full`: a shell (or a command) on the target, in the `DEPLOY_PATH` directory |
   | `docker PROJECT ENV ps [-a]` | the project's containers |
   | `docker PROJECT ENV logs [--tail N] [-f] [--since TIME] [-t] CONTAINER` | the log |
   | `docker PROJECT ENV exec [--root\|-u USER] [-w DIRECTORY] CONTAINER [COMMAND …]` | a shell or a command in the container |
   | `docker PROJECT ENV start\|stop\|restart CONTAINER …`, `top`, `stats`, `inspect` | management |

   A container is a name (`beta-prod-php-fpm-1`) or a service (`php-fpm`, `database`). The target, the Docker context and
   `APP_UID` are taken from the build directory (`target*.env`, `env/<env>.env`), like the deploy's. The command gives a terminal when
   the SSH session has one (`ssh -t`); without it – stdin is passed through, so `… exec … -- mysqldump … > file` works too.
   Every request (including rejected ones) and its end (exit code) are written to `/srv/setup-server/access.log`: time,
   key name, address, request, level and the command run. **An interactive session's** (with a terminal) input – what
   the person typed, not what the screen showed – is recorded with `script --log-in` into
   `/srv/setup-server/access-sessions/<key>/<UTC time>-<pid>.log` (account `setup`, 0600, kept for 180 days), and
   the `access.log` line names the file (`session=…`). This way the commands typed in a shell are known too; a
   password typed in them (e.g. `mysql -p`) is recorded as well. Without a terminal there is no recording: the command is already in the request. The UI shows
   each key's activity (logins, backups, shells with their duration, commands, rejected attempts) and the session recordings –
   `server-status --activity [KEY]`, `--session KEY FILE`. The installed copy of the command is updated by `bin/update`.

## File backups (`BACKUP_FILE_PATHS`)

Some code tree files are written by the shop itself on the target and are not in git (e.g. module translations
`modules/*/translations/`). The deploy does not delete them, but they exist nowhere else either: if the target broke they would be lost. The project
lists them in the setup variable `BACKUP_FILE_PATHS` (like `DEPLOY_PROTECTED_PATHS`: paths from the code root, space-separated, a directory
ends with `/`, `*` is allowed), and the worker downloads them every night and keeps backups. For a PrestaShop project (`PROFILE=prestashop`)
nothing needs to be set: the setup 4.0.0+ profile always backs up the translations (`modules/*/translations/`,
`themes/*/modules/*/translations/` and the language packs `translations/*-*/`; setup 5.3.0+ – also the mail templates `mails/*/lang.php`,
`themes/*/mails/*/lang.php`), and `BACKUP_FILE_PATHS` adds the project's paths:

```dotenv
# env/common.env (the env/<env>.env value wins)
BACKUP_FILE_PATHS="modules/*/translations/ themes/*/lang/"
```

```bash
sudo bin/install-backups --check                  # among other things – setup-server-backup-files.timer and what it would back up tonight
sudo bin/install-backups [--files-at 02:30]       # default – the same time as the nightly DB backup (--at)
sudo setup-server-backup --check --files          # which projects' environments and which paths
sudo setup-server-backup --files shop prod        # a backup of one environment now (or "Files backup now" in the UI)
```

- **Which environments** – the same ones whose DB is backed up (`env/<env>.env` in the build directory), if the project is PrestaShop or
  `BACKUP_FILE_PATHS` is not empty for them. The nightly run of other projects does not change. A PrestaShop project without `BACKUP_FILE_PATHS`
  needs setup 4.0.0+: an older setup does not back up such a project (`BACKUP_FILE_PATHS is empty`).
- **How** – setup's `make files-backup ENV=<env> file=…` (setup 3.1.0+; older – "setup has no make files-backup"; from 2.x – a new MAJOR,
  `sudo bin/update-setup v3.1.0`), account `setup`, the same way as the deploy (`DEPLOY_SSH` with its key or a local directory). rsync
  downloads only the listed paths into the mirror `.generated/backups/.files-<env>/` (the next night – only the changes), from which
  `files-<env>-<UTC time>Z.tar.gz` is packed. **The target is only read**: nothing is written to it and nothing is restored
  from there.
- **Rotation** – like the nightly DB backups: the newest of the last 7 days and 4 weeks (up to 11); older ones are deleted only after
  a successful new one. It does not touch the DB backups, and the DB backups do not touch it. Its own lock (`.lock-files-<env>`): it does not wait for DB backups.
- **No passwords are needed** (the secret store is not read). Errors – like the DB backups: one environment's error does not stop the others,
  the service ends `failed`, `journalctl -u setup-server-backup-files.service`.
- **For developers** – setup's `make files-pull [from=prod] [fresh=1] [extract=1]` downloads the newest (with `fresh=1` – just
  made) file backup through the same key and permissions as `make db-pull` (see "Steps", step 7): `db` – download,
  `fresh` – order a new one (one younger than 30 min. is returned as is).
- **Viewing** – the UI's *Files*: the environments with file backups (PrestaShop or `BACKUP_FILE_PATHS`), their backups, a backup's files (new, changed and missing
  since the previous backup), a file's contents, the differences from the previous backup and a download (up to 2 MiB). Read-only.
- **Metrics** – `setup_worker_files_backup*` (see "Worker metrics"). After updating Server OPS, `bin/update` shows that
  the timer does not exist yet (`note: … differs`) – run `sudo bin/install-backups`.

## Tool access (`bin/tool-access`)

The projects' tools (`pma.`, `cron.`, `mailpit.`, `backup.`, `metrics.<domain>` and the project's `PROXY_TOOLS`) on stage/prod
let in only from an IP list. Until now every project kept that list and the htpasswd lines in its own Deployment variables:
the same VPN address had to be repeated everywhere, and revoking a person's access meant changing and deploying every project.
Now this is managed here, in one place (setup 3.6.0+):

- **Addresses** – this server's addresses that the targets let in, e.g. the VPN server's public IP (developer
  traffic to the targets goes through it). They are added to every tool's IP list, but not to the website's.
- **Users** – the developer key names (`bin/add-backup-key`). For each one the administrator sets a tools
  password and marks which project environments' tools they may open. The tool asks for a name and password on top of the IP
  rule. If the project has its own `<TOOL>_HTPASSWD` for a tool, both lists let in (setup 3.8.0+; an older setup
  then uses only the project's list); a name present in both is checked against the project's password.
- **Person** – the key owner's first and last name (`--full-name`), which the UI shows instead of the key name (in the list,
  on the key page, in the activity; search finds by it too), and the login name for the tools (`--login`): a name or e-mail
  address the person chooses, instead of the key name. The login name is Latin letters, digits,
  `. _ @ + -` (no spaces or colon: that way all browsers send it the same), unique regardless of case
  and different from any other key's name; the tools are case-sensitive. An empty value returns to the key name. In the logs
  and in `authorized_keys` the key stays the key.
- **Immediately.** After every change the command writes `projects/<name>/tool-access/<env>/users.htpasswd` and
  `allowed-ips` and runs `make tool-access ENV=<env>` there: setup copies them to the target's `data/<env>/tool-access/`
  and reloads nginx-proxy. Granting or revoking takes effect within a few seconds, without a deploy; every deploy brings them
  again. For an environment not yet deployed the files wait for the first deploy.
- **When the last permission is revoked** the environment's file stays empty: the tools let nobody in, rather than falling back to the IP
  list alone. Environments that nobody ever got are not changed.

```bash
sudo bin/tool-access --allowed-ips 198.51.100.7                 # the VPN server's public IP
sudo bin/tool-access --name tomas --password - < password.txt    # 12+ characters, via stdin; only the hash is stored
sudo bin/tool-access --name tomas --tools shop:prod,blog         # without :ENV – stage and prod; the list is replaced
sudo bin/tool-access --name tomas --no-tools                     # revokes all
sudo bin/tool-access --name jane --full-name 'Jane Doe' --login jane@example.com  # the person; '' – none / the key name
sudo bin/tool-access --apply                                     # sends all files again (e.g. after an error)
sudo bin/tool-access --show                                      # the state as JSON, without hashes
```

- State – `/etc/setup-server/tool-access.json` (root, 0600): addresses, user password hashes (crypt SHA-512,
  `openssl passwd -6`), allowed environments, names and login names, the result of the last send. The password is never written anywhere.
- `bin/add-backup-key --remove NAME` removes the user too: their lines disappear from all targets at once. A new key's
  name cannot be another key's login name.
- After updating Server OPS the UI needs a new sudo rule (`tool-access`): `sudo bin/install-ui`. The shared setup
  must be 3.6.0+ (`sudo bin/update-setup`); while it is older, the files wait in the build directories – after updating,
  `sudo bin/tool-access --apply`.
- In the UI – the key page's *Tools* tab (password and environments) and *Access* → "Tool access" (addresses,
  failed sends, "Send again").
- Target DNS and TLS: the tool subdomains can be a single wildcard record `*.<domain>` and a wildcard certificate
  `*.<domain>` – then the individual tool names do not show up in the Certificate Transparency logs. Protection comes not from the names but from the IP list
  and the password.

## Extra environments (e.g. a temporary `demo`)

For a bigger piece of work a project can have more environments than stage and prod. The project's `env/common.env` lists them:
`STAGE_ENVIRONMENTS="demo"` (setup; the name is lowercase letters and digits, 2–16 characters, not `local`/`test`/`stage`/`prod`).
Each such environment behaves like stage in setup (see setup `doc/lt/09-aplinkos.md`), and on this server – like any
other environment:

- **Target** – the shared `target.env` or its own: `sudo bin/add-project --target demo=ssh://deploy@demo.example/home/deploy/shop NAME URL`
  (`target.demo.env`). On the same target as stage or prod the environment needs its own ports or address (setup checks this).
- **Deploy** – the project's `bitbucket-pipelines.yml` custom pipeline `deploy-demo` with `deployment: demo` (Bitbucket
  deployment environment `demo`, type Staging) or the GitHub environment `demo`; on the project page in the UI – "Deploy to demo"
  (shown once the environment is listed, even before the first deploy).
- **Backups** – nightly, weekly full and "backup now" (`… setup-server-backup "$BUILD_ROOT" demo`), when
  `env/demo.env` exists; `make db-pull from=demo`. The "backup now" sudo rule also accepts an extra environment's name: after updating
  Server OPS repeat `sudo bin/install-backups` (`bin/update` shows that the rule differs).
- **Metrics** – the same lines with `environment="demo"`.
- **Access** – `--grant project:demo=LEVEL` and the UI's *Access* (on the key page demo stands next to prod and stage;
  "All at once" – the row "others"). `--grant project=LEVEL` without an environment – only stage and prod.
- **When the environment is no longer needed** – on the setup side `make down ENV=demo`, removing `data/demo` and the name from `STAGE_ENVIRONMENTS`;
  here – the environment's own target goes with `sudo bin/add-project --remove-target demo --name NAME` (the UI's project page also offers it;
  it is refused when there is no shared `target.env` to fall back to), `app/.generated/backups/*-demo-*` (if the backups are no longer needed)
  are deleted by hand, the keys' `…:demo` permissions are revoked in *Access* (the gate does not let anyone connect to an unlisted environment anyway).

## Bitbucket SSH address (since 2026-11-12)

Since 2026-11-12 Bitbucket answers git over SSH only on `ssh.bitbucket.org` (`git@ssh.bitbucket.org:workspace/repo.git`);
`bitbucket.org` will no longer accept SSH connections. HTTPS (API, the alpha.local git mirror) does not change. The new name has the same
host keys (ED25519 `SHA256:ybgmFkzwOSotHTHLJgHO0QN8L0xErw6vd0VhFA9m3SM`), but a `known_hosts` entry is bound to the
name, and the build directories' `origin` stays the one they were cloned with. So on the server, before that date:

```bash
cd ~/setup-server
sudo bin/prepare-host --check          # must show: would record the SSH host key of ssh.bitbucket.org
sudo bin/prepare-host                  # records it (only with the published fingerprint)
sudo bin/move-bitbucket-ssh --check    # which remotes will move and whether the new address responds
sudo bin/move-bitbucket-ssh            # moves the app/ and app/public/ remotes; does not touch GitHub
sudo bin/install-ui                    # the sudo rule for the web UI (the "Bitbucket SSH" action)
```

- `move-bitbucket-ssh` first reads every new URL with `git ls-remote` as the `setup` account (with its key and
  `known_hosts`); it does not change a remote that did not respond and ends with code 1. It can be repeated.
- `bin/add-project` from now on clones an old `git@bitbucket.org:…` URL through `ssh.bitbucket.org`, and when run again
  it moves an existing build directory's remote by itself.
- The web UI recognizes both names (pipelines, deploys).
- **On a developer's computer** the same command without sudo moves the remotes of their clones (all of them, not only `origin`):
  `bin/move-bitbucket-ssh --check --under ~/Projects`, then without `--check`. First write the
  `ssh.bitbucket.org` key into `~/.ssh/known_hosts`, after comparing its fingerprint with the published one (`ssh-keyscan -t ed25519 ssh.bitbucket.org | ssh-keygen -lf -`).

## Updating

One command updates both checkouts: this repo (`~/setup-server`, the administrator's clone from which
the command is run) and the shared setup checkout `/srv/setup-server/setup` (of the `setup` account). The manual
update of the shared server is described below. For the administrator's two working clones an automatic
update after the GitHub Actions checks can also be enabled (see "Automatic update of local clones").

```bash
cd ~/setup-server
bin/update --check --server-ref v0.2.0 --setup-ref v2.0.0   # shows what would change
bin/update --server-ref v0.2.0 --setup-ref v2.0.0           # updates, checks, rolls back on failure
```

- Run it **without sudo**, as the owner of this clone (Server OPS is fetched with your git key); the setup checkout
  part the script runs itself through `sudo -u setup` (it will ask for a password). Run with sudo – it refuses.
- Only one side can be given (`--setup-ref` or `--server-ref`); the other stays where it was.
- **Installed command copies.** When Server OPS changes, after the switch (before the health check) the installed copies
  `/usr/local/sbin/setup-server-backup`, `/usr/local/sbin/setup-server-metrics`,
  `/usr/local/sbin/setup-server-backup-pull`, `/usr/local/sbin/setup-server-access` and the web UI's `/usr/local/sbin/setup-server-ui` together with
  `/usr/local/lib/setup-server/bin/` (only those installed) are overwritten through `sudo` from the new version: `bin/install-backups --refresh`,
  `bin/install-metrics --refresh`, `bin/add-backup-key --refresh` and `bin/install-ui --refresh`.
  When the metrics or UI copy changes, the service `setup-server-metrics` or `setup-server-ui` is restarted (the runner's – not).
  If the clone is already on the given ref (e.g. after `git pull`), `bin/update` still compares the installed copies with the clone
  (`bin/… --refresh --check`) and overwrites outdated ones – otherwise the UI, the gates and the timers would stay on the old version.
  Other files (units, sudo rules, settings) are not changed; if they differ in the new version, a note
  `note: … differs` is printed – then repeat `sudo bin/install-backups` (the time is kept), `sudo bin/install-metrics` or
  `sudo bin/install-ui`. If the overwrite or the health check fails, the previous copies are restored together with the checkouts.
- **From the web UI** (*Actions → Update*) `bin/update` runs as the clone owner, but without a terminal:
  it cannot ask for the sudo password, so its calls are then `sudo -n`. If the owner is not allowed sudo without a password,
  `bin/update` refuses right away (code 77, nothing changed) – update in a terminal. The clone must also be able to
  fetch (`git fetch`) without a passphrase. During the update the UI restarts into the new copy, and the action continues.
- **Ref** – a tag (preferred), an `origin` branch (`master` or `origin/master`) or a commit id. Server OPS may also be off a tag
  (there are no tags yet). setup off a `vX.Y.Z` tag is fine **for stage only**: for a prod release setup must be on a
  release tag – the preview shows this as a note.
- `--check` fetches (`git fetch`) and shows, for each checkout, the current and new ref, the number of commits
  ahead/behind, a summary of changed files and the list of commits (`+` – will appear, `-` – will disappear).
  It does not change the checkouts.
- It refuses if a checkout has local changes, the ref is unknown (it shows the newest tags) or a
  pipeline step is running (a `make` process of the `setup` account outside a container; `make` of the project cron container's jobs with
  the same UID is not counted) – then wait until it finishes.
- Tags are fetched without `--force`: if an `origin` tag was moved to another commit (rewritten), the local one
  stays where it was, and `bin/update` (and `--check`) ends with an error, showing the tag and both commits.
  A release tag must not move – find out who moved it and why.

**Health check after the update** (all three; any failure = rollback):

1. The Server OPS tests (`python3 -m unittest discover -s tests`) from the new version – they check that the
   scripts work on this server;
2. the setup checkout is on the new commit and the `setup` account reads it without changes – that is exactly what
   the pipelines use (`SETUP_DIRECTORY`);
3. the runner service `setup-server-runner` and every GitHub runner service `setup-server-github-runner@<scope>`
   that was running before the update are still active **with the same PID** – this proves they were not
   restarted. If the Bitbucket runner was not running before the update, this is only shown; GitHub services that were not running
   are not checked.

The runner services are never restarted: they are run from `/srv/setup-server/runner/current` and
`/srv/setup-server/github-runner/<scope>`, not from this repo, and every pipeline step reads the setup checkout
afresh. Whether the runner is ONLINE (on GitHub – Idle), check in Bitbucket or GitHub (the script does not read their
API).

If the check fails, both checkouts are returned to their previous commits (or to the branch, if the checkout was on
one), and the installed command copies – to the previous ones; it prints which check failed and returns code 1. Code 2 means that the rollback failed too –
the commands for manual recovery are printed.

### Automatic update of local clones

All `tests` workflow jobs run on this repository's local runner
`[self-hosted, linux, setup.build]`: tests, channel publishing and the local update. The tests keep the
Ubuntu 22.04 / Python 3.10 environment from `tests/fixtures/ci.Dockerfile`, run in a Docker container on this PC.
The container works with the runner's UID/GID; only the checkout is mounted, without the Docker socket or the user's home directory.
CI runs on a push to any branch of the repo and on a manual workflow_dispatch; PR checks come from the branch push.
The workflow does not run automatically for PRs from external forks. Only a `master` push may, after the tests, publish the channel or update the environment.

GitHub Actions checks `master` and publishes only a verified version on the `deploy/local` branch:

- setup's `Release` workflow, after a successful `Setup compatibility`, publishes in one atomic push the release commit,
  its `vX.Y.Z` tag and `deploy/local`. If `master` changed in the meantime, the whole push is rejected.
- In the Server OPS `tests` workflow, the dependent `publish-local` job, after successful tests, checks the pushed commit
  and pushes `deploy/local`. A PR, a failed CI and an outdated check result do not push the channel.

The local `setup-server-auto-update.timer` checks both channels every minute (with up to 10 s of extra delay).

The workflow also has a dependent `local-update` job: after `publish-local` it runs on this
repository's registered GitHub runner with the labels `[self-hosted, linux, setup.build]`.
It starts the existing `setup-server-auto-update.service`, waits for it to finish and checks that both local
clones are clean, match the channels confirmed by CI and have the commit verified by the workflow. The runner's systemd
service passes `SETUP_SERVER_ROOT`; its account must be allowed to start the update
service non-interactively. The job does not run for PR code. If this repository's runner is not connected, it will wait in the queue;
the independent timer keeps working even when the GitHub job cannot reach the PC.

For the local checks Python 3.10+, Git, Make, Docker with Compose and PHP CLI must be in place; the installer
does not install missing packages. On CachyOS the PHP CLI package is `php`.
It needs only the existing non-interactive Git SSH access. No GitHub API token, self-hosted runner or incoming
network port is needed. After the PC was off it checks again; it also works before a graphical login.

An example for user `tomas`'s two clones; first the preview, then the same command without `--check`:

```bash
cd /home/tomas/Projects/setup-server
sudo env SETUP_SERVER_USER=tomas SETUP_SERVER_ROOT=/home/tomas/Projects/runtime/setup-server \
  SETUP_DIRECTORY=/home/tomas/Projects/setup bin/install-auto-update --check
sudo env SETUP_SERVER_USER=tomas SETUP_SERVER_ROOT=/home/tomas/Projects/runtime/setup-server \
  SETUP_DIRECTORY=/home/tomas/Projects/setup bin/install-auto-update
```

The default Server OPS clone is the one the installer is run from, setup is its sibling `setup`;
another server clone is given by `SETUP_SERVER_CHECKOUT`. Both must belong to `SETUP_SERVER_USER` (default –
the owner of the server clone). `SETUP_SERVER_ROOT` here chooses only the state directory `auto-update/`, not the other clones.
The installer does not require Ubuntu and does not change `prepare-host`, packages, projects or runners. When repeated, the existing
units are compared; a running update is not stopped. To change these paths, stop the timer and wait for
the running update service to finish.

`bin/auto-update` keeps `master` in both clones and only does a fast-forward. If there are modified or
untracked files, another branch, an unfinished Git operation, unpublished commits or that user's `make` outside a
container, the update is postponed. A newer local `master` whose CI is still running is not moved back.
The setup update commit must match the release tag corresponding to its `VERSION`. New MAJOR
releases are allowed too: these are two working clones, not a production project's choice of version.

After the change the local tests of both repositories run: the Server OPS unittest and setup level A (`tests/run`,
without the optional Docker integrations). On failure both clones return to their previous commits via `git reset --keep`.
If you started editing during the check, the automation does not delete your work and leaves a clear manual recovery error.
An interrupted update is recovered from the transaction log before the next attempt. Only one
update runs at a time; a version that failed the same checks is not retried until a new one appears or you pass `--retry`.

This mode updates **only the code of the two Git clones**. It does not change the root-owned installed command copies, sudo rules,
units or containers. For those there are still `bin/update` and the corresponding `bin/install-*`; for setup's host helper
copies – `setup/install-host` (on CachyOS update them by hand, do not run the Ubuntu package installation).

```bash
systemctl list-timers setup-server-auto-update.timer
journalctl -u setup-server-auto-update.service -n 50
cat /home/tomas/Projects/runtime/setup-server/auto-update/status.json
bin/auto-update --check --state-dir /home/tomas/Projects/runtime/setup-server/auto-update
sudo systemctl disable --now setup-server-auto-update.timer  # stops future attempts
```

Disabling the timer does not interrupt an update already in progress. Wait for it to finish before a manual recovery.
`--check` only fetches and shows; it does not change the working clones. For a manual retry add `--retry` and use
the same `--state-dir` as the service. The status of a successful run is `updated` or `up_to_date`; a channel not yet created –
`waiting_for_ci`; a rolled-back version – `rolled_back`; `updating` after the process has ended requires recovery.

### Setup only (no password)

The shared setup checkout (`/srv/setup-server/setup`) is updated by one command, without the administrator's password and without
updating Server OPS – or in the UI's *Update* ("Update to the newest"):

```bash
sudo bin/update-setup --check          # the current version, the newest release of the same MAJOR, commits
sudo bin/update-setup                  # switches to it
sudo bin/update-setup v2.6.0           # or to the given release tag
```

- Only release tags (`vX.Y.Z`), only a clean checkout, not during a pipeline step (`make` of the `setup` account outside a container).
  If the switch fails the checkout returns. The Git work is done by the `setup` account.
- **The newest release is `master`:** setup releases every merge into `master` itself after a successful CI
  (`Release X.Y.Z`, tag `vX.Y.Z`), so "to the latest" means what `master` has, and prod deploys work (setup's
  `make release ENV=prod` builds only from a release tag). If setup was ever left off a tag (switched by hand),
  *Update* and *Overview* ("Needs attention") show it; to go back – "Update to the newest".
- A new MAJOR – only when the tag is given; the preview warns that projects may need changes.
- **The deploy also does this by itself:** when the deployed commit's `SETUP_MIN_VERSION` is newer than the server's setup, setup 2.7.0+
  `pipeline-deploy` switches setup to the newest release of the same MAJOR and restarts the step.
- A full update (Server OPS and setup together, with the health check) – `bin/update`, below.

### Manual recovery

When `bin/update` ends with "The automatic return failed" (code 2) or was interrupted unexpectedly:

1. **The previous commits.** They are printed by the line `Previous commits (for a manual return): …`. If you
   no longer have it – the reflog (the first line before the update's `checkout`):

   ```bash
   git -C ~/setup-server reflog -n 5
   (cd / && sudo -u setup -H git -C /srv/setup-server/setup reflog -n 5)
   ```

2. **Why the return failed.** Most often – permissions (`.git` not writable) or corrupted files:

   ```bash
   ls -ld ~/setup-server/.git /srv/setup-server/setup/.git
   sudo chown -R "$USER": ~/setup-server
   sudo chown -R setup:setup /srv/setup-server/setup
   ```

3. **Return** (the same commands the script prints):

   ```bash
   git -C ~/setup-server checkout --force <setup-server commit>
   (cd / && sudo -u setup -H git -C /srv/setup-server/setup checkout --force <setup commit>)
   ```

   If the script also printed a `sudo install … /usr/local/sbin/setup-server-…` (or `… /usr/local/lib/setup-server/bin/`)
   line, run it (the previous copy is kept in a `/tmp/tmp.…` directory). Without it, the installed copies are overwritten from the returned checkout
   by `sudo bin/install-backups --refresh`, `sudo bin/install-metrics --refresh`, `sudo bin/add-backup-key --refresh`
   and `sudo bin/install-ui --refresh`.

4. **Check:**

   ```bash
   cd ~/setup-server && python3 -m unittest discover -s tests
   (cd / && sudo -u setup -H git -C /srv/setup-server/setup describe --tags --always)
   systemctl is-active setup-server-runner      # active; in Bitbucket the runner is ONLINE
   systemctl is-active 'setup-server-github-runner@*'   # active; on GitHub the runner is Idle
   ```

   If the runner is not active, see `journalctl -u setup-server-runner -n 50` (`-u setup-server-github-runner@<scope>`)
   and only then `sudo systemctl start setup-server-runner` (`setup-server-github-runner@<scope>`).

5. **If the checkout is corrupted** (git errors even after `chown`): setup – move the old one aside and let
   `prepare-host` clone it again (while there is no `setup/`, the pipelines will fail):

   ```bash
   sudo mv /srv/setup-server/setup /srv/setup-server/setup.broken
   sudo bin/prepare-host --setup-ref <tag>
   ```

   Server OPS – clone it again alongside and swap:

   ```bash
   git clone git@github.com:BigSant/DockerDevelopmentEnvironmentServer.git ~/setup-server.new
   git -C ~/setup-server.new checkout <commit>
   mv ~/setup-server ~/setup-server.broken && mv ~/setup-server.new ~/setup-server
   ```

## Worker metrics

The worker serves its own Prometheus `/metrics` – what only it knows: DB backups, deploys, the runner and
the disk. The target's metrics (system, website, containers, cron) are a separate setup endpoint on the project's
domain; this one does not duplicate them.

```bash
cd ~/setup-server
sudo bin/install-metrics --check --address 10.8.0.14 --allow 10.8.0.20   # shows the unit and the settings; changes nothing
sudo bin/install-metrics --address 10.8.0.14 --allow 10.8.0.20           # installs and starts
```

- `--address` – where it listens: on the VPN server its VPN address (`10.8.0.14`); the default is `127.0.0.1` (only from the
  server itself). `--allow` – the Prometheus address(es), IP/CIDR, separated by spaces or commas; required with any
  address except `127.0.0.1`/`::1`. `--port` – the default is `9101`.
- **The token is optional.** Access is protected by the IP list; without a token it answers every address on the list.
  `--new-token` – generates a 43-character token (or replaces the existing one; for Prometheus:
  `sudo sed -n 's/^METRICS_TOKEN=//p' /etc/setup-server/metrics.env`), `--no-token` – removes the existing one.
  A token is useful when the list has more addresses than Prometheus (e.g. the whole VPN subnet).
- The settings are kept in `/etc/setup-server/metrics.env` (root, 0600). When run again, settings not given
  (and the token) stay, the service is restarted only if the file changed; the runner service is not touched. The installed copy
  of the command is updated by `bin/update` (see "Updating").
- Access like the target's `/metrics`: not from the list – `403`, with a token – without it or with a wrong one `401`, another path –
  `404`. Without a list or with a token shorter than 24 characters the service does not start (`journalctl -u setup-server-metrics`).
  `X-Forwarded-For` is ignored: there is no proxy in front, clients connect directly.

**Why a separate service, not nginx-proxy.** The worker has no nginx-proxy of its own (the projects' nginx-proxy belongs to
the projects and their releases). A Python standard library HTTP server running as the `setup` account (the owner of the build
directories) reads the files directly: no Docker socket, no root, no new packages. systemd keeps it
read-only (`ProtectSystem=strict`, `NoNewPrivileges`, no capabilities). It listens on one address, so
it is not visible publicly; it takes the VPN address even when the VPN interface comes up later (`IP_FREEBIND`).

**Metrics** (labels `project` – the build directory name, `environment` – `stage`/`prod`):

| Metric | Meaning |
| --- | --- |
| `setup_worker_backup_newest_age_seconds` | How many seconds have passed since the newest backup (file time) |
| `setup_worker_backup_newest_size_bytes` | The size of the newest backup |
| `setup_worker_backups` | How many backups are kept; `0` – the environment is deployed from here (`env/<env>.env`), but there is no backup |
| `setup_worker_deploy_last_timestamp_seconds` | The time of the last deploy (including a failed one) |
| `setup_worker_deploy_last_success` | `1` – the last deploy finished; `0` – it stopped after rsync (`"failed": "after-rsync"`) |
| `setup_worker_deploy_last_success_timestamp_seconds` | The time of the last finished deploy |
| `setup_worker_deploy_last_rollback` | `1` – the last deploy was `rollback=1` |
| `setup_worker_deploy_last_info{ref,commit}` | The last deploy's ref (up to 64 characters) and commit (12 characters) |
| `setup_worker_files_backups`, `setup_worker_files_backup_newest_age_seconds`, `_newest_size_bytes` | The same for file backups (`files-<env>-…Z.tar.gz`, `BACKUP_FILE_PATHS`); `0` – only for an environment with `BACKUP_FILE_PATHS` and only once `setup-server-backup-files.timer` is installed |
| `setup_worker_backup_policy_interval_seconds{series}` | The backup policy (see step 6) of each environment deployed from here: how often the series (`db-short`, `db-full`, `files`) is backed up – `86400` daily, `604800` weekly, `2419200` monthly, `0` off; for alert thresholds that follow the policy |
| `setup_worker_runner_up`, `setup_worker_runner_state{state}`, `setup_worker_runner_restarts` | The `setup-server-runner` (Bitbucket) state from `systemctl show` (`active`, `failed`, `not-found` …) and automatic restarts |
| `setup_worker_github_runner_up{scope}`, `setup_worker_github_runner_state{scope,state}`, `setup_worker_github_runner_restarts{scope}` | The same for every registered GitHub runner (`github-runner/<scope>` with `.runner`): the service `setup-server-github-runner@<scope>`. If there are none – these lines are absent |
| `setup_worker_disk_size_bytes`, `_used_bytes`, `_available_bytes` `{path}` | The file system holding `/srv/setup-server` (like `df`) |
| `setup_metrics_group_up{group}` | `0` if part of a group (`backups`, `deploys`, `runner`, `github_runner`, `disk`) could not be read (the reason is in the journal) |

The Bitbucket runner metrics were left as they were (without labels) so that existing alerts and graphs do not change; GitHub
runners have separate metrics with the `scope` label, because there may be several.

Backups – only `worker-<env>-…Z.sql.gz` (nightly and "backup now"). Deploy and "fresh" backups
are not counted, so that they do not hide a stopped timer. The weekly full series (`full-<env>-…Z.sql.gz`) has its own
metrics `setup_worker_full_backups`, `setup_worker_full_backup_newest_age_seconds` and
`setup_worker_full_backup_newest_size_bytes`; `0` for an environment without a full backup is shown only once
`setup-server-backup-full.timer` is installed, so that a server without weekly backups does not raise an alert (or when the policy makes it nightly: `db-full=daily`). A series the backup policy switches off (`off`) expects nothing: no `0` count for it; backups that exist keep their metrics. Deploys – from setup's history
`app/.generated/release/<env>/deployments.json` (the last entry). To check without Prometheus:

```bash
systemctl status setup-server-metrics
sudo -u setup /usr/local/sbin/setup-server-metrics --once          # metrics without the server and the token
curl http://10.8.0.14:9101/metrics                                  # from a listed address, without a token
curl -H "Authorization: Bearer $(sudo sed -n 's/^METRICS_TOKEN=//p' /etc/setup-server/metrics.env)" http://10.8.0.14:9101/metrics   # with the token
```

**Prometheus** (the server is connected to the VPN; `authorization` – only with a token):

```yaml
scrape_configs:
  - job_name: 'setup-worker'
    scheme: http                      # the VPN encrypts the traffic
    metrics_path: /metrics
    scrape_interval: 1m
    authorization:
      credentials_file: /etc/prometheus/tokens/setup-worker
    static_configs:
      - targets: ['10.8.0.14:9101']
```

**Suggested Grafana alert "backup older than 26 h"** (enabled together with the other alerts):

```promql
max by (project, environment) (setup_worker_backup_newest_age_seconds) > 26 * 3600
  or setup_worker_backups == 0
```

For the file backup – the same with `setup_worker_files_backup_newest_age_seconds` and `setup_worker_files_backups`.
For the weekly full backup – "older than 8 days":

```promql
max by (project, environment) (setup_worker_full_backup_newest_age_seconds) > 8 * 24 * 3600
  or setup_worker_full_backups == 0
```

With backup policies (step 6) the thresholds can follow each environment's frequency, and a series that is `off`
raises nothing (here `db-short`, the DB short backup; the same with `series="db-full"` / `"files"` and their metrics):

```promql
max by (project, environment) (setup_worker_backup_newest_age_seconds)
  > on (project, environment) (setup_worker_backup_policy_interval_seconds{series="db-short"} > 0) * 1.1
  or setup_worker_backups == 0
```

Also worth having: `up{job="setup-worker"} == 0` or "no data" (the VPN or the service is down),
`setup_metrics_group_up == 0`, `setup_worker_runner_up == 0`, `setup_worker_github_runner_up == 0`,
`setup_worker_deploy_last_success == 0`.

## Web UI

The server status and the same `bin/` actions in a browser. The UI listens only on `127.0.0.1` (default port `9102`),
so from a computer it is reachable only through an SSH tunnel.

The pages are a dark monospace control board (amber accent, lamps, hazard tape for prod); every text colour keeps at least
4.5:1 contrast on its background and nothing is smaller than 12 px. New pages reuse its parts as described in DESIGN.md.

```bash
cd ~/setup-server
sudo bin/install-ui --check        # account, unit, sudo rule, settings (secrets hidden); changes nothing
sudo bin/install-ui                # installs, starts and shows the password once
```

- `--port` – another port (1024–65535). There is no other address: `--address` accepts only `127.0.0.1`, the service itself does not
  start on another address.
- `--user` – the first administrator's name (default `admin`), `--new-password` – a new password for them (the old one
  no longer works; see "Users and groups" – this is how root always gets access back).
- Run again, it keeps the name, port, password and tokens; it restarts the service only if the file changed.
  The installed copies are updated by `bin/update` (see "Updating").

**Signing in.** On your computer open the tunnel and in the browser – `http://localhost:9102/`:

```bash
ssh -L 9102:127.0.0.1:9102 <server>    # the UI is reachable while the tunnel is open
```

The tunnel goes through the server's SSH; if it does not respond, see `bin/install-ssh` ("Steps", step 7).

A sign-in page opens (name and password; SSH encrypts the traffic). The first time – the `admin` password shown by `install-ui`;
the server keeps only its PBKDF2 hash in `/etc/setup-server/ui.env` (root, 0600). If you forget it –
`sudo bin/install-ui --new-password`. For scripts (`curl -u name:password`) Basic auth also works with any enabled
user's name; the browser's pop-up dialog is gone.

**Users and groups.** The *Users* section (`/users`): who signs in to the UI and what they can do there.

- **A group** gives, for each section (Overview, Projects, Runners, Access, Files, Update, Actions, Instructions, Logs,
  Metrics, Users), "none", "view" (the section is shown) or "manage" (its actions as well: preview and run; Instructions,
  Logs and Metrics are only shown). An action is allowed if the user manages at least one section that contains its form
  (e.g. "Backup now" – Overview, Projects, Files or Actions; starting, stopping, restarting a runner – Overview or
  Runners; adding a project, its targets and its backup policy – Projects; removing a project – Projects or Overview). Where the user can only view, there are no buttons, and a direct POST gets `403`.
- **A user** belongs to one or more groups; their permission for a section is the highest of them. They can (optionally) be linked
  to a person in *Access* (an SSH key and tools): this is only a link, signing in does not depend on the key. A disabled
  user cannot sign in, their sessions end immediately; a new password ends their other sessions.
- **Default groups:** *Administrators* (manages everything, cannot be changed or deleted), *Operators* (manages Overview, Projects, Runners,
  Files and Actions, views the other sections except Users), *Viewers* (views everything except Users). Their ids are
  `administrators`, `operators` and `viewers`; on start the UI automatically migrates the old Lithuanian group ids in an existing
  `accounts.json` (`administratoriai`, `operatoriai`, `stebetojai`) to them. At least
  one enabled administrator must remain: the UI does not allow the last one to be disabled, removed from the group or deleted. When a new
  section appears, the existing groups get it at the level they have on the closest one (*Runners* and *Projects* – like
  *Actions*; a group that sees the *Overview* also gets "view" on *Projects*, where the project pages moved); later this
  is changed like any other permission.
- **The first administrator.** When the UI starts for the first time, it makes the `ui.env` name (`admin`) a member of the
  *Administrators* group. When the `ui.env` password is changed (`sudo bin/install-ui --new-password`) that user is
  enabled again, put back into *Administrators* with the new password, and their sessions are ended – root's way back if
  everyone got locked out.
- **My account** (`/account`, the name at the top right) – one's own groups, permissions and password change (12–128 characters).
- **Where it is kept:** `/var/lib/setup-server-ui/accounts.json` (`setup-ui`, 0600; passwords – PBKDF2 hashes) and
  `sessions.json` (only the sha256 of session cookies). A session is the cookie `ops_session` (HttpOnly, SameSite=Lax; behind an HTTPS proxy –
  also Secure), it ends after 12 h of inactivity and no later than after 14 days; the "Sign out" button ends it. After 10 failed
  sign-ins from one address within 15 min. (behind the LAN proxy – by the `X-Real-IP` it passes) – `429` from then on until the time has passed. A corrupted `accounts.json` – the UI
  does not start (the journal says why); once it is removed, the `ui.env` user becomes the first administrator again.
- Every change (sign-in, sign-out, saving a user or group, deletion, refusal) is written to
  `ui-actions.log` (`users …`); passwords – never. If the line cannot be written, the change is not made.

**What it shows.** In the navigation, next to *Access* – the number of keys, next to *Update* – a dot: yellow when there is a newer
commit, red when setup is not on a release tag.

- *Runners* (`/runners`) – all of this server's CI runners on one page. GitHub runners are grouped by
  organization or repo (with a link to their GitHub settings): the name on GitHub, the service state, whether it is running a job now,
  the version, whether it starts with the server, restarts and the start, stop, restart buttons. On a runner's page
  (`/runners/github/<scope>`) – its details (and the GitHub ID with a link), **an extra instance** (a free number 2–9,
  the name and the `./config.sh --url …` line already filled in – only the token remains to be pasted) and **removal** (with GitHub's
  "Remove" command or without it). *Add runner* (`/runners/new`) – a new GitHub or Bitbucket runner.
  The Bitbucket runner – `/runners/bitbucket` (replace, remove). The name and labels on GitHub cannot be changed – only by
  removing and adding again. In the Overview the runners are one card (how many are running, how many are running a job).
- *Overview* – at the top "Needs attention": everything that is broken or waiting (failed units, setup off a tag, a newer
  version, little space, a failed deploy, a stopped secret store, unhealthy containers, a late or missing backup),
  each with a link. Which backup is watched follows the environment's backup policy (see "DB backups"): the DB short (registry)
  series while `db-short` is not `off`, else the DB full series while `db-full` is not `off`, else none (the alarm says
  `old DB short backup`, `no DB full backup`, …); it is late after 26 h (`daily`),
  8 days (`weekly`) or 29 days (`monthly`). The files backup is watched the same way by its own frequency, while the files
  timer is installed and `files` is not `off`. An environment whose policy is all `off` raises no backup alarm; one without a
  target or a development one none either. A backup alarm leads to the environment's backup forms (`/project/NAME#backup-ENV`). Then – the Server OPS version (installed and of the administrator's clone), the shared setup checkout's version
  (commit, whether it is a release tag, how far ahead origin/master is as of the last fetch), the runner summary, the nightly and weekly full backup timers (last run, result, next), the metrics service,
  the nightly file backup timer, the UI itself, the free space on the disk holding `/srv/setup-server`. Next to every installed service – its buttons
  (start, stop, restart; backups – "Start"), which lead to the same preview as the other actions.
- *Overview → Projects* – every build directory: repo URL, environments (stage/prod), target (`target*.env`, not a secret),
  the last deploy (time, commit, success or not, rollback), the newest nightly and weekly full backup (age,
  size, how many are kept), whether `<PROJECT_NAME>-<env>-secrets` is running, the containers (state, health) and the latest
  Bitbucket pipelines.
- *Projects* (`/projects`) – where projects are managed. The list: how many projects and environments, how many have no target,
  deploy to another server or failed their last deploy; per environment its target, the last deploy and its state, with a link to
  the project's settings. A deploy names the branch its commit comes from (`master · v1.2.0 · 2 h ago · 3 behind`; on the project
  page `branch master (3 commits behind origin/master)`), read from the code clone's already fetched `origin/*` branches – the
  server fetches nothing for it, and git runs there without hooks, fsmonitor or system config; unknown, it is left out. *+ Add a project* (`/projects/new`) – the env repo URL, the code repo URL (optional), the name (optional),
  *monorepo* (`--monorepo`), *the account key* (`--shared-key`) and the targets: one for every environment (`target.env`), prod's and
  stage's own, and further `ENV=target` lines (`local`, `local:/PATH`, `ssh://USER@HOST/PATH`, `files:` in front for files only).
  Each target field has its type in front: *Docker stack* or *Files only* (`--target …=files:…`: a server without Docker, only the
  code goes there); *Change the target* and *Add an environment* have the same choice. A files-only environment carries a quiet
  `files only` chip on the Projects list (its second line shows its files backup, or `no DB here`) and on the project page,
  where *Containers* and *Secret store* read `not used (files only)`, there are no DB backup rows, no "Backup now" and only the
  files series in its *Backup policy* form (none without `BACKUP_FILE_PATHS`), and no DB, container or secret-store alarm is
  raised for it; on *Backups* its *DB short* and *DB full* columns read `n/a (files only)`. The project page (`/project/<name>`)
  shows each environment (the last successful deploy, all of its backups – DB short, DB full, DB short/full fresh, DB
  pre-deploy, files –, each container's
  `docker logs` – the last 200, 500 or 2000 lines – and "Backup now on the server"), then **Settings**: the layout and repos, every
  environment with its target and target file (`target.env` shared or `target.<env>.env` its own), *Change the target* and
  *Add an environment* (a name of lowercase letters and digits, 2–16, not `local`/`test`; it deploys once the project lists it in
  `STAGE_ENVIRONMENTS`), and *Remove an environment's own target* (only when a shared `target.env` remains). Each environment
  (not a development one) also has *DB short backup*, *DB full backup*, *DB short, fresh* and *Files backup* rows and a
  *Backup policy* row (`DB short daily · DB full weekly · files daily`, `DB full only, daily`, `DB off · files daily`, `off`;
  "(default)" when nothing is changed) and, next to "Backup now", a *Backup policy* form (`#backup-ENV`): a select for the DB
  short (`db-short`: daily, weekly, off), DB full (`db-full`: daily, weekly, monthly – every 4 weeks, off) and files series
  (`files`: daily, weekly, off – only when the environment has `BACKUP_FILE_PATHS` or files backups; otherwise its value is sent
  unchanged); the preview lists what changes (`db-short: daily → off`) and warns above "Run" when the database would no longer be
  backed up at all. In the list each environment's second line shows the newest DB backup the policy watches: `DB short 5 h ago`,
  `DB full 2 d ago` (DB full only), `no DB short backup`, or a quiet `backups off`. *Backups* (`/projects/backups`, linked from *Projects* and *Actions → Backups*) – every
  project environment in one table (50 projects fit): the project, the environment, its policy (`· default` when unchanged), the
  age of the newest backup in the *DB short*, *DB full* and *Files* columns (`late` / `none` in red for the series the alarm
  watches, `off` quiet); late or missing ones first (`?sort=name` by name), a filter by name or repo, "only late or missing" and
  the policy (not the default, DB full only, DB off), with "N of M"; each row leads to the environment's policy form. Then who reaches the project
  (*Access*), its pipelines and *Remove*.
- *Access* – developer keys: for each – how many prod, stage and other environments it reaches and at what level (`make ssh`),
  backups (`make db-pull`: the all-projects level and the environments above it), networks and last use; search by key
  or project; the latest activity of all keys. "Add key" – public key, name, networks, initial backups (all
  projects, prod, stage) and access (prod, stage). On a key's page there are two cards – *Access (make ssh)* and *Backups
  (make db-pull)*, both with:
  - *All at once* – one level (or "—", revoke) for all prod, all stage or all environments, for all projects
    or only where the key already has a permission; the backups card also has "all projects" (including new ones);
  - *Projects* – a row per project, its environments (prod, stage, others) side by side, the level – a button bar (in backups "all: db"
    – an environment without its own permission, the all-projects level applies); a filter by name and "only with permission" (only the shown
    environments are changed, hidden ones stay);
  - *Key settings* – "Person" (first and last name, shown instead of the key name, and the login name for the tools –
    `bin/tool-access --full-name … --login …`), networks, a new public key (applies to all environments), permissions for a project
    that does not exist yet; revoking the key;
  - *Activity* – who, when, from where: logins, backups, shells (level, duration, result, session recording), commands,
    rejected attempts; on a session's page – the typed lines.

  All changes are the same `add-backup-key` command with the key's full permission list; the preview above the command shows what
  will change (`+` new, `~` another level, `−` revoked). On the project page – who reaches that project's environments.
  The third tab *Tools* – `bin/tool-access`: the key's tools password (entered, not shown, the date it was set) and
  the project environments whose tools it opens (a failed send is marked), with the same *All at once*
  (tools or "—" for all prod, stage, other or all environments; local and test – never). In the list – a *Tools* column, at the bottom of the
  page "Tool access" – this server's addresses and "Send again" (see "Tool access").
- *Files* – file backups (`BACKUP_FILE_PATHS`, see "File backups"): every environment with its paths and newest backup;
  on the environment page – all of its backups and "Files backup now"; on a backup's page – the files with search, what is new, changed
  and gone since the previous backup; on a file's page – the contents, the differences from the previous backup and "Download". Read-
  only: neither the backups nor the target are changed. "Needs attention" shows a file backup older than 26 h (when the timer is installed).
- *Update* – Server OPS and setup: version, commit, whether it is a release tag, how far ahead origin/master is.
  "Update to the newest" – Server OPS to `origin/master` (`bin/update`, see "Updating"), setup – to the newest
  release of the same MAJOR (`bin/update-setup`; every merge into master makes a setup release); a specific version
  or a new MAJOR – "A specific version or ref".
- *Users* – the UI's users and groups (see "Users and groups"); without the "view" permission the section is not shown.
- *Metrics* – the worker's `/metrics` as Prometheus sees it (`setup-server-metrics --once`, without a token), and which
  groups were collected.
- *Logs* – developer activity (all keys), `ui-actions.log`, `backup-pulls.log`, `access.log`, the Server OPS services' journal (50 lines each) and the latest
  actions with their output.
- *Instructions* – how to prepare another server as a project target (`bin/add-project --target ENV=ssh://…`): you choose the OS
  (Ubuntu, Debian; CentOS/AlmaLinux/Rocky – untested), the control panel (DirectAdmin or none), the environment (prod, stage) and
  SSL (Let's Encrypt, own, no SSL – VPN only), enter the server, user, directory, domain, port and get
  numbered steps with commands: how to find out the OS and architecture on the target (`/etc/os-release`, `uname -m`),
  packages, Docker, the user and the `docker` group, the directory, the worker's key (the real one, from
  `/srv/setup-server/.ssh/id_ed25519.pub`), the firewall (DirectAdmin – also CSF: after `csf -r` Docker restores its
  rules, `live-restore` keeps the containers running), swap, ports, the DirectAdmin domain (directories
  `/home/USER/docker/<environment>`: stage and prod can belong to one user), DNS (the website and `*.` for the tools;
  Cloudflare – the tools' wildcard DNS only), the certificate and the proxy (enabled after the first deploy),
  `env/<env>.env.example` (limits – ceilings, stage – a small buffer pool), passwords (exporting the PS keys from the existing
  shop's `parameters.php`), Access keys for both repos, registration, host key comparison, the workspace runner and
  the first deploy. Every step
  names its source; where setup does not describe anything yet (DirectAdmin proxy, a certificate without a panel, a non-Debian OS), this
  is marked with a warning. The page runs nothing; the button opens *Projects → Add a project* with the target filled in.
  Next to it – an *example* (`/instructions/example`): the whole addition of a project with made-up names (PrestaShop with an environment
  repo and a separate code repo, stage on a DirectAdmin server, the website in a subfolder of a shared hostname) and what trips up a real
  addition: versions and disk space, Docker for the user, Access keys for both repos, the host key before `BatchMode`,
  the runner level, `Alias` and the proxy for a subfolder, cancellation.

**Actions.** Each one is first run with `--check`: the page shows the command and its output. Only then does
"Run" start **the same** command without `--check` (the preview is valid for 15 min. and once; if `--check` ended
with an error, there is no "Run"). One action runs at a time; its page refreshes until it finishes.

| Action | What it runs (`L` = `/usr/local/lib/setup-server/bin`) |
| --- | --- |
| Add a project (*Projects*) | `sudo -n L/add-project [--shared-key] [--monorepo] --target … [NAME] URL [CODE_URL]` (the target is required for a new project) |
| Change the target, add an environment (project page) | `sudo -n L/add-project [--monorepo] --target ENV=TARGET NAME URL [CODE_URL]` – the project's own URLs, as the status reads them from its clones; the clones are kept |
| Remove an environment's own target (project page) | `sudo -n L/add-project --remove-target ENV --name NAME` |
| Remove project (on the project page) | `sudo -n L/remove-project [--with-backups] NAME` |
| Install runner (*Runners*) | `sudo -n L/install-runner --provider bitbucket\|github [--name …] [--instance N] [--tarball URL\|FILE [--sha256 …]]`; the dialog text – via stdin |
| Remove runner (*Runners*) | `sudo -n L/install-runner --provider github --remove --scope …` (the GitHub "Remove" command – via stdin, not shown) or `--remove` (Bitbucket) |
| Backup timers (nightly DB short, weekly DB full and files) | `sudo -n L/install-backups [--at HH:MM] [--full-at "DAY HH:MM"] [--files-at HH:MM]` – when they run; how often each environment is backed up is its backup policy (project page, *Projects → Backups*) |
| Backup now on the server (also on the project and *Files* pages) | `sudo -n L/backup-projects [--full\|--files] [PROJECT [ENV]]` – without Bitbucket, works for GitHub projects too; it backs up whatever the backup policy says |
| Backup policy (project page) | `sudo -n L/backup-projects --set PROJECT ENV db-short=… db-full=… files=…` (the preview: the same with `--check` at the end); the project and environment must be in the status, not a development one |
| SSH from the VPN | `sudo -n L/install-ssh [--from NETWORKS]` |
| Services (also in the Overview) | `sudo -n L/server-unit start\|stop\|restart UNIT` (see below) |
| Worker metrics | `sudo -n L/install-metrics [--address …] [--allow …] [--port …] [--new-token\|--no-token]` |
| Keys (*Access*) | `sudo -n L/add-backup-key [-] --name … [--from …] --pull-all … --pull …\|--no-pulls --grant …\|--no-grants` (all lists explicit; a new key via stdin, for an existing one – without it), `--remove NAME` |
| Tool access (the key's *Tools*, *Access*) | `sudo -n L/tool-access --name … --tools …\|--no-tools`, `--name … --password -` (the password via stdin, in the output – `<hidden>`) `\|--no-password`, `--name … --full-name … --login …`, `--allowed-ips …\|--no-allowed-ips`, `--apply` |
| Prepare the server | `sudo -n L/prepare-host [--setup-ref …]` (`--check` code 1 – "Needs attention" – still allows running) |
| Update setup (*Update*) | `sudo -n L/update-setup [vX.Y.Z]` (only release tags, no password) |
| Update Server OPS (*Update*) | `sudo -n -H -u <clone owner> <clone>/bin/update --server-ref origin/master` (or other `--server-ref`/`--setup-ref`) |
| Deploy, "backup now", runner test | Bitbucket API (see below) |

- Names, URLs, targets, refs, times and addresses are checked against strict patterns, the command is passed as an argument list
  (not a shell line): `;`, `$(…)` or `--option` in place of a value are rejected even before `--check`.
- **Runner dialog.** The text of the Bitbucket "Runner installation" dialog or the GitHub "New self-hosted runner" page
  is pasted into a field and passed to the command through stdin – like `sudo bin/install-runner < dialog.txt`. It is never
  written anywhere; the `--OAuthClientSecret` and `--token` values are replaced with `<hidden>` in the output. Between the preview and "Run"
  the text is kept only in the UI's memory (after a restart – paste it again).
- **Update** runs as the owner of the clone `install-ui` was run from; sudo must work for them without a
  password (see "Updating"), otherwise – in a terminal.
- **Services.** `bin/server-unit` manages only Server OPS units and only with allowed actions; what to do and with what is
  set by its own list, not by the caller. `--check` shows the unit's state and whether a pipeline step is running (stopping
  or restarting the runner would interrupt it).

  | UNIT | Actions | systemd unit |
  | --- | --- | --- |
  | `runner` | start, stop, restart | `setup-server-runner.service` |
  | `github-runner@SCOPE` | start, stop, restart | `setup-server-github-runner@SCOPE.service` (only a registered SCOPE) |
  | `metrics` | start, stop, restart | `setup-server-metrics.service` |
  | `backup`, `backup-full`, `backup-files` | start (waits until the backup finishes) | `setup-server-backup.service`, `setup-server-backup-full.service`, `setup-server-backup-files.service` |
  | `ui` | restart (rereads `ui.env`, e.g. new tokens) | `setup-server-ui.service` |

  By hand: `sudo bin/server-unit --check restart runner`.
- **What the UI does not do:** `bin/install-ui` (port, name, password) and the manual recovery after a failed
  `bin/update` (see "Manual recovery") stay in the terminal: they change the UI itself or are needed when it is not working.

**Deploys through Bitbucket.** On the project page – "Deploy to stage", "Deploy to prod" and for every extra environment
"Deploy to demo" (custom pipelines `deploy-stage`, `deploy-<env>`,
`deploy-production` with the variables `REF`, `IMAGES`, `BACKUP`, `ROLLBACK`; with CACHE checked – also `CACHE=yes`, and
unchecked it is not sent, and the setup 5.2.0+ pipeline clears the cache only when the deploy changes it), "Backup now" (`backup-now`, with
FULL checked – with the variable `FULL=yes`; unchecked the variable is not sent) and "Runner
test" (`runner-test`). The UI starts them through the Bitbucket API, so the history stays in Bitbucket (Pipelines,
Deployments), while the work is done by the runner on this server. The repo is the `origin` URL of the build directory's `app/`, the branch – its
`origin/HEAD` (it can be changed in the form). The preview shows the request (JSON), "Run" sends it and shows a link to
the pipeline. Starting pipelines for GitHub projects is not supported yet.

```bash
sudo nano /etc/setup-server/ui.env        # write the token (see below)
sudo systemctl restart setup-server-ui    # the service reads the tokens when it starts (or in the Overview: Web UI → Restart)
```

```ini
BITBUCKET_ACCESS_TOKEN_ALPHA_SHOP=…    # for one build directory: the name in uppercase, - replaced with _
BITBUCKET_ACCESS_TOKEN=…                  # for all others (e.g. a workspace access token)
```

The token is a Bitbucket *Repository* or *Workspace access token* (Bearer) with the permissions **Repositories: Read** and
**Pipelines: Read, Write**. It is kept only in `ui.env` (root, 0600), the service gets it through the environment; it is
not passed to commands, not shown on pages, not written to logs (the log has only the variable name). It is sent only to
`api.bitbucket.org`, redirects are not followed.

**Permissions and why.**

- The UI runs as **a separate account `setup-ui`** (no shell and no home directory), not `setup`: pipeline steps run as
  `setup`, and they must not be given the UI's permissions. `setup-ui` is in the `setup` group (it reaches `/srv/setup-server`, 0750, and
  its own log), but **not** in the `docker` group (if it is, `install-ui` refuses).
- What needs root the UI does only through `sudo -n` according to `/etc/sudoers.d/setup-server-ui` (checked with `visudo -cf`):
  with root permissions – only the root-owned copies in `/usr/local/lib/setup-server/bin/` (`add-project`, `remove-project`,
  `install-backups`, `install-metrics`, `install-runner`, `install-ssh`, `add-backup-key`, `backup-projects`, `prepare-host`,
  `server-status`, `server-unit`, `update-setup`), never
  the administrator's clone, which its owner can change. `bin/update` – only with the clone owner's permissions (it does not run as root).
- sudo does not restrict the arguments: the limit is set by the UI's check and by the commands themselves. So through these commands `setup-ui`
  (e.g. `install-runner`, `prepare-host`) has administrator power; it is protected by `127.0.0.1`, the sign-in, group permissions
  and the CSRF token.
- The status is read by one read-only command `server-status` (JSON): `systemctl show`, `journalctl`, versions and disk –
  with root permissions, and the build directories, Docker containers and the secret store – with the `setup` account's permissions. A symlink planted by a pipeline
  therefore reveals nothing more, and only the target values come out of the env files. Container logs –
  only of that project environment's container (from the same list the overview shows), in its Docker context. By hand:
  `sudo /usr/local/lib/setup-server/bin/server-status | less` (`--logs 100` – logs,
  `--container-logs PROJECT ENV CONTAINER [LINES]`, `--metrics`, `--files PROJECT ENV BACKUP` – a file backup's list,
  `--file PROJECT ENV BACKUP PATH` – one of its files). File backups are read with the `setup` account's permissions and are never
  unpacked anywhere: only the requested file is read.
- **systemd.** `NoNewPrivileges` is off: no `sudo` would work with it. `ProtectSystem`, `ProtectHome`, `PrivateTmp`
  are off too: the actions run in the service's namespace and write `/usr`, `/etc` (installations), the administrator's clone
  (`bin/update`) and `/tmp` (previous copies for a manual return). What does no harm is kept: `ProtectKernelModules`,
  `ProtectControlGroups`, `LockPersonality`, `RestrictRealtime`. `KillMode=process`: only the UI is restarted, a running
  action continues.
- **CSRF.** Every form (including sign-out) has a token – an HMAC of the user name with a key the service creates when it starts
  (after a restart open forms no longer work: refresh the page). A POST without it or with a foreign `Origin` – `403`. `Host` must be
  `127.0.0.1` or `localhost` (protection against DNS rebinding), otherwise – `400`. The name, password and tokens are compared
  in constant time.

**Log.** Every action is written to `/srv/setup-server/ui-actions.log` (append-only): time, user,
client, command (argv) and exit code; for pipelines – repo, pipeline, variables, the HTTP response and the token's
variable name. Stdin (the runner dialog, a key) and tokens are not written. If the line cannot be written, the action is not run.
sudo itself also records every command. The actions' output – `/var/lib/setup-server-ui/jobs/` (`setup-ui`, 0700;
the last 100 are kept).

```bash
systemctl status setup-server-ui
journalctl -u setup-server-ui -n 50                 # rejected sign-ins and CSRF, errors
sudo tail -n 20 /srv/setup-server/ui-actions.log
```

## Project containers on the server

- **Which Docker.** The projects are started by the `setup` account through `DEPLOY_CONTEXT=default`, i.e. the server's system
  Docker (`/var/run/docker.sock`). On Ubuntu Desktop the administrator's `docker` often points to Docker Desktop
  (`docker context ls` marks `desktop-linux` with an asterisk) – that is another VM with other containers, so
  the projects are not visible there. The projects' containers, logs and `exec`:
  ```sh
  sudo docker --context default ps -a --filter name=<project>-prod
  sudo docker --context default logs --tail 50 <container>
  ```
  zsh does not split a variable into words: `D="sudo docker --context default"; $D ps` does not work, write the whole command.
- **Ports 80/443.** If on the target they are held by the computer's web server (e.g. the nginx of an earlier stack),
  the project's nginx-proxy does not get them. A setup newer than 2.7.1 checks this before starting the containers and stops;
  free the port (`sudo ss -ltnp 'sport = :80'`, then e.g. `sudo systemctl disable --now nginx`) and deploy
  again.
- **Changing data directories.** Before changing or renaming a project's data directory
  (`data/<env>/mysql`, `…/meilisearch` and the like), stop that container: a running container stays attached
  to the renamed directory (if it is deleted – `No such file or directory`), and sees the new directory only after
  `docker restart`.

## Security

- The `setup` account is in the Docker group, which on this server is equivalent to root permissions. This is accepted
  because the server is both the build server and the target. For remote targets this question is solved
  separately.
- The existing Docker is never replaced or restarted, so the containers already running
  (the current alpha.local) keep running.
- The runner's OAuth credentials are kept only in `/etc/setup-server/runner.env` (root, 0600), but on the running runner's
  command line they are visible to all accounts on the server, and in its environment – to the `setup` account's processes and pipeline
  steps (see "Steps", step 3).
- The GitHub runner's registration token is never written anywhere; a registered runner's key is in its directory
  (account `setup`, so it is reachable by jobs too). A self-hosted runner – only for private repos.
- GitHub project deploy keys – one per repo, read-only (`/srv/setup-server/.ssh/github-…`).
- Passwords are not written to this repo, to the project directories or to server files: they live only in Bitbucket and in the running
  containers' environment (Docker configuration, just like the DB passwords in the DB container); the nightly backups take them
  from the project's `secrets` container. The sudo rule adds no new permissions to the `setup` account: it is in the Docker group anyway.
- SSH (`bin/install-ssh`) accepts only keys from outside the VPN network and not from this server; password login is possible only
  from the VPN (if the server's settings allow it). The port is not opened to the internet: that is decided by the router or ufw.
- Backup download keys (`bin/add-backup-key`) connect to the `setup` account, but only from the given network
  and only with a forced command that hands out only the backups of the allowed project environments (`restrict`: no terminal and no
  forwarding). The backups contain real customer data: give only the projects that are needed (`--pull`, not `--pull-all`);
  every download and rejected request is recorded in `backup-pulls.log`.
- Keys with `--grant` reach the project targets through this server the same way as the deploy (see "Steps", step
  8): the permissions are written in the key line, the forced command checks them, there is no forwarding (`restrict,pty`),
  every request – `access.log`, an interactive session's input – `access-sessions/` (including passwords typed in it). `full` for a local target and `docker` (`exec` as root into project containers with
  their mounted directories) are high permissions; `phpfpm-readonly` protects only the files, not the database. `authorized_keys`
  belongs to the `setup` account, so a pipeline step could change it – but it already is `setup` with Docker anyway.
- Tool passwords (`bin/tool-access`) are set by the administrator; the UI passes them only through stdin, in the output they are
  replaced with `<hidden>`, and the server keeps only crypt SHA-512 hashes (`/etc/setup-server/tool-access.json`, root 0600;
  in the build directory and on the target – `users.htpasswd`). The tools get the Basic-auth password with every request, so their
  subdomains need HTTPS.
- File backups (`BACKUP_FILE_PATHS`) only read the target, with the same `setup` key as the deploy; nothing is written to it. The backups are
  the `setup` account's files (0600) in the build directory, next to the DB backups; the UI only shows them. Do not put
  paths with secrets into `BACKUP_FILE_PATHS` (e.g. `app/config/parameters.php`): the UI shows file contents.
- The worker's `/metrics` token (if used) is only in `/etc/setup-server/metrics.env` (root, 0600); the service
  gets it through the environment. The endpoint listens only on the given address (VPN), answers only allowed addresses (and, when
  a token is set, only with it) and changes nothing.
- The web UI listens only on `127.0.0.1` (from elsewhere – through an SSH tunnel), requires a password and, for every form, a
  CSRF token. It runs as a separate account `setup-ui` (not `setup`, not in the `docker` group) and with root permissions runs only
  the root-owned command copies in `/usr/local/lib/setup-server/bin/` according to its sudo rule. Through them it has
  administrator power, so protect its password like an administrator's. Bitbucket tokens – only in
  `/etc/setup-server/ui.env` (root, 0600); every action – `ui-actions.log`.
- `bin/update` from the UI works only when the clone owner can use sudo without a password. Server OPS does not grant
  such permissions: granting them is the administrator's decision (the owner's account then becomes equivalent to root without a
  password).

## Not yet included

These parts are still in progress (the map "Map — setup-server"):

- Connecting Prometheus through the VPN and the Grafana alerts (backup older than 26 h, "no data");
- Starting GitHub Actions workflows from the web UI (Bitbucket pipelines can already be started);

## Tests

```bash
python3 -m unittest discover -s tests                                    # without root and without network
SETUP_SERVER_UBUNTU_IMAGE=ubuntu:24.04 python3 -m unittest discover -s tests   # + a real Ubuntu container
SETUP_PROMTOOL_IMAGE=prom/prometheus:latest python3 -m unittest discover -s tests  # + a metrics format check (without promtool)
```

### Existing local monorepo development environment

When the DDE files are in the monorepo's `app/` and the code in `app/public/`, do not use `add-project` to clone into an already working directory (for a server stage/prod build directory of a monorepo – `add-project --monorepo`, see "GitHub project"). Run as its owner:

```sh
SETUP_SERVER_ROOT=/home/tomas/Projects/runtime/setup-server bin/register-development-project example-app /home/tomas/Projects/example-app
```

Registration creates the `projects/example-app/app` link and `development.json`; the code, private env files and the DB are not moved. The web overview shows the `local` containers, their logs, the DDE-format deployment history and a link to GitHub Actions; deployment metrics are read too. This grants no server SSH, DB copy or backup permissions for the `local` environment. example-app uses its own encrypted recovery backups. CI/CD is run by this repo's separate GitHub runner (`bin/install-runner --provider github`); the deployment of a specific product is described by the product's pipeline and its DDE commands.

The automatic update systemd service uses `--wait-lock`: an active DDE CI or deployment lock postpones the update until the jobs finish. A plain `bin/auto-update` without this option stays non-blocking. If a local `ExecStartPost` is used to update the installed copies, its `flock` must also wait for the lock rather than use `-n`.

After a `bin/auto-update` update, repeat `bin/install-auto-update` with the same `SETUP_SERVER_ROOT`, `SETUP_SERVER_USER`, `SETUP_DIRECTORY` and `SETUP_SERVER_CHECKOUT`: a Git pull alone does not change the already installed systemd command. `systemctl cat setup-server-auto-update.service` must show `--wait-lock`; for the local installed-copies `ExecStartPost` use `flock --wait 1800`, not `flock -n`.

The CI step `bin/apply-local-update` waits for the service and checks its own tested commit. If a finished update was started before this publication, it starts another update (up to three attempts, without blind waiting). Real health check errors, local changes or an unconfirmed version stop the step immediately. This protects against a falsely green old deployment and against an error merely because the service was already running an earlier job.

The local version check takes a shared read lock: another timer update cannot change the version being checked between reading the state file and the Git HEAD/ref.
