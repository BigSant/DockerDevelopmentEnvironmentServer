# setup-server – build serverio ir taikinio paruošimas

Scriptai, kurie paruošia Ubuntu serverį setup v2 projektams. Šis serveris kartu yra ir
**build serveris** (Bitbucket Pipelines self-hosted runner'is, image'ų ir release'ų kūrimas), ir
**taikinys** (ten veikia projektų konteineriai). Deploy'ai valdomi Bitbucket'e, o darbą atlieka
runner'is šiame serveryje, todėl Bitbucket build minutės nenaudojamos.

Pirmasis serveris – VPN serveris (Ubuntu Desktop), pirmasis projektas – asana.local.

## Ką paruošia

| Scriptas | Ką daro |
| --- | --- |
| `bin/prepare-host` | Paketai (`git`, `make`, `rsync`, `python3`, OpenJDK 25 runner'iui), Docker su Compose (jei jo nėra – iš download.docker.com; esamas Docker nekeičiamas), paskyra `setup` su Docker grupe, katalogai `/srv/setup-server`, git raktas, bendras setup checkout'as. |
| `bin/install-runner` | Bitbucket Linux Shell runner'is kaip systemd paslauga `setup-server-runner` (paskyra `setup`). |
| `bin/add-project` | Projekto build katalogas `/srv/setup-server/projects/<repo vardas>`: aplinkos repo `app/`, atskiras kodo repo `app/public/`. |
| `bin/remove-project` | Pašalina projekto build katalogą; DB kopijas ir deploy'ų istoriją – tik su `--with-backups`; katalogo, kurį naudoja veikiantis konteineris, netrina. |
| `bin/update` | Atnaujina šį setup-server checkout'ą ir bendrą setup checkout'ą į nurodytą tag'ą, patikrina sveikatą, nepavykus grąžina abu į ankstesnius commit'us. Runner'io paslaugos neperkrauna. |
| `bin/install-backups` | Naktinės DB kopijos: systemd laikmatis `setup-server-backup.timer`, komanda `/usr/local/sbin/setup-server-backup` (kopija iš `bin/backup-projects`) ir sudo taisyklė „backup dabar“ pipeline'ui. |
| `bin/install-metrics` | Worker'io Prometheus `/metrics`: systemd paslauga `setup-server-metrics` (paskyra `setup`, tik skaito), komanda `/usr/local/sbin/setup-server-metrics` (kopija iš `bin/worker-metrics`), nustatymai `/etc/setup-server/metrics.env`. |

Visi scriptai yra idempotentiški, juos galima kartoti. Su `--check` jie tik parodo, ką keistų,
ir nieko nekeičia. Pirmiausia paleisk būtent su `--check`.

```text
~/setup-server/                 šis repo, administratoriaus klonas (iš jo leidžiami scriptai)
/srv/setup-server/              paskyros setup namai
├── setup/                      bendras setup checkout'as (serveriuose – ant release tag'o)
├── projects/<vardas>/app/      projekto build katalogas (BUILD_ROOT = projects/<vardas>)
│   └── .generated/backups/     DB kopijos (worker-<env>-<UTC laikas>Z.sql.gz – naktinės ir „backup dabar“)
├── runner/current/             runner'io versija; runner/temp – jo darbo katalogas
└── .ssh/id_ed25519             git raktas (tik skaitymui)
/etc/setup-server/runner.env    runner'io OAuth duomenys (root, 0600)
/etc/setup-server/projects/<vardas>/<env>.env   projekto slaptažodžiai naktinėms kopijoms (root, 0600)
/etc/setup-server/metrics.env   worker'io /metrics token'as, adresas ir leidžiami adresai (root, 0600)
```

## Eiga

1. **Serveryje** nusiklonuok šį repo ir patikrink mašiną:

   ```bash
   git clone git@github.com:BigSant/DockerDevelopmentEnvironmentServer.git ~/setup-server
   cd ~/setup-server
   bin/prepare-host --check
   ```

   Peržiūra parodo Ubuntu versiją, RAM, laisvą vietą, trūkstamus paketus, ar yra Docker ir
   ar Compose pakankamai naujas, taip pat ką scriptas sukurtų. Runner'iui reikia **≥ 8 GB RAM**
   ir **OpenJDK 25**.

2. **Paruošk** (reikia sudo):

   ```bash
   sudo bin/prepare-host --setup-ref v2.0.0
   ```

   Pabaigoje parodomas viešasis git raktas. Pridėk jį **tik skaitymui** prie setup repo (GitHub,
   deploy key) ir prie kiekvieno projekto repo (Bitbucket, Access keys). Tada paleisk scriptą dar
   kartą: jis nusiklonuos setup. Palygink parodytus `bitbucket.org` ir `github.com` SSH
   fingerprint'us su paslaugų paskelbtais.

3. **Runner'is.** Bitbucket: *Repository (arba Workspace) settings → Runners → Add runner → Linux
   Shell*, pridėk žymą `setup.build`. Dialogo komandų **nevykdyk** – jose esančias reikšmes scriptas
   paklaus po vieną:

   ```bash
   sudo bin/install-runner --check   # klausia reikšmių, nieko nekeičia
   sudo bin/install-runner           # klausia tų pačių reikšmių, įdiegia
   ```

   Klausiama: atsisiuntimo nuoroda (galima įklijuoti visą `curl …` eilutę), `accountUuid`,
   `repositoryUuid` (workspace runner'iui – Enter), `runnerUuid`, `OAuthClientId`, `OAuthClientSecret`
   (rašant nerodomas). Galima įklijuoti ir `--accountUuid {…}` – paimama tik reikšmė; tuščia reikšmė –
   klaida. Archyvas imamas tik iš `product-downloads.atlassian.com` (kitą galima nurodyti `--tarball`).
   Visą dialogo tekstą galima ir paduoti per stdin (`sudo bin/install-runner < dialogas.txt`).

   OAuth duomenys įrašomi tik į `/etc/setup-server/runner.env` (root, 0600); dialogo teksto niekur
   kitur nekopijuok. Pats runner'is juos gauna kaip `start.sh` argumentus (taip jį sukūrė Atlassian),
   todėl šiame serveryje jie matomi `ps`. Po minutės Bitbucket'e runner'is turi būti **ONLINE**. Kitas
   runner'is (pvz. workspace vietoj repository) įdiegiamas paleidžiant tą patį scriptą su naujomis
   reikšmėmis.

4. **Projektas:**

   ```bash
   sudo bin/add-project git@bitbucket.org:rsmaizys/asana_tracker.git
   sudo bin/add-project git@bitbucket.org:rsmaizys/forsena_docker.git git@bitbucket.org:rsmaizys/forsena.git
   ```

   Build katalogas pavadinamas pagal repo vardą (`/srv/setup-server/projects/asana_tracker`), todėl
   pipeline'as jį randa pats: `BUILD_ROOT=${BUILD_ROOT:-$SETUP_SERVER_ROOT/projects/$BITBUCKET_REPO_SLUG}`
   (`SETUP_SERVER_ROOT` perduoda runner'io paslauga, `BITBUCKET_REPO_SLUG` – Bitbucket). Repository
   variable `BUILD_ROOT` reikia tik nurodžius kitą vardą (`add-project VARDAS URL`). Slaptažodžius įrašyk
   į Deployment variables (Secured). Kiekvienas pipeline žingsnis turi turėti
   `runs-on: [self.hosted, linux.shell, setup.build]`, kitaip jis vyktų Bitbucket debesyje ir naudotų
   minutes. Žingsnio scriptas turi prasidėti `unset DOCKER_HOST`: Bitbucket nustato
   `DOCKER_HOST=tcp://localhost:2375` savo debesies docker servisui, o shell runner'yje Docker yra paties
   serverio (unix socket'as) – kitaip `docker` ir `make release/deploy` neranda daemon'o.

5. **Projekto pašalinimas:**

   ```bash
   ls /srv/setup-server/projects/
   sudo bin/remove-project --check asana_tracker    # parodo dydį, kopijas, deploy'ų istoriją
   sudo bin/remove-project asana_tracker
   ```

   Jei kataloge yra DB kopijų ar deploy'ų istorijos, reikia `--with-backups` – tada prarandamos kopijos
   ir rollback'as. Katalogo, kurį naudoja veikiantis konteineris, scriptas netrina.

6. **Naktinės DB kopijos** (worker'is, ne Bitbucket):

   ```bash
   sudo bin/install-backups --check          # unit'ai ir ką šią naktį kopijuotų; nieko nekeičia
   sudo bin/install-backups                  # įdiegia; --at 03:30 – paleidimo laikas (numatytas)
   ```

   **Kurie projektai ir aplinkos.** Kopijuojama kiekviena `/srv/setup-server/projects/<vardas>` aplinka,
   kuriai aplinkos repo (`app/`, senesniame išdėstyme `app/docker/`) turi `env/stage.env` arba `env/prod.env`.
   Šį failą sukuria projekto deploy pipeline'as (`cp env/prod.env.example env/prod.env`), todėl kopijuojama
   tai, kas iš šio serverio deploy'inama. `local` ir `test` nekopijuojamos. Kopija – setup `make db-backup
   ENV=<env>`: stage/prod pagal lentelių registrą, su `DATABASE_EXTRA_SCHEMAS` – visos schemos vienu dump'u
   (asana.local). Kopijuojama tik duomenų bazė: `data/` katalogai (paieškos indeksai ir pan.) nekopijuojami,
   o kurios schemos ir lentelės patenka į dump'ą, nusprendžia projekto `DATABASE_EXTRA_SCHEMAS` ir registras.

   **Slaptažodžiai.** Laikmatis veikia be Bitbucket, todėl Deployment variables jam reikia serveryje.
   Kiekvienai aplinkai – root failas (tie patys `VARDAS=reikšmė`, kaip Bitbucket Deployment variables:
   `DATABASE_PASSWORD`, `DATABASE_ROOT_PASSWORD`, `METRICS_TOKEN`, PS raktai …):

   ```bash
   sudo install -d -m 0700 /etc/setup-server/projects/asana_tracker
   sudo install -m 0600 /dev/null /etc/setup-server/projects/asana_tracker/prod.env
   sudoedit /etc/setup-server/projects/asana_tracker/prod.env
   ```

   Failą skaito tik root; `make db-backup` vykdomas paskyra `setup`, slaptažodžiai jai perduodami per
   aplinką (ne argumentais), išvestyje jie nerodomi. Failas, kuris ne root ar ne 0600, atmetamas. Pašalinus
   projektą (`remove-project`) jo katalogą `/etc/setup-server/projects/<vardas>` ištrink rankomis.

   **Rotacija.** Kiekvienam projektui ir aplinkai paliekama naujausia kiekvienos iš 7 paskutinių dienų,
   kurios turi kopiją, kopija ir dar 4 savaičių (ISO savaitė) naujausios kopijos – iš viso iki 11, maždaug
   5 savaitės atgal. Senesnės ištrinamos tik po sėkmingos naujos kopijos; nepavykus nieko netrinama.
   Liečiami tik `worker-<env>-…Z.sql.gz` failai: deploy'aus kopijos (`<projektas>-<env>-….sql.gz`) ir kiti
   failai lieka (jas valo setup `make db-backup-prune`). Kopijos laikomos tik šio serverio diske.

   **Klaidos.** Vieno projekto klaida kitų nestabdo; pabaigoje `==> Backups: N ok, M failed: projektas/env`,
   o paslauga baigiasi klaida (`failed`). Patikrinti:

   ```bash
   systemctl list-timers setup-server-backup.timer      # kada kitas paleidimas
   sudo systemctl start setup-server-backup.service     # paleisti dabar (laukia pabaigos)
   systemctl status setup-server-backup.service         # paskutinio paleidimo būsena
   journalctl -u setup-server-backup.service -n 100     # kas nukopijuota, kas nepavyko
   sudo setup-server-backup --check                     # ką kopijuotų, ar yra slaptažodžių failai
   ```

   **„Backup dabar“** – Bitbucket custom pipeline'as, vykdomas runner'io šiame serveryje. Jis kviečia tą pačią
   komandą per `sudo` (taisyklė leidžia paskyrai `setup` tik `/usr/local/sbin/setup-server-backup`), todėl
   naudoja tuos pačius root slaptažodžių failus, rotaciją ir nekuria įrašo Deployments istorijoje:

   ```yaml
   pipelines:
     custom:
       backup-now:            # „backup dabar“
         - step:
             name: Backup now
             runs-on: [self.hosted, linux.shell, setup.build]
             script:
               - unset DOCKER_HOST
               - BUILD_ROOT=${BUILD_ROOT:-${SETUP_SERVER_ROOT:-/srv/setup-server}/projects/$BITBUCKET_REPO_SLUG}
               - sudo -n /usr/local/sbin/setup-server-backup "$BUILD_ROOT"
   ```

   Antras argumentas (`stage` arba `prod`) apriboja aplinką. Atnaujinus šį repo, `sudo bin/install-backups`
   pakartok – įdiegta komandos kopija atnaujinama.

## Atnaujinimas

Viena komanda atnaujina abu checkout'us: šį repo (`~/setup-server`, administratoriaus klonas, iš kurio
paleista komanda) ir bendrą setup checkout'ą `/srv/setup-server/setup` (paskyros `setup`). Abu repo yra
GitHub'e, todėl atnaujinama rankomis serveryje, ne per Pipelines.

```bash
cd ~/setup-server
bin/update --check --server-ref v0.2.0 --setup-ref v2.0.0   # parodo, kas keistųsi
bin/update --server-ref v0.2.0 --setup-ref v2.0.0           # atnaujina, patikrina, nepavykus grąžina
```

- Leisk **be sudo**, kaip šio klono savininkas (setup-server parsiunčiamas tavo git raktu); setup checkout'o
  dalį scriptas pats vykdo per `sudo -u setup` (paprašys slaptažodžio). Paleistas su sudo – atsisako.
- Galima nurodyti tik vieną pusę (`--setup-ref` arba `--server-ref`); kita lieka, kur buvo.
- **Ref'as** – tag'as (pageidautina), `origin` šaka arba commit'o id. setup-server gali būti ir ne ant tag'o
  (tag'ų dar nėra). setup ne ant `vX.Y.Z` tag'o tinka **tik stage**: prod release'ui setup turi būti ant
  release tag'o – peržiūra tai parodo kaip pastabą.
- `--check` parsiunčia (`git fetch`) ir parodo kiekvieno checkout'o dabartinį ir naują ref'ą, commit'ų
  skaičių pirmyn/atgal, pakeistų failų suvestinę ir commit'ų sąrašą (`+` – atsiras, `-` – dings).
  Checkout'ų nekeičia.
- Atsisako, jei checkout'e yra vietinių pakeitimų, ref'as nežinomas (parodo naujausius tag'us) arba vyksta
  pipeline žingsnis (paskyros `setup` `make` procesas) – tada palauk, kol jis baigsis.

**Sveikatos patikra po atnaujinimo** (visos trys, bet kuri nepavykusi = grįžimas):

1. setup-server testai (`python3 -m unittest discover -s tests`) iš naujos versijos – patikrina, kad
   scriptai veikia šiame serveryje;
2. setup checkout'as yra ant naujo commit'o ir paskyra `setup` jį skaito be pakeitimų – būtent jį
   pipeline'ai naudoja (`SETUP_DIRECTORY`);
3. runner'io paslauga `setup-server-runner` tebėra aktyvi **su tuo pačiu PID** – tai įrodo, kad ji
   nebuvo perkrauta. Jei prieš atnaujinimą ji neveikė, tai tik parodoma.

Runner'io paslauga niekada neperkraunama: ji leidžiama iš `/srv/setup-server/runner/current`, ne iš šio
repo, o setup checkout'ą kiekvienas pipeline žingsnis skaito iš naujo. Ar runner'is ONLINE, patikrink
Bitbucket'e (scriptas Bitbucket API neskaito).

Nepavykus patikrai abu checkout'ai grąžinami į ankstesnius commit'us (ar šaką, jei checkout'as buvo ant
jos), išvedama, kuri patikra nepavyko, ir grąžinamas kodas 1. Kodas 2 reiškia, kad nepavyko ir grįžimas –
išvedamos komandos rankiniam atstatymui.

### Rankinis atstatymas

Kai `bin/update` baigiasi „The automatic return failed“ (kodas 2) arba buvo nutrauktas netikėtai:

1. **Ankstesni commit'ai.** Juos išveda eilutė `Previous commits (for a manual return): …`. Jei jos
   nebeturi – reflog'as (pirmoji eilutė prieš atnaujinimo `checkout`):

   ```bash
   git -C ~/setup-server reflog -n 5
   (cd / && sudo -u setup -H git -C /srv/setup-server/setup reflog -n 5)
   ```

2. **Kodėl nepavyko grįžti.** Dažniausiai – teisės (`.git` neįrašomas) arba sugadinti failai:

   ```bash
   ls -ld ~/setup-server/.git /srv/setup-server/setup/.git
   sudo chown -R "$USER": ~/setup-server
   sudo chown -R setup:setup /srv/setup-server/setup
   ```

3. **Grąžink** (komandos tos pačios, kurias išveda scriptas):

   ```bash
   git -C ~/setup-server checkout --force <setup-server commit'as>
   (cd / && sudo -u setup -H git -C /srv/setup-server/setup checkout --force <setup commit'as>)
   ```

4. **Patikrink:**

   ```bash
   cd ~/setup-server && python3 -m unittest discover -s tests
   (cd / && sudo -u setup -H git -C /srv/setup-server/setup describe --tags --always)
   systemctl is-active setup-server-runner      # active; Bitbucket'e runner'is ONLINE
   ```

   Jei runner'is neaktyvus, žr. `journalctl -u setup-server-runner -n 50` ir tik tada
   `sudo systemctl start setup-server-runner`.

5. **Jei checkout'as sugadintas** (git klaidos ir po `chown`): setup – perkelk seną į šalį ir leisk
   `prepare-host` nusiklonuoti iš naujo (kol nėra `setup/`, pipeline'ai nepavyks):

   ```bash
   sudo mv /srv/setup-server/setup /srv/setup-server/setup.broken
   sudo bin/prepare-host --setup-ref <tag'as>
   ```

   setup-server – nusiklonuok iš naujo greta ir sukeisk:

   ```bash
   git clone git@github.com:BigSant/DockerDevelopmentEnvironmentServer.git ~/setup-server.new
   git -C ~/setup-server.new checkout <commit'as>
   mv ~/setup-server ~/setup-server.broken && mv ~/setup-server.new ~/setup-server
   ```

## Worker'io metrikos

Worker'is atiduoda savo Prometheus `/metrics` – tai, ką žino tik jis: DB kopijos, deploy'ai, runner'is ir
diskas. Taikinio metrikos (sistema, svetainė, konteineriai, cron) – atskiras setup endpoint'as projekto
domene; šis jų nedubliuoja.

```bash
cd ~/setup-server
sudo bin/install-metrics --check --address 10.8.0.14 --allow 10.8.0.20   # parodo unit'ą ir nustatymus; nieko nekeičia
sudo bin/install-metrics --address 10.8.0.14 --allow 10.8.0.20           # įdiegia ir paleidžia
sudo sed -n 's/^METRICS_TOKEN=//p' /etc/setup-server/metrics.env         # token'as Prometheus'ui
```

- `--address` – kur klausoma: VPN serveryje jo VPN adresas (`10.8.0.14`); numatytas `127.0.0.1` (tik iš paties
  serverio). `--allow` – Prometheus adresas(-ai), IP/CIDR, atskirti tarpais ar kableliais; privalomas su bet
  kuriuo adresu, išskyrus `127.0.0.1`/`::1`. `--port` – numatytas `9101`. `--new-token` – naujas token'as.
- Nustatymai laikomi `/etc/setup-server/metrics.env` (root, 0600): pirmą kartą sugeneruojamas 43 simbolių
  token'as. Pakartotinai paleidus nenurodyti nustatymai lieka, paslauga perkraunama tik pasikeitus failui;
  runner'io paslauga neliečiama. Atnaujinus šį repo (`bin/update`), `sudo bin/install-metrics` pakartok –
  įdiegta komandos kopija atnaujinama.
- Prieiga kaip taikinio `/metrics`: ne iš sąrašo – `403`, be token'o ar su neteisingu – `401`, kitas kelias – `404`.
  Be 24+ simbolių token'o ar be sąrašo paslauga nepasileidžia (`journalctl -u setup-server-metrics`).
  `X-Forwarded-For` ignoruojamas: priešais nėra proxy, klientai jungiasi tiesiogiai.

**Kodėl atskira paslauga, ne nginx-proxy.** Worker'is neturi savo nginx-proxy (projektų nginx-proxy priklauso
projektams ir jų release'ams). Python standartinės bibliotekos HTTP serveris paskyra `setup` (build katalogų
savininkė) skaito failus tiesiogiai: be Docker socket'o, be root, be naujų paketų. systemd jį laiko tik
skaitančiu (`ProtectSystem=strict`, `NoNewPrivileges`, be capability). Klausoma vienu adresu, todėl
viešai jis nematomas; VPN adresą jis užima ir tada, kai VPN sąsaja pakyla vėliau (`IP_FREEBIND`).

**Metrikos** (žymos `project` – build katalogo vardas, `environment` – `stage`/`prod`):

| Metrika | Reikšmė |
| --- | --- |
| `setup_worker_backup_newest_age_seconds` | Kiek sekundžių praėjo nuo naujausios kopijos (failo laikas) |
| `setup_worker_backup_newest_size_bytes` | Naujausios kopijos dydis |
| `setup_worker_backups` | Kiek kopijų laikoma; `0` – aplinka deploy'inama iš čia (`env/<env>.env`), bet kopijos nėra |
| `setup_worker_deploy_last_timestamp_seconds` | Paskutinio deploy'aus laikas (ir nepavykusio) |
| `setup_worker_deploy_last_success` | `1` – paskutinis deploy'us baigtas; `0` – sustojo po rsync (`"failed": "after-rsync"`) |
| `setup_worker_deploy_last_success_timestamp_seconds` | Paskutinio baigto deploy'aus laikas |
| `setup_worker_deploy_last_rollback` | `1` – paskutinis deploy'us buvo `rollback=1` |
| `setup_worker_deploy_last_info{ref,commit}` | Paskutinio deploy'aus ref'as (iki 64 simbolių) ir commit'as (12 simbolių) |
| `setup_worker_runner_up`, `setup_worker_runner_state{state}`, `setup_worker_runner_restarts` | `setup-server-runner` būsena iš `systemctl show` (`active`, `failed`, `not-found` …) ir automatiniai perkrovimai |
| `setup_worker_disk_size_bytes`, `_used_bytes`, `_available_bytes` `{path}` | Failų sistema, kurioje `/srv/setup-server` (kaip `df`) |
| `setup_metrics_group_up{group}` | `0`, jei grupės (`backups`, `deploys`, `runner`, `disk`) dalies nepavyko perskaityti (priežastis – journal'e) |

Kopijos – tik `worker-<env>-…Z.sql.gz` (naktinės ir „backup dabar“). Deploy'aus kopijos neskaičiuojamos, kad
deploy'us neuždengtų sustojusio laikmačio. Deploy'ai – iš setup istorijos
`app/.generated/release/<env>/deployments.json` (paskutinis įrašas). Patikrinti be Prometheus:

```bash
systemctl status setup-server-metrics
sudo -u setup /usr/local/sbin/setup-server-metrics --once          # metrikos be serverio ir token'o
curl -H "Authorization: Bearer $(sudo sed -n 's/^METRICS_TOKEN=//p' /etc/setup-server/metrics.env)" http://10.8.0.14:9101/metrics
```

**Prometheus** (serveris prijungtas prie VPN, token'as faile):

```yaml
scrape_configs:
  - job_name: 'setup-worker'
    scheme: http                      # srautą šifruoja VPN
    metrics_path: /metrics
    scrape_interval: 1m
    authorization:
      credentials_file: /etc/prometheus/tokens/setup-worker
    static_configs:
      - targets: ['10.8.0.14:9101']
```

**Siūlomas Grafana įspėjimas „kopija senesnė nei 26 val.“** (įjungiamas kartu su kitais įspėjimais):

```promql
max by (project, environment) (setup_worker_backup_newest_age_seconds) > 26 * 3600
  or setup_worker_backups == 0
```

Papildomai verta: `up{job="setup-worker"} == 0` arba „no data“ (VPN ar paslauga neveikia),
`setup_metrics_group_up == 0`, `setup_worker_runner_up == 0`, `setup_worker_deploy_last_success == 0`.

## Saugumas

- Paskyra `setup` yra Docker grupėje, o tai šiame serveryje prilygsta root teisėms. Tai priimta,
  nes serveris yra ir build serveris, ir taikinys. Nutolusiems taikiniams šis klausimas sprendžiamas
  atskirai.
- Esamas Docker niekada nepakeičiamas ir neperkraunamas, todėl jau veikiantys konteineriai
  (dabartinė asana.local) lieka veikti.
- Slaptažodžiai į šį repo ir į projekto katalogus nerašomi. Naktinėms kopijoms jie laikomi tik
  `/etc/setup-server/projects/` (root, 0600). Sudo taisyklė `setup` paskyrai naujų teisių neprideda: ji ir
  taip Docker grupėje.
- Worker'io `/metrics` token'as yra tik `/etc/setup-server/metrics.env` (root, 0600); paslauga jį gauna per
  aplinką. Endpoint'as klauso tik nurodytu adresu (VPN), atsako tik leidžiamiems adresams su token'u ir nieko
  nekeičia.

## Dar neįtraukta

Šios dalys dar daromos (žemėlapis „Map — setup-server“):

- Prometheus prijungimas per VPN ir Grafana įspėjimai (kopija senesnė nei 26 val., „no data“);
- asana.local setup v2 forma ir perjungimas tame pačiame serveryje.

## Testai

```bash
python3 -m unittest discover -s tests                                    # be root ir be tinklo
SETUP_SERVER_UBUNTU_IMAGE=ubuntu:24.04 python3 -m unittest discover -s tests   # + tikras Ubuntu konteineris
SETUP_PROMTOOL_IMAGE=prom/prometheus:latest python3 -m unittest discover -s tests  # + metrikų formato patikra (be promtool)
```
