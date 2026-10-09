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
| `bin/install-backups` | Naktinės DB kopijos: systemd laikmatis `setup-server-backup.timer`, komanda `/usr/local/sbin/setup-server-backup` (kopija iš `bin/backup-projects`) ir sudo taisyklė „backup dabar“ pipeline'ui. |

Visi scriptai yra idempotentiški, juos galima kartoti. Su `--check` jie tik parodo, ką keistų,
ir nieko nekeičia. Pirmiausia paleisk būtent su `--check`.

```text
/srv/setup-server/              paskyros setup namai
├── setup/                      bendras setup checkout'as (serveriuose – ant release tag'o)
├── projects/<vardas>/app/      projekto build katalogas (BUILD_ROOT = projects/<vardas>)
│   └── .generated/backups/     DB kopijos (worker-<env>-<UTC laikas>Z.sql.gz – naktinės ir „backup dabar“)
├── runner/current/             runner'io versija; runner/temp – jo darbo katalogas
└── .ssh/id_ed25519             git raktas (tik skaitymui)
/etc/setup-server/runner.env    runner'io OAuth duomenys (root, 0600)
/etc/setup-server/projects/<vardas>/<env>.env   projekto slaptažodžiai naktinėms kopijoms (root, 0600)
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

## Saugumas

- Paskyra `setup` yra Docker grupėje, o tai šiame serveryje prilygsta root teisėms. Tai priimta,
  nes serveris yra ir build serveris, ir taikinys. Nutolusiems taikiniams šis klausimas sprendžiamas
  atskirai.
- Esamas Docker niekada nepakeičiamas ir neperkraunamas, todėl jau veikiantys konteineriai
  (dabartinė asana.local) lieka veikti.
- Slaptažodžiai į šį repo ir į projekto katalogus nerašomi. Naktinėms kopijoms jie laikomi tik
  `/etc/setup-server/projects/` (root, 0600). Sudo taisyklė `setup` paskyrai naujų teisių neprideda: ji ir
  taip Docker grupėje.

## Dar neįtraukta

Šios dalys dar sprendžiamos žemėlapyje „Map — setup-server“:

- išorinio Prometheus prieiga prie serverio `/metrics` ir TLS;
- kopijų amžius worker'io metrikose ir įspėjimas, kai kopija senesnė nei 26 val.;
- asana.local perjungimas į setup v2 tame pačiame serveryje;
- setup-server atnaujinimas per Pipelines.

## Testai

```bash
python3 -m unittest discover -s tests                                    # be root ir be tinklo
SETUP_SERVER_UBUNTU_IMAGE=ubuntu:24.04 python3 -m unittest discover -s tests   # + tikras Ubuntu konteineris
```
