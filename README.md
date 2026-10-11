# setup-server – build serverio ir taikinio paruošimas

Scriptai, kurie paruošia Ubuntu serverį setup v2 projektams. Šis serveris kartu yra ir
**build serveris** (Bitbucket Pipelines ir/ar GitHub Actions self-hosted runner'iai, image'ų ir release'ų
kūrimas), ir **taikinys** (ten veikia projektų konteineriai). Deploy'ai valdomi Bitbucket'e arba GitHub'e,
o darbą atlieka runner'is šiame serveryje, todėl debesies build minutės nenaudojamos. Abu runner'iai gali
veikti tame pačiame serveryje kartu.

Pirmasis serveris – VPN serveris (Ubuntu Desktop), pirmasis projektas – asana.local.

## Ką paruošia

| Scriptas | Ką daro |
| --- | --- |
| `bin/prepare-host` | Paketai (`git`, `make`, `rsync`, `python3`, OpenJDK 25 runner'iui), Docker su Compose (jei jo nėra – iš download.docker.com; esamas Docker nekeičiamas), paskyra `setup` su Docker grupe, katalogai `/srv/setup-server`, git raktas, bendras setup checkout'as. |
| `bin/install-runner` | Bitbucket Linux Shell runner'is kaip systemd paslauga `setup-server-runner` (paskyra `setup`); su `--provider github` – GitHub Actions runner'is, paslauga `setup-server-github-runner@<scope>` (po vieną organizacijai ar repo). |
| `bin/add-project` | Projekto build katalogas `/srv/setup-server/projects/<repo vardas>`: aplinkos repo `app/`, atskiras kodo repo `app/public/`. GitHub repo – su savo deploy key. |
| `bin/remove-project` | Pašalina projekto build katalogą; DB kopijas ir deploy'ų istoriją – tik su `--with-backups`; katalogo, kurį naudoja veikiantis konteineris, netrina. |
| `bin/update` | Atnaujina šį setup-server checkout'ą ir bendrą setup checkout'ą į nurodytą tag'ą, patikrina sveikatą, nepavykus grąžina abu į ankstesnius commit'us. Runner'io paslaugos neperkrauna. |
| `bin/install-backups` | Naktinės DB kopijos: systemd laikmatis `setup-server-backup.timer`, komanda `/usr/local/sbin/setup-server-backup` (kopija iš `bin/backup-projects`) ir sudo taisyklė „backup dabar“ pipeline'ui. |
| `bin/add-backup-key` | Programuotojo SSH raktas naujausioms DB kopijoms atsisiųsti (`make db-pull`): tik iš nurodyto tinklo (VPN), tik komanda `/usr/local/sbin/setup-server-backup-pull` (kopija iš `bin/backup-pull`), be shell'o ir Docker. |
| `bin/install-metrics` | Worker'io Prometheus `/metrics`: systemd paslauga `setup-server-metrics` (paskyra `setup`, tik skaito), komanda `/usr/local/sbin/setup-server-metrics` (kopija iš `bin/worker-metrics`), nustatymai `/etc/setup-server/metrics.env`. |

Visi scriptai yra idempotentiški, juos galima kartoti. Su `--check` jie tik parodo, ką keistų,
ir nieko nekeičia. Pirmiausia paleisk būtent su `--check`.

```text
~/setup-server/                 šis repo, administratoriaus klonas (iš jo leidžiami scriptai)
/srv/setup-server/              paskyros setup namai
├── setup/                      bendras setup checkout'as (serveriuose – ant release tag'o)
├── projects/<vardas>/app/      projekto build katalogas (BUILD_ROOT = projects/<vardas>)
│   └── .generated/backups/     DB kopijos (worker-<env>-<UTC laikas>Z.sql.gz – naktinės ir „backup dabar“)
├── runner/current/             Bitbucket runner'io versija; runner/temp – jo darbo katalogas
├── github-runner/<scope>/      GitHub runner'is (organizacijai `bigsant`, repo `bigsant.<repo>`); _work – darbo katalogas
├── .ssh/id_ed25519             git raktas (tik skaitymui): setup repo ir Bitbucket projektai
└── .ssh/github-<savininkas>.<repo>   GitHub projekto deploy key (tik skaitymui); host alias'ai – .ssh/config
/etc/setup-server/runner.env    Bitbucket runner'io OAuth duomenys (root, 0600)
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
   deploy key) ir prie kiekvieno Bitbucket projekto repo (Access keys); GitHub projekto repo gauna savo
   raktą (žr. 4 žingsnį). Tada paleisk scriptą dar
   kartą: jis nusiklonuos setup. `bitbucket.org` ir `github.com` SSH host raktai įrašomi tik tada, kai
   nuskenuoto rakto fingerprint'as sutampa su paslaugos paskelbtu (jie įrašyti scripte su nuorodomis į
   GitHub ir Atlassian puslapius); kitaip raktas neįrašomas ir parodoma „Needs attention“.

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
   kitur nekopijuok. Kol runner'is veikia, `OAuthClientId` ir `OAuthClientSecret` šiame serveryje
   perskaitomi (taip jį sukūrė Atlassian):

   - **komandų eilutėje**: systemd perduoda juos `start.sh` argumentais, o šis – `java` procesui kaip
     `-D…` parametrus; `ps` ir `/proc/<PID>/cmdline` juos rodo **visoms** serverio paskyroms;
   - **aplinkoje** (`RUNNER_ARGS` iš root failo): `/proc/<PID>/environ` skaito paskyra `setup`, taigi ir
     visi jos procesai, **įskaitant pipeline žingsnius**.

   `start.sh` (6.x) reikšmes priima ir iš aplinkos kintamųjų (`OAUTH_CLIENT_SECRET` ir kt.), konfigūracijos
   failo neturi, bet `java` procesui jas vis tiek perduoda komandų eilutėje, todėl tai nieko nepaslėptų –
   paliktas argumentų būdas. Dėl to į šį serverį neįleisk paskyrų, kurios neturi matyti šių duomenų.

   Po minutės Bitbucket'e runner'is turi būti **ONLINE**. Kitas runner'is (pvz. workspace vietoj
   repository) įdiegiamas paleidžiant tą patį scriptą su naujomis reikšmėmis.

   **GitHub Actions runner'is** (projektams GitHub'e; veikia greta Bitbucket runner'io, jo neliečia).
   GitHub: *organizacijos* (rekomenduojama: vienas runner'is visiems organizacijos repo) arba *repo
   Settings → Actions → Runners → New self-hosted runner*, Linux. Organizacijos runner'iui reikia
   organizacijos savininko, repo – repo administratoriaus teisių. Puslapio komandų **nevykdyk**:

   ```bash
   sudo bin/install-runner --provider github --check   # klausia URL ir token'o, nieko nekeičia
   sudo bin/install-runner --provider github           # klausia tų pačių reikšmių, įdiegia
   ```

   Klausiama: organizacijos ar repo URL (`--url` reikšmė, pvz. `https://github.com/BigSant` arba
   `https://github.com/BigSant/<repo>`) ir registracijos token'as (`--token` reikšmė, rašant nerodomas).
   Galima paduoti ir visą puslapio tekstą per stdin (`sudo bin/install-runner --provider github < puslapis.txt`) –
   iš jo imami tik `--url` ir `--token`. Token'as **galioja valandą** ir registruoja vieną kartą: pasibaigus –
   atnaujink puslapį ir imk naują. Jį duoda ir API (`POST /orgs/{org}/actions/runners/registration-token`,
   `admin:org`; `POST /repos/{owner}/{repo}/actions/runners/registration-token`).

   - **Archyvas** – scripte prisegta versija (2.338.0) iš `github.com/actions/runner` releases; įdiegiama tik
     sutapus SHA-256 (įrašytas scripte, toks pat release puslapyje). Kita versija – `--tarball URL --sha256 HASH`.
     Puslapyje siūloma naujesnė versija tik parodoma: runner'is atsinaujina pats.
   - **Žymos.** GitHub pats prideda `self-hosted`, `Linux`, `X64`, scriptas – `setup.build`. Workflow job'ai:
     `runs-on: [self-hosted, linux, setup.build]` (runner'is turi turėti visas nurodytas žymas). `--no-default-labels`
     nenaudojamas: `self-hosted` tada būtų tik sava žyma, o `X64` netrukdo.
   - **Vardas** GitHub'e – serverio vardas (`hostname -s`), kitas – `--name`. Registruojama su `--replace`: įdiegus
     serverį iš naujo, tas pats vardas pakeičia seną registraciją.
   - **Kur.** Kiekvienas scope (organizacija ar repo) – atskiras katalogas `/srv/setup-server/github-runner/<scope>`
     (`bigsant`, `bigsant.<repo>`) ir atskira paslauga `setup-server-github-runner@<scope>` (vienas systemd šablonas
     `setup-server-github-runner@.service`). Kitai organizacijai ar repo – tas pats scriptas su kitu URL.
     Paslauga veikia paskyra `setup` su tais pačiais `SETUP_SERVER_ROOT` ir `SETUP_DIRECTORY`, kaip Bitbucket'o.
   - **Pakartotinai** paleistas su jau užregistruotu scope (`.runner` yra) – nei atsisiunčia, nei registruoja,
     token'o neklausia: įrašo unit'ą ir paleidžia paslaugą, jei ji neveikia. Veikiančios neperkrauna; pasikeitus
     unit'ui parodo `sudo systemctl restart …` – paleisk, kai nevyksta job'as.
   - **Token'as** niekur neįrašomas ir nerodomas: `config.sh` (jis atsisako veikti kaip root, todėl leidžiamas
     paskyra `setup`) jį gauna per aplinkos kintamąjį `ACTIONS_RUNNER_INPUT_TOKEN`, ne komandų eilutėje.
     Užsiregistravęs runner'is laiko savo duomenis savo kataloge (0750; `.runner`, `.credentials`, privatus raktas
     `.credentials_rsaparams` – 0600): juos skaito paskyra `setup`, taigi ir job'ai (kaip Bitbucket OAuth duomenis).
     Root failo nėra: runner'is veikia paskyra `setup` ir jam pačiam jų reikia.
   - **Atnaujinimas.** Runner'is atsinaujina pats (GitHub numatyta; neatnaujintam per 30 dienų nuo naujos versijos
     GitHub job'ų nebeduoda): pakeičia `bin/` savo kataloge, o paslauga leidžia `bin/runsvc.sh`, todėl po perkrovimo –
     naują versiją. Pagrindinis procesas (`runsvc.sh`) lieka tas pats, todėl `bin/update` patikra nenukenčia.
   - **Kodėl ne GitHub'o `svc.sh install setup`:** jo unit'o vardas priklauso nuo registracijos
     (`actions.runner.<…>.service`), o pipeline'ams reikia `SETUP_SERVER_ROOT`/`SETUP_DIRECTORY`. Savas unit'as
     kartoja GitHub šabloną (`KillMode=process`, `KillSignal=SIGTERM`, `TimeoutStopSec=5min` – vykstantis job'as
     baigiasi tvarkingai) ir prideda `Restart=on-failure`.
   - **Kodėl ne `--ephemeral`:** toks runner'is atlieka vieną job'ą ir išsiregistruoja, kitam reikia naujos
     registracijos (token'o iš API su PAT ar GitHub App). Build katalogai ir taip išlieka tarp job'ų.
   - **Prieiga.** Organizacijos runner'is patenka į *Default* runner group; pagal nutylėjimą grupės runner'iais
     naudojasi tik **privatūs** repo. Viešiems repo self-hosted runner'io neleisk: GitHub perspėja, kad bet kas gali
     atidaryti pull request'ą ir paleisti savo kodą šiame serveryje.

   Po minutės GitHub'e runner'is turi būti **Idle**. Patikrinti: `systemctl status setup-server-github-runner@bigsant`,
   `journalctl -u setup-server-github-runner@bigsant -n 50`. Pašalinti: `sudo systemctl disable --now
   setup-server-github-runner@<scope>`, GitHub'e *Runners → … → Remove runner*, tada
   `sudo rm -r /srv/setup-server/github-runner/<scope>`.

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

   **GitHub projektas:**

   ```bash
   sudo bin/add-project git@github.com:BigSant/<aplinkos-repo>.git [git@github.com:BigSant/<kodo-repo>.git]
   ```

   GitHub deploy key tinka **tik vienam** repo, o paskyros raktas `id_ed25519` jau yra setup repo deploy key. Todėl
   kiekvienas GitHub SSH URL gauna savo raktą `/srv/setup-server/.ssh/github-<savininkas>.<repo>` ir host alias'ą
   `github.com-<savininkas>.<repo>` paskyros `.ssh/config` (`IdentitiesOnly yes`, tikrinamas tas pats `github.com`
   host raktas), ir klonuojama per jį. Pirmą kartą scriptas sukuria raktą, parodo jį ir nuorodą
   `https://github.com/<savininkas>/<repo>/settings/keys` ir sustoja (kodas 1): pridėk jį (*Add deploy key*,
   „Allow write access“ **neįjungtas**) ir paleisk tą pačią komandą dar kartą. Jei yra „machine user“ paskyra, kurios
   raktas (paskyros `id_ed25519`) skaito visus repo, – `--shared-key`: klonuojama su juo, per paduotą URL.

   Build katalogas – repo vardas mažosiomis raidėmis, todėl workflow jį randa pats:
   `BUILD_ROOT=${BUILD_ROOT:-$SETUP_SERVER_ROOT/projects/<repo vardas mažosiomis>}` (bash'e:
   `repository=${GITHUB_REPOSITORY#*/}` ir `${repository,,}`; `SETUP_SERVER_ROOT` perduoda runner'io paslauga).
   Kitu vardu – Actions variable `BUILD_ROOT`. Slaptažodžiai – GitHub environment secrets. Job'as turi turėti
   `runs-on: [self-hosted, linux, setup.build]`. `unset DOCKER_HOST` nereikia: GitHub jo nenustato.

5. **Projekto pašalinimas:**

   ```bash
   ls /srv/setup-server/projects/
   sudo bin/remove-project --check asana_tracker    # parodo dydį, kopijas, deploy'ų istoriją
   sudo bin/remove-project asana_tracker
   ```

   Jei kataloge yra DB kopijų ar deploy'ų istorijos (`app/.generated` arba senesniame išdėstyme
   `app/docker/.generated`), reikia `--with-backups` – tada prarandamos kopijos ir rollback'as. Katalogo,
   kurį naudoja veikiantis konteineris, scriptas netrina. Projekto slaptažodžių katalogo
   `/etc/setup-server/projects/<vardas>` (naktinėms kopijoms) scriptas netrina – jis tai parodo; kai nebereikia,
   `sudo rm -r /etc/setup-server/projects/<vardas>`.

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
   komandą per `sudo`, todėl naudoja tuos pačius root slaptažodžių failus, rotaciją ir nekuria įrašo
   Deployments istorijoje. Sudo taisyklė leidžia paskyrai `setup` tik šią formą:
   `/usr/local/sbin/setup-server-backup /srv/setup-server/projects/<vardas> [stage|prod]` (sudo 1.9.10+,
   pvz. Ubuntu 24.04, tikrina reguliariąja išraiška). Senesnis sudo (Ubuntu 22.04: 1.9.9) turi tik
   šablonus: pirmas argumentas privalo būti build katalogas, o kitus argumentus atmeta pati komanda.
   Atnaujinus setup-server, pakartok `sudo bin/install-backups`, jei `bin/update` parodo pastabą, kad
   sudo taisyklė skiriasi.

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

   Antras argumentas (`stage` arba `prod`) apriboja aplinką. Įdiegtą komandos kopiją atnaujina `bin/update`
   (žr. „Atnaujinimas“). Pakartotinai paleistas `sudo bin/install-backups` palieka įdiegtą laiką, jei
   nenurodytas `--at`.

7. **Kopijų atsisiuntimas programuotojams (neprivaloma).** Programuotojas savo kompiuteryje setup komanda
   `make db-pull` parsisiunčia naujausią šio serverio išsaugotą projekto kopiją (naktinę, „backup dabar“ ar
   prieš deploy'ų – kuri naujausia) ir iš jos pasiruošia veikiančią aplinką. LIVE nepasiekiamas.

   ```bash
   sudo bin/add-backup-key ~/tomas.pub --name tomas --from 10.8.0.0/24 --check   # parodo, ką keistų
   sudo bin/add-backup-key ~/tomas.pub --name tomas --from 10.8.0.0/24           # įrašo raktą
   sudo bin/add-backup-key --list                                                # kam leista ir iš kur
   sudo bin/add-backup-key --remove tomas                                        # atima leidimą
   ```

   Raktas (programuotojo `.pub` failas, niekada ne privatus) įrašomas į paskyros `setup`
   `.ssh/authorized_keys` su apribojimais: `from="10.8.0.0/24"` – jungtis galima tik iš nurodyto tinklo (VPN;
   `0.0.0.0/0` atmetamas), `command="/usr/local/sbin/setup-server-backup-pull --key tomas"` – raktas vykdo tik
   šią komandą, `restrict` – be terminalo, port'ų ir agento persiuntimo. Shell'o, Docker ir kitų failų raktas
   negauna. Tas pats vardas pakartotinai – raktas pakeičiamas; kiti `authorized_keys` įrašai nekeičiami.
   Prisijungiama tik raktu (paskyra `setup` slaptažodžio neturi).

   Komanda moka du dalykus: `latest <projektas> <stage|prod>` – naujausios kopijos vardas, `get <projektas>
   <stage|prod> <failas>` – tos kopijos turinys. Kiekvienas prašymas (ir atmestas) įrašomas į
   `/srv/setup-server/backup-pulls.log`: laikas, rakto vardas, adresas, prašymas, rezultatas. Projekto vardas –
   build katalogo vardas (`projects/<vardas>`, pvz. `asana_tracker`).

   Kopijose yra tikri klientų duomenys (stage/prod pagal lentelių registrą): leisk tik tiems, kam jų reikia,
   ir atimk leidimą (`--remove`), kai nebereikia. Įdiegtą komandos kopiją atnaujina `bin/update`.

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
- **Įdiegtos komandų kopijos.** Kai keičiasi setup-server, po perjungimo (prieš sveikatos patikrą) per
  `sudo` perrašomos įdiegtos kopijos `/usr/local/sbin/setup-server-backup`, `/usr/local/sbin/setup-server-metrics` ir
  `/usr/local/sbin/setup-server-backup-pull` (tik tos, kurios įdiegtos) iš naujos versijos: `bin/install-backups --refresh`,
  `bin/install-metrics --refresh` ir `bin/add-backup-key --refresh`.
  Pasikeitus metrikų kopijai, perkraunama paslauga `setup-server-metrics` (runner'io – ne). Kiti failai
  (unit'ai, sudo taisyklė, nustatymai) nekeičiami; jei naujoje versijoje jie kitokie, išvedama pastaba
  `note: … differs` – tada pakartok `sudo bin/install-backups` (laikas lieka) arba `sudo bin/install-metrics`.
  Nepavykus perrašymui ar sveikatos patikrai, kartu su checkout'ais grąžinamos ir ankstesnės kopijos.
- **Ref'as** – tag'as (pageidautina), `origin` šaka arba commit'o id. setup-server gali būti ir ne ant tag'o
  (tag'ų dar nėra). setup ne ant `vX.Y.Z` tag'o tinka **tik stage**: prod release'ui setup turi būti ant
  release tag'o – peržiūra tai parodo kaip pastabą.
- `--check` parsiunčia (`git fetch`) ir parodo kiekvieno checkout'o dabartinį ir naują ref'ą, commit'ų
  skaičių pirmyn/atgal, pakeistų failų suvestinę ir commit'ų sąrašą (`+` – atsiras, `-` – dings).
  Checkout'ų nekeičia.
- Atsisako, jei checkout'e yra vietinių pakeitimų, ref'as nežinomas (parodo naujausius tag'us) arba vyksta
  pipeline žingsnis (paskyros `setup` `make` procesas) – tada palauk, kol jis baigsis.
- Tag'ai parsiunčiami be `--force`: jei `origin` tag'as perkeltas į kitą commit'ą (perrašytas), vietinis
  lieka, kur buvo, o `bin/update` (ir `--check`) baigiasi klaida, parodydamas tag'ą ir abu commit'us.
  Release tag'as neturi judėti – išsiaiškink, kas ir kodėl jį perkėlė.

**Sveikatos patikra po atnaujinimo** (visos trys, bet kuri nepavykusi = grįžimas):

1. setup-server testai (`python3 -m unittest discover -s tests`) iš naujos versijos – patikrina, kad
   scriptai veikia šiame serveryje;
2. setup checkout'as yra ant naujo commit'o ir paskyra `setup` jį skaito be pakeitimų – būtent jį
   pipeline'ai naudoja (`SETUP_DIRECTORY`);
3. runner'io paslauga `setup-server-runner` ir kiekviena prieš atnaujinimą veikusi GitHub runner'io paslauga
   `setup-server-github-runner@<scope>` tebėra aktyvios **su tuo pačiu PID** – tai įrodo, kad jos nebuvo
   perkrautos. Jei Bitbucket runner'is prieš atnaujinimą neveikė, tai tik parodoma; neveikusios GitHub paslaugos
   netikrinamos.

Runner'ių paslaugos niekada neperkraunamos: jos leidžiamos iš `/srv/setup-server/runner/current` ir
`/srv/setup-server/github-runner/<scope>`, ne iš šio repo, o setup checkout'ą kiekvienas pipeline žingsnis
skaito iš naujo. Ar runner'is ONLINE (GitHub'e – Idle), patikrink Bitbucket'e ar GitHub'e (scriptas jų API
neskaito).

Nepavykus patikrai abu checkout'ai grąžinami į ankstesnius commit'us (ar šaką, jei checkout'as buvo ant
jos), o įdiegtos komandų kopijos – į ankstesnes; išvedama, kuri patikra nepavyko, ir grąžinamas kodas 1. Kodas 2 reiškia, kad nepavyko ir grįžimas –
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

   Jei scriptas išvedė ir `sudo install … /usr/local/sbin/setup-server-…` eilutę, įvykdyk ją (ankstesnė
   kopija laikoma `/tmp/tmp.…` kataloge). Be jos įdiegtas kopijas iš grąžinto checkout'o perrašo
   `sudo bin/install-backups --refresh` ir `sudo bin/install-metrics --refresh`.

4. **Patikrink:**

   ```bash
   cd ~/setup-server && python3 -m unittest discover -s tests
   (cd / && sudo -u setup -H git -C /srv/setup-server/setup describe --tags --always)
   systemctl is-active setup-server-runner      # active; Bitbucket'e runner'is ONLINE
   systemctl is-active 'setup-server-github-runner@*'   # active; GitHub'e runner'is Idle
   ```

   Jei runner'is neaktyvus, žr. `journalctl -u setup-server-runner -n 50` (`-u setup-server-github-runner@<scope>`)
   ir tik tada `sudo systemctl start setup-server-runner` (`setup-server-github-runner@<scope>`).

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
  runner'io paslauga neliečiama. Įdiegtą komandos kopiją atnaujina `bin/update` (žr. „Atnaujinimas“).
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
| `setup_worker_runner_up`, `setup_worker_runner_state{state}`, `setup_worker_runner_restarts` | `setup-server-runner` (Bitbucket) būsena iš `systemctl show` (`active`, `failed`, `not-found` …) ir automatiniai perkrovimai |
| `setup_worker_github_runner_up{scope}`, `setup_worker_github_runner_state{scope,state}`, `setup_worker_github_runner_restarts{scope}` | Tas pats kiekvienam užregistruotam GitHub runner'iui (`github-runner/<scope>` su `.runner`): paslauga `setup-server-github-runner@<scope>`. Nė vieno nėra – šių eilučių nėra |
| `setup_worker_disk_size_bytes`, `_used_bytes`, `_available_bytes` `{path}` | Failų sistema, kurioje `/srv/setup-server` (kaip `df`) |
| `setup_metrics_group_up{group}` | `0`, jei grupės (`backups`, `deploys`, `runner`, `github_runner`, `disk`) dalies nepavyko perskaityti (priežastis – journal'e) |

Bitbucket runner'io metrikos paliktos kaip buvo (be žymų), kad esami įspėjimai ir grafikai nesikeistų; GitHub
runner'iai – atskiros metrikos su žyma `scope`, nes jų gali būti keli.

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
`setup_metrics_group_up == 0`, `setup_worker_runner_up == 0`, `setup_worker_github_runner_up == 0`,
`setup_worker_deploy_last_success == 0`.

## Saugumas

- Paskyra `setup` yra Docker grupėje, o tai šiame serveryje prilygsta root teisėms. Tai priimta,
  nes serveris yra ir build serveris, ir taikinys. Nutolusiems taikiniams šis klausimas sprendžiamas
  atskirai.
- Esamas Docker niekada nepakeičiamas ir neperkraunamas, todėl jau veikiantys konteineriai
  (dabartinė asana.local) lieka veikti.
- Runner'io OAuth duomenys laikomi tik `/etc/setup-server/runner.env` (root, 0600), bet veikiančio runner'io
  komandų eilutėje jie matomi visoms serverio paskyroms, o aplinkoje – paskyros `setup` procesams ir pipeline
  žingsniams (žr. „Eiga“, 3 žingsnis).
- GitHub runner'io registracijos token'as niekur neįrašomas; užsiregistravusio runner'io raktas yra jo kataloge
  (paskyra `setup`, todėl pasiekiamas ir job'ams). Self-hosted runner'is – tik privatiems repo.
- GitHub projektų deploy key – po vieną kiekvienam repo, tik skaitymui (`/srv/setup-server/.ssh/github-…`).
- Slaptažodžiai į šį repo ir į projekto katalogus nerašomi. Naktinėms kopijoms jie laikomi tik
  `/etc/setup-server/projects/` (root, 0600). Sudo taisyklė `setup` paskyrai naujų teisių neprideda: ji ir
  taip Docker grupėje.
- Kopijų atsisiuntimo raktai (`bin/add-backup-key`) prisijungia prie paskyros `setup`, bet tik iš nurodyto tinklo
  ir tik su priverstine komanda, kuri atiduoda vien kopijas (`restrict`: be terminalo ir persiuntimų). Kopijose
  yra tikri klientų duomenys; kiekvienas atsisiuntimas registruojamas `backup-pulls.log`.
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
