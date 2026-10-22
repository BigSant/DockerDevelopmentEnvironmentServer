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
| `bin/install-backups` | DB kopijos: naktinė (`setup-server-backup.timer`, pagal lentelių registrą) ir savaitinė pilna (`setup-server-backup-full.timer`, visų lentelių duomenys), komanda `/usr/local/sbin/setup-server-backup` (kopija iš `bin/backup-projects`) ir sudo taisyklė „backup dabar“ pipeline'ui. |
| `bin/install-ssh` | SSH iš VPN: `openssh-server` (Ubuntu Desktop jo neturi), paleistas ir startuojantis po perkrovimo, nustatymai `/etc/ssh/sshd_config.d/10-setup-server.conf` – iš už nurodytų tinklų ir šio serverio tik raktu, be slaptažodžio; įjungtam ufw – leidimas tiems tinklams. Reikia `make db-pull` ir valdymo sąsajos tuneliui. |
| `bin/add-backup-key` | Programuotojo SSH raktas DB kopijoms atsisiųsti (`make db-pull`), su `--allow-fresh` – ir naujai kopijai užsakyti (`make db-pull fresh=1`): tik iš nurodyto tinklo (VPN), tik komanda `/usr/local/sbin/setup-server-backup-pull` (kopija iš `bin/backup-pull`), be shell'o ir Docker. Su `--grant PROJEKTAS:ENV=LYGIS` – ir prieiga prie to projekto taikinio per šį serverį (`make ssh`), per `/usr/local/sbin/setup-server-access` (kopija iš `bin/server-access`). |
| `bin/install-metrics` | Worker'io Prometheus `/metrics`: systemd paslauga `setup-server-metrics` (paskyra `setup`, tik skaito), komanda `/usr/local/sbin/setup-server-metrics` (kopija iš `bin/worker-metrics`), nustatymai `/etc/setup-server/metrics.env`. |
| `bin/install-ui` | Valdymo sąsaja naršyklėje (tik `127.0.0.1`, per SSH tunelį): paslauga `setup-server-ui` (atskira paskyra `setup-ui`), komanda `/usr/local/sbin/setup-server-ui` (kopija iš `bin/web-ui`), komandų kopijos `/usr/local/lib/setup-server/bin/`, sudo taisyklė, nustatymai `/etc/setup-server/ui.env`. |
| `bin/move-bitbucket-ssh` | Perkelia build katalogų Bitbucket remote'us iš `bitbucket.org` į `ssh.bitbucket.org` (Bitbucket SSH adresas nuo 2026-11-12); perkelia tik tada, kai naujas adresas atsako. Žr. „Bitbucket SSH adresas“. |
| `bin/server-status` | Serverio būsena JSON formatu (tik skaito): paslaugos, laikmačiai, versijos, diskas, projektai, konteineriai. Ją naudoja valdymo sąsaja; galima leisti ir ranka. |

Visi scriptai yra idempotentiški, juos galima kartoti. Su `--check` jie tik parodo, ką keistų,
ir nieko nekeičia. Pirmiausia paleisk būtent su `--check`.

```text
~/setup-server/                 šis repo, administratoriaus klonas (iš jo leidžiami scriptai)
/srv/setup-server/              paskyros setup namai
├── setup/                      bendras setup checkout'as (serveriuose – ant release tag'o)
├── projects/<vardas>/app/      projekto build katalogas (BUILD_ROOT = projects/<vardas>)
│   └── .generated/backups/     DB kopijos: worker-<env>-<UTC laikas>Z.sql.gz – naktinės ir „backup dabar“,
│                               full-<env>-…Z – savaitinės pilnos, fresh(-full)-<env>-…Z – programuotojų „fresh“
├── runner/current/             Bitbucket runner'io versija; runner/temp – jo darbo katalogas
├── github-runner/<scope>/      GitHub runner'is (organizacijai `bigsant`, repo `bigsant.<repo>`); _work – darbo katalogas
├── ui-actions.log              valdymo sąsajos veiksmų žurnalas (setup-ui, 0640)
├── backup-pulls.log, access.log   programuotojų raktų prašymai: kopijos ir prieiga prie taikinių
├── .ssh/id_ed25519             git raktas (tik skaitymui): setup repo ir Bitbucket projektai
└── .ssh/github-<savininkas>.<repo>   GitHub projekto deploy key (tik skaitymui); host alias'ai – .ssh/config
/etc/setup-server/runner.env    Bitbucket runner'io OAuth duomenys (root, 0600)
/etc/setup-server/metrics.env   worker'io /metrics adresas, leidžiami adresai ir neprivalomas token'as (root, 0600)
/etc/setup-server/ui.env        valdymo sąsajos prisijungimas (slaptažodžio hash'as) ir Bitbucket token'ai (root, 0600)
/usr/local/lib/setup-server/bin/   komandų kopijos, kurias valdymo sąsaja vykdo per sudo (root)
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
   kartą: jis nusiklonuos setup. `bitbucket.org`, `ssh.bitbucket.org` ir `github.com` SSH host raktai įrašomi tik tada, kai
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
   sudo bin/add-project --target local git@ssh.bitbucket.org:rsmaizys/asana_tracker.git
   sudo bin/add-project --target prod=ssh://deploy@shop.example/home/deploy/shop git@ssh.bitbucket.org:rsmaizys/forsena_docker.git git@ssh.bitbucket.org:rsmaizys/forsena.git
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

   **Kur deploy'inama (taikinys)** – serverio, ne projekto žinojimas. `add-project` jį įrašo į build katalogo
   `target.env` (setup 2.6.0+ jį prideda prie `env/<env>.env` kiekvieno deploy'aus metu), o projekto
   `env/*.env.example` `DEPLOY_CONTEXT`, `DEPLOY_SSH`, `DEPLOY_PATH`, `APP_UID`, `APP_GID` nebemini (setup
   tokį pavyzdį atmeta):

   ```bash
   sudo bin/add-project --target local git@ssh.bitbucket.org:rsmaizys/asana_tracker.git   # šis serveris
   sudo bin/add-project --target prod=ssh://deploy@shop.example/home/deploy/shop git@ssh.bitbucket.org:rsmaizys/forsena_docker.git
   ```

   - **Taikinys nurodomas visada:** naujas projektas be `--target` nesukuriamas (nieko neklonuoja).
   - **`local`** – šis serveris (`DEPLOY_CONTEXT=default`), projekto šaknis yra build katalogas,
     konteineriai veikia paskyra `setup` (jos `APP_UID`/`APP_GID`).
   - **`local:/KELIAS`** – šis serveris, bet projekto šaknis kitame kataloge (pvz. `local:/home/vardas/Projects/asana`).
     Nesantį katalogą scriptas sukuria paskyrai `setup` (0750); esamas turi būti jai rašomas. Failai ir konteineriai –
     paskyros `setup`. Deploy'us ten kopijuoja kodą, o setup 2.7.0+ ir konteinerių prijungiamus aplinkos failus
     (`config/` ir kt.). Jei failai turi priklausyti katalogo savininkui, naudok `ssh://SAVININKAS@localhost/KELIAS`
     (tada kopijuojama ir konteineriai veikia jo vardu; jam reikia `setup` rakto ir `docker` grupės).
   - **`ssh://VARTOTOJAS@SERVERIS/KELIAS`** – kitas serveris: paskyra `setup` jungiasi savo raktu
     (`/srv/setup-server/.ssh/id_ed25519`; jei negali – scriptas parodo raktą, kurį reikia įdėti į
     `VARTOTOJAS` `authorized_keys`, ir sustoja), patikrina Docker ir ar `KELIAS` rašomas, perima vartotojo
     `id -u`/`id -g` ir sukuria Docker context'ą `setup-<vardas>[-<env>]`. Naujas host raktas įrašomas
     pirmo prisijungimo metu (parodomas).
   - **`stage=…` / `prod=…`** – tik viena aplinka (`target.<env>.env`, laimi prieš bendrą `target.env`),
     kai stage ir prod skirtinguose serveriuose. `--target` galima kartoti.
   - Pakartotinai paleistas be `--target` taikinio nekeičia; su `--target` – perrašo. `remove-project`
     pašalina ir build katalogą (su `target*.env`), ir projekto Docker context'us.

   **GitHub projektas:**

   ```bash
   sudo bin/add-project --target local git@github.com:BigSant/<aplinkos-repo>.git [git@github.com:BigSant/<kodo-repo>.git]
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
   kurį naudoja veikiantis konteineris, scriptas netrina.

6. **DB kopijos: naktinės ir savaitinės pilnos** (worker'is, ne Bitbucket):

   ```bash
   sudo bin/install-backups --check          # unit'ai ir ką šią naktį kopijuotų; nieko nekeičia
   sudo bin/install-backups                  # įdiegia; --at 03:30 ir --full-at "Sun 04:30" – laikai (numatyti)
   ```

   | Kopija | Laikmatis | Kas joje | Failai | Laikoma |
   | --- | --- | --- | --- | --- |
   | Naktinė | `setup-server-backup.timer`, kasdien `--at` | pagal lentelių registrą: visų lentelių struktūra, `import` lentelių duomenys | `worker-<env>-…Z.sql.gz` | 7 dienų + 4 savaičių |
   | Savaitinė pilna | `setup-server-backup-full.timer`, kartą per savaitę `--full-at` | visų lentelių duomenys (`make db-backup full=1`) | `full-<env>-…Z.sql.gz` | 2 naujausios |
   | „Fresh“ | programuotojo `make db-pull fresh=1 [full=1]` (žr. 7 punktą) | registro arba pilna | `fresh-<env>-…Z`, `fresh-full-<env>-…Z` | po 2 naujausias |

   Kiekviena serija rotuojama atskirai, kitų failų rotacija neliečia. Vienam projektui ir aplinkai vienu metu
   daroma viena kopija (užraktas `.generated/backups/.lock-<env>`): jei naktinė dar vyksta, savaitinė ar „fresh“
   jos palaukia. Pilna kopija gali užtrukti iki 8 val., kitos – iki 4. Terminale (`sudo setup-server-backup --full
   asana_tracker prod`) rodoma juosta: parašyti baitai, palyginti su ankstesnės tokios pat kopijos dydžiu.

   **Kurie projektai ir aplinkos.** Kopijuojama kiekviena `/srv/setup-server/projects/<vardas>` aplinka,
   kuriai aplinkos repo (`app/`, senesniame išdėstyme `app/docker/`) turi `env/stage.env` arba `env/prod.env`.
   Šį failą sukuria projekto deploy pipeline'as (`cp env/prod.env.example env/prod.env`), todėl kopijuojama
   tai, kas iš šio serverio deploy'inama. `local` ir `test` nekopijuojamos. Kopija – setup `make db-backup
   ENV=<env>`: stage/prod pagal lentelių registrą, su `DATABASE_EXTRA_SCHEMAS` – visos schemos vienu dump'u
   (asana.local). Kopijuojama tik duomenų bazė: `data/` katalogai (paieškos indeksai ir pan.) nekopijuojami,
   o kurios schemos ir lentelės patenka į dump'ą, nusprendžia projekto `DATABASE_EXTRA_SCHEMAS` ir registras.

   **Slaptažodžiai – tik Bitbucket'e.** Serveryje jų kopijos nėra. Laikmatis veikia be Bitbucket'o, todėl
   `make db-backup` (paskyra `setup`) juos ima iš projekto stack'o: setup 2.5.0+ kiekvienoje stage/prod
   aplinkoje laiko servisą `<PROJECT_NAME>-<env>-secrets`, kurį kiekvienas deploy'us užpildo Bitbucket
   Deployment variables reikšmėmis (setup `doc/lt/09-aplinkos.md`, „Komandos serveryje be pipeline'o“).
   Pakeitus slaptažodį Bitbucket'e, kopija naudoja naują po kito deploy'aus – nieko daugiau keisti nereikia.
   Projektas, kuris dar nedeploy'intas su setup 2.5.0+ (store'o nėra), kopijuojamas nebus: `--check` ir
   žurnalas tai įvardija (`no secret store …`). Su `DEPLOY_CONTEXT` store'as ieškomas per jį (paskyros
   `setup` Docker context'ai). Senas `/etc/setup-server/projects/<vardas>/<env>.env` nebenaudojamas –
   `--check` (su sudo) primena jį ištrinti.

   **Rotacija.** Kiekvienam projektui ir aplinkai paliekama naujausia kiekvienos iš 7 paskutinių dienų,
   kurios turi kopiją, kopija ir dar 4 savaičių (ISO savaitė) naujausios kopijos – iš viso iki 11, maždaug
   5 savaitės atgal. Senesnės ištrinamos tik po sėkmingos naujos kopijos; nepavykus nieko netrinama.
   Liečiami tik `worker-<env>-…Z.sql.gz` failai: deploy'aus kopijos (`<projektas>-<env>-….sql.gz`) ir kiti
   failai lieka (jas valo setup `make db-backup-prune`). Kopijos laikomos tik šio serverio diske.

   **Klaidos.** Vieno projekto klaida kitų nestabdo; pabaigoje `==> Backups: N ok, M failed: projektas/env`,
   o paslauga baigiasi klaida (`failed`). Patikrinti:

   ```bash
   systemctl list-timers 'setup-server-backup*'          # kada kitas naktinis ir savaitinis paleidimas
   sudo systemctl start setup-server-backup.service     # naktinė dabar (laukia pabaigos)
   sudo systemctl start setup-server-backup-full.service  # savaitinė pilna dabar
   systemctl status setup-server-backup.service         # paskutinio paleidimo būsena
   journalctl -u setup-server-backup.service -n 100     # kas nukopijuota, kas nepavyko (-full – savaitinės)
   sudo setup-server-backup --check                     # ką kopijuotų, ar yra kiekvieno projekto secret store
   sudo setup-server-backup --check --full              # tas pats savaitinei pilnai kopijai
   ```

   **„Backup dabar“** – Bitbucket custom pipeline'as, vykdomas runner'io šiame serveryje. Jis kviečia tą pačią
   komandą per `sudo`, todėl naudoja tą patį secret store, rotaciją ir nekuria įrašo
   Deployments istorijoje. Sudo taisyklė leidžia paskyrai `setup` tik šią formą:
   `/usr/local/sbin/setup-server-backup [--full] /srv/setup-server/projects/<vardas> [stage|prod]` (sudo 1.9.10+,
   pvz. Ubuntu 24.04, tikrina reguliariąja išraiška). Senesnis sudo (Ubuntu 22.04: 1.9.9) turi tik
   šablonus: pirmas argumentas privalo būti build katalogas (ar `--full` ir build katalogas), o kitus
   argumentus atmeta pati komanda.
   Atnaujinus setup-server, pakartok `sudo bin/install-backups`, jei `bin/update` parodo pastabą, kad
   sudo taisyklė skiriasi.

   ```yaml
   pipelines:
     custom:
       backup-now:            # „backup dabar“
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

   Antras argumentas (`stage` arba `prod`) apriboja aplinką, `FULL=yes` – pilna kopija (į `full-<env>` seriją).
   Įdiegtą komandos kopiją atnaujina `bin/update` (žr. „Atnaujinimas“). Pakartotinai paleistas
   `sudo bin/install-backups` palieka įdiegtus laikus, jei nenurodyti `--at` ir `--full-at`.

7. **Kopijų atsisiuntimas programuotojams (neprivaloma).** Programuotojas savo kompiuteryje setup komanda
   `make db-pull` parsisiunčia naujausią šio serverio išsaugotą projekto kopiją (naktinę, „backup dabar“ ar
   prieš deploy'ų – kuri naujausia; su `full=1` – savaitinę pilną) ir iš jos pasiruošia veikiančią aplinką.
   Su `fresh=1` serveris kopiją padaro tuo metu. LIVE nepasiekiamas.

   Pirmiausia serveris turi priimti SSH iš VPN. Ubuntu Desktop `openssh-server` neturi, tada į 22 port'ą
   niekas neklauso (`ssh: connect to host … port 22: Connection refused`):

   ```bash
   bin/install-ssh --check --from 10.8.0.0/24   # paketas, sshd nustatymai, paslauga, ufw; nieko nekeičia
   sudo bin/install-ssh --from 10.8.0.0/24      # įdiegia; vėliau užtenka sudo bin/install-ssh (tinklai lieka)
   ```

   - **Nustatymai** – `/etc/ssh/sshd_config.d/10-setup-server.conf` (įrašomas prieš diegiant paketą, todėl naujas
     sshd be jo nepasileidžia): iš už `--from` tinklų ir ne iš šio serverio – tik raktas
     (`PasswordAuthentication no`, `KbdInteractiveAuthentication no`). VPN viduje galioja serverio paties nustatymai.
     sshd ima pirmą perskaitytą reikšmę, todėl failo vardas eina prieš kitus (`50-cloud-init.conf` ir pan.).
     `sshd -t` jo nepriėmus, ankstesnis failas grąžinamas. Port'ų, `ListenAddress` ir kitų failų scriptas nekeičia.
   - **Paslauga** – Ubuntu 22.10+ `ssh.socket` (sshd startuoja pirmu prisijungimu), senesniuose `ssh.service`;
     įjungiama, kad veiktų ir po perkrovimo. Pasikeitus nustatymams veikianti paslauga perkraunama `reload`
     (atidaryti seansai lieka).
   - **Tikrinama**, ar sshd klauso visais adresais ar VPN adresu; jei tik kitais (`ListenAddress`) – parodoma.
   - **ufw** – jei įjungtas, pridedama `ufw allow proto tcp from 10.8.0.0/24 to any port 22`; išjungtas paliekamas
     išjungtas (pakartotinai paleistas scriptas taisyklę pridės, kai ufw bus įjungtas).
   - Pabaigoje parodomas serverio ED25519 host rakto fingerprint'as: pirmą kartą jungiantis programuotojas jį
     palygina prieš atsakydamas `yes`.

   Tada – programuotojo raktas:

   ```bash
   sudo bin/add-backup-key ~/tomas.pub --name tomas --from 10.8.0.0/24 --check   # parodo, ką keistų
   sudo bin/add-backup-key ~/tomas.pub --name tomas --from 10.8.0.0/24           # įrašo raktą (tik atsisiuntimas)
   sudo bin/add-backup-key ~/tomas.pub --name tomas --from 10.8.0.0/24 --allow-fresh   # ir „fresh“ kopijos
   sudo bin/add-backup-key --list                                                # kam leista, iš kur, ar „fresh“
   sudo bin/add-backup-key --remove tomas                                        # atima leidimą
   ```

   Raktas (programuotojo `.pub` failas, niekada ne privatus) įrašomas į paskyros `setup`
   `.ssh/authorized_keys` su apribojimais: `from="10.8.0.0/24"` – jungtis galima tik iš nurodyto tinklo (VPN;
   `0.0.0.0/0` atmetamas), `command="/usr/local/sbin/setup-server-backup-pull --key tomas"` – raktas vykdo tik
   šią komandą, `restrict` – be terminalo, port'ų ir agento persiuntimo. Shell'o, Docker ir kitų failų raktas
   negauna. Tas pats vardas pakartotinai – raktas pakeičiamas; kiti `authorized_keys` įrašai nekeičiami.
   Prisijungiama tik raktu (paskyra `setup` slaptažodžio neturi).

   Komanda moka:

   | Prašymas | Atsakymas |
   | --- | --- |
   | `latest <projektas> <stage\|prod> [full]` | naujausios kopijos vardas: be `full` – registro kopija (naktinė, „fresh“, prieš deploy'ų), su `full` – pilna (savaitinė ar „fresh“ pilna) |
   | `info <projektas> <stage\|prod> [full]` | tas pats ir dydis baitais: `<failas> <dydis>` (atsisiuntimo progresui) |
   | `get <projektas> <stage\|prod> <failas>` | tos kopijos turinys |
   | `fresh <projektas> <stage\|prod> [full]` | tik raktui su `--allow-fresh`: paleidžia `setup-server-backup --fresh [--full]` paskyra `setup`, eigą siunčia į stderr (`progress backup BAITAI TIKĖTINA`), pabaigoje – `<failas> <dydis>` |

   „Fresh“ apkrauna projekto DB, todėl jo nedaro du kartus: jei tos pačios rūšies (registro ar pilna) kopija
   jaunesnė nei 30 min., atiduodama ji; jei kopija tuo metu daroma, laukiama jos pabaigos (užraktas). Reikia
   įdiegtos `/usr/local/sbin/setup-server-backup` (`sudo bin/install-backups`). Kiekvienas prašymas (ir atmestas)
   įrašomas į `/srv/setup-server/backup-pulls.log`: laikas, rakto vardas, adresas, prašymas, rezultatas. Projekto
   vardas – build katalogo vardas (`projects/<vardas>`, pvz. `asana_tracker`).

   Kopijose yra tikri klientų duomenys (stage/prod pagal lentelių registrą): leisk tik tiems, kam jų reikia,
   ir atimk leidimą (`--remove`), kai nebereikia. Įdiegtą komandos kopiją atnaujina `bin/update`.

8. **Prieiga prie taikinių per šį serverį (neprivaloma).** Šis serveris jau pasiekia kiekvieną projekto taikinį:
   deploy'us jungiasi `DEPLOY_SSH` savo raktu, o konteinerius valdo per Docker context'ą. Tuo pačiu keliu gali eiti
   ir programuotojas: jo raktas LIVE serveryje nereikalingas, o tiesioginės prieigos prie LIVE jam nereikia. Tas pats
   raktas, kaip kopijoms, gauna leidimus kiekvienam projektui ir aplinkai:

   ```bash
   sudo bin/add-backup-key ~/tomas.pub --name tomas --from 10.8.0.0/24 \
       --grant forsena:prod=phpfpm-readonly --grant forsena:stage=docker --check     # parodo, ką keistų
   sudo bin/add-backup-key ~/tomas.pub --name tomas --from 10.8.0.0/24 \
       --grant forsena:prod=phpfpm-readonly --grant forsena:stage=docker             # įrašo
   sudo bin/add-backup-key ~/tomas.pub --name tomas --from 10.8.0.0/24 --grant asana_tracker=full   # abi aplinkos
   sudo bin/add-backup-key --list          # tomas  from 10.8.0.0/24  access forsena:prod=phpfpm-readonly …
   ```

   `--grant` galima kartoti arba rašyti per kablelį; be `:stage`/`:prod` – abi aplinkos. Pakartotinai paleidus su tuo
   pačiu vardu įrašas pakeičiamas visas: išvardyk visus leidimus (be `--grant` – lieka tik kopijos). Projektas – build
   katalogo vardas (`projects/<vardas>`); dar nesančiam projektui leidimas įrašomas su pastaba.

   | Lygis | Ką leidžia | Ko neleidžia |
   | --- | --- | --- |
   | `phpfpm-readonly` | shell ir komandos aplinkos php-fpm konteineryje vartotoju `nobody` (65534) | rašyti į projekto vartotojo failus. **Tik failai:** konteinerio aplinkoje ir parduotuvės konfigūracijoje (`parameters.php`) lieka DB slaptažodis, todėl duomenų bazę keisti galima |
   | `phpfpm` | tas pats projekto vartotoju (`APP_UID`, failų savininkas) arba `root` konteineryje | kitų konteinerių, taikinio |
   | `docker` | projekto konteineriai: `ps`, `logs`, `exec` (ir `root`), `start`, `stop`, `restart`, `top`, `stats`, `inspect` | `docker run`, `exec --privileged`, image'ų, volume'ų, kitų projektų konteinerių ir `exec` į konteinerį su Docker socket'u (`metrics-docker`, `metrics-logs`: tai būtų root serveryje) |
   | `full` | taikinio shell: `DEPLOY_SSH` vartotojas taikinyje, kataloge `DEPLOY_PATH` | – (tiek, kiek gali tas vartotojas). **`--target local` projektui tai paskyra `setup` šiame serveryje:** visi projektai, jų paslaptys ir Docker, t. y. root |

   Kiekvienas lygis apima žemesnius. Raktas su leidimais įrašomas kaip
   `from="10.8.0.0/24",command="/usr/local/sbin/setup-server-access --key tomas --grant forsena:prod=phpfpm-readonly …",restrict,pty`:
   tik iš VPN, tik ši komanda, su terminalu (`pty`), bet be port'ų, agento ir X11 persiuntimo, todėl nei `ProxyJump`,
   nei tunelis į LIVE neveikia. Kopijų prašymus (`latest`, `info`, `get`, `fresh`) komanda perduoda
   `setup-server-backup-pull`, todėl `make db-pull` veikia kaip anksčiau.

   Programuotojo kompiuteryje – setup `make ssh` (tas pats serveris ir raktas, kaip `make db-pull`) arba tiesiogiai:

   | Prašymas (`ssh -t setup@10.8.0.14 …`) | Ką daro |
   | --- | --- |
   | `access` | kur raktas gali jungtis: `PROJEKTAS ENV LYGIS` eilutės |
   | `shell PROJEKTAS ENV [root\|readonly]` | shell php-fpm konteineryje (raktui tik su `phpfpm-readonly` – visada `readonly`) |
   | `exec PROJEKTAS ENV [root\|readonly] -- KOMANDA …` | viena komanda ten |
   | `host PROJEKTAS ENV [-- KOMANDA …]` | `full`: shell (ar komanda) taikinyje, `DEPLOY_PATH` kataloge |
   | `docker PROJEKTAS ENV ps [-a]` | projekto konteineriai |
   | `docker PROJEKTAS ENV logs [--tail N] [-f] [--since LAIKAS] [-t] KONTEINERIS` | žurnalas |
   | `docker PROJEKTAS ENV exec [--root\|-u VARTOTOJAS] [-w KATALOGAS] KONTEINERIS [KOMANDA …]` | shell ar komanda konteineryje |
   | `docker PROJEKTAS ENV start\|stop\|restart KONTEINERIS …`, `top`, `stats`, `inspect` | valdymas |

   Konteineris – vardas (`forsena-prod-php-fpm-1`) arba servisas (`php-fpm`, `database`). Taikinys, Docker context'as ir
   `APP_UID` imami iš build katalogo (`target*.env`, `env/<env>.env`), kaip deploy'aus. Terminalą komanda duoda, kai jį
   turi SSH seansas (`ssh -t`); be jo – stdin perduodamas, todėl veikia ir `… exec … -- mysqldump … > failas`.
   Kiekvienas prašymas (ir atmestas) ir jo pabaiga (išėjimo kodas) įrašomi į `/srv/setup-server/access.log`: laikas,
   rakto vardas, adresas, prašymas, lygis ir vykdoma komanda. Seanso turinys neįrašomas. Įdiegtą komandos kopiją
   atnaujina `bin/update`.

## Bitbucket SSH adresas (nuo 2026-11-12)

Nuo 2026-11-12 Bitbucket git per SSH atsako tik `ssh.bitbucket.org` (`git@ssh.bitbucket.org:darbo-erdve/repo.git`);
`bitbucket.org` SSH ryšių nebepriims. HTTPS (API, asana.local git mirror) nesikeičia. Naujas vardas turi tuos pačius
host raktus (ED25519 `SHA256:ybgmFkzwOSotHTHLJgHO0QN8L0xErw6vd0VhFA9m3SM`), bet `known_hosts` įrašas rišamas prie
vardo, o build katalogų `origin` lieka toks, su kokiu jie nuklonuoti. Todėl serveryje, iki tos datos:

```bash
cd ~/setup-server
sudo bin/prepare-host --check          # turi rodyti: would record the SSH host key of ssh.bitbucket.org
sudo bin/prepare-host                  # įrašo jį (tik su paskelbtu fingerprint'u)
sudo bin/move-bitbucket-ssh --check    # kurie remote'ai persikels ir ar naujas adresas atsako
sudo bin/move-bitbucket-ssh            # perkelia app/ ir app/public/ remote'us; GitHub nelieča
sudo bin/install-ui                    # sudo taisyklė web sąsajai (veiksmas „Bitbucket SSH“)
```

- `move-bitbucket-ssh` kiekvieną naują URL pirmiau perskaito `git ls-remote` kaip `setup` paskyra (jos raktu ir
  `known_hosts`); neatsakiusio remote'o nekeičia ir baigia kodu 1. Kartoti galima.
- `bin/add-project` seną `git@bitbucket.org:…` URL nuo šiol klonuoja per `ssh.bitbucket.org`, o pakartotinai
  paleistas esamo build katalogo remote'ą perkelia pats.
- Web sąsaja atpažįsta abu vardus (pipeline'ai, deploy'ai).
- **Programuotojo kompiuteryje** ta pati komanda be sudo perkelia savo klonų remote'us (visus, ne tik `origin`):
  `bin/move-bitbucket-ssh --check --under ~/Projects`, tada be `--check`. Pirmiau į `~/.ssh/known_hosts` įrašyk
  `ssh.bitbucket.org` raktą, palyginęs jo fingerprint'ą su paskelbtu (`ssh-keyscan -t ed25519 ssh.bitbucket.org | ssh-keygen -lf -`).

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
  `sudo` perrašomos įdiegtos kopijos `/usr/local/sbin/setup-server-backup`, `/usr/local/sbin/setup-server-metrics`,
  `/usr/local/sbin/setup-server-backup-pull`, `/usr/local/sbin/setup-server-access` ir valdymo sąsajos `/usr/local/sbin/setup-server-ui` kartu su
  `/usr/local/lib/setup-server/bin/` (tik tos, kurios įdiegtos) iš naujos versijos: `bin/install-backups --refresh`,
  `bin/install-metrics --refresh`, `bin/add-backup-key --refresh` ir `bin/install-ui --refresh`.
  Pasikeitus metrikų ar sąsajos kopijai, perkraunama paslauga `setup-server-metrics` ar `setup-server-ui` (runner'io – ne).
  Kiti failai (unit'ai, sudo taisyklės, nustatymai) nekeičiami; jei naujoje versijoje jie kitokie, išvedama pastaba
  `note: … differs` – tada pakartok `sudo bin/install-backups` (laikas lieka), `sudo bin/install-metrics` arba
  `sudo bin/install-ui`. Nepavykus perrašymui ar sveikatos patikrai, kartu su checkout'ais grąžinamos ir ankstesnės kopijos.
- **Iš valdymo sąsajos** (*Veiksmai → Atnaujinti*) `bin/update` vykdomas kaip klono savininkas, bet be terminalo:
  sudo slaptažodžio paklausti negali, todėl jo kvietimai tada yra `sudo -n`. Jei savininkui sudo be slaptažodžio
  neleidžiamas, `bin/update` iškart atsisako (kodas 77, niekas nepakeista) – atnaujink terminale. Klonas taip pat turi
  parsisiųsti (`git fetch`) be slaptažodžio frazės. Atnaujinimo metu sąsaja persikrauna į naują kopiją, o veiksmas tęsiasi.
- **Ref'as** – tag'as (pageidautina), `origin` šaka (`master` arba `origin/master`) arba commit'o id. setup-server gali būti ir ne ant tag'o
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

### Tik setup (be slaptažodžio)

Bendrą setup checkout'ą (`/srv/setup-server/setup`) atnaujina viena komanda, be administratoriaus slaptažodžio ir be
setup-server atnaujinimo – arba sąsajoje *Veiksmai → Atnaujinti setup*:

```bash
sudo bin/update-setup --check          # dabartinė versija, naujausias to paties MAJOR release'as, commit'ai
sudo bin/update-setup                  # perjungia į jį
sudo bin/update-setup v2.6.0           # arba į nurodytą release tag'ą
```

- Tik release tag'ai (`vX.Y.Z`), tik švarus checkout'as, ne pipeline žingsnio metu (`make` paskyros `setup`).
  Nepavykus perjungti checkout'as grįžta. Git darbą atlieka paskyra `setup`.
- Naujas MAJOR – tik nurodžius tag'ą; peržiūra perspėja, kad projektams gali reikėti pakeitimų.
- **Deploy'us tai daro ir pats:** kai deploy'inamo commit'o `SETUP_MIN_VERSION` naujesnis už serverio setup, setup 2.7.0+
  `pipeline-deploy` perjungia setup į naujausią to paties MAJOR release'ą ir žingsnį paleidžia iš naujo.
- Pilnas atnaujinimas (setup-server ir setup kartu, su sveikatos patikra) – `bin/update`, žemiau.

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

   Jei scriptas išvedė ir `sudo install … /usr/local/sbin/setup-server-…` (ar `… /usr/local/lib/setup-server/bin/`)
   eilutę, įvykdyk ją (ankstesnė kopija laikoma `/tmp/tmp.…` kataloge). Be jos įdiegtas kopijas iš grąžinto checkout'o
   perrašo `sudo bin/install-backups --refresh`, `sudo bin/install-metrics --refresh`, `sudo bin/add-backup-key --refresh`
   ir `sudo bin/install-ui --refresh`.

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
```

- `--address` – kur klausoma: VPN serveryje jo VPN adresas (`10.8.0.14`); numatytas `127.0.0.1` (tik iš paties
  serverio). `--allow` – Prometheus adresas(-ai), IP/CIDR, atskirti tarpais ar kableliais; privalomas su bet
  kuriuo adresu, išskyrus `127.0.0.1`/`::1`. `--port` – numatytas `9101`.
- **Token'as neprivalomas.** Prieigą saugo IP sąrašas; be token'o atsako kiekvienam sąrašo adresui.
  `--new-token` – sugeneruoja 43 simbolių token'ą (arba pakeičia esamą; Prometheus'ui:
  `sudo sed -n 's/^METRICS_TOKEN=//p' /etc/setup-server/metrics.env`), `--no-token` – pašalina esamą.
  Token'as naudingas, kai sąraše daugiau adresų nei Prometheus (pvz. visas VPN potinklis).
- Nustatymai laikomi `/etc/setup-server/metrics.env` (root, 0600). Pakartotinai paleidus nenurodyti nustatymai
  (ir token'as) lieka, paslauga perkraunama tik pasikeitus failui; runner'io paslauga neliečiama. Įdiegtą komandos
  kopiją atnaujina `bin/update` (žr. „Atnaujinimas“).
- Prieiga kaip taikinio `/metrics`: ne iš sąrašo – `403`, su token'u – be jo ar su neteisingu `401`, kitas kelias –
  `404`. Be sąrašo ar su trumpesniu nei 24 simbolių token'u paslauga nepasileidžia (`journalctl -u setup-server-metrics`).
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

Kopijos – tik `worker-<env>-…Z.sql.gz` (naktinės ir „backup dabar“). Deploy'aus ir „fresh“ kopijos
neskaičiuojamos, kad neuždengtų sustojusio laikmačio. Savaitinė pilna serija (`full-<env>-…Z.sql.gz`) turi savo
metrikas `setup_worker_full_backups`, `setup_worker_full_backup_newest_age_seconds` ir
`setup_worker_full_backup_newest_size_bytes`; `0` aplinkai be pilnos kopijos rodomas tik įdiegus
`setup-server-backup-full.timer`, kad serveris be savaitinių kopijų nekeltų įspėjimo. Deploy'ai – iš setup istorijos
`app/.generated/release/<env>/deployments.json` (paskutinis įrašas). Patikrinti be Prometheus:

```bash
systemctl status setup-server-metrics
sudo -u setup /usr/local/sbin/setup-server-metrics --once          # metrikos be serverio ir token'o
curl http://10.8.0.14:9101/metrics                                  # iš sąrašo adreso, be token'o
curl -H "Authorization: Bearer $(sudo sed -n 's/^METRICS_TOKEN=//p' /etc/setup-server/metrics.env)" http://10.8.0.14:9101/metrics   # su token'u
```

**Prometheus** (serveris prijungtas prie VPN; `authorization` – tik su token'u):

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

Savaitinei pilnai kopijai – „senesnė nei 8 dienos“:

```promql
max by (project, environment) (setup_worker_full_backup_newest_age_seconds) > 8 * 24 * 3600
  or setup_worker_full_backups == 0
```

Papildomai verta: `up{job="setup-worker"} == 0` arba „no data“ (VPN ar paslauga neveikia),
`setup_metrics_group_up == 0`, `setup_worker_runner_up == 0`, `setup_worker_github_runner_up == 0`,
`setup_worker_deploy_last_success == 0`.

## Valdymo sąsaja (web)

Naršyklėje – serverio būsena ir tie patys `bin/` veiksmai. Sąsaja klauso tik `127.0.0.1` (numatytas port'as `9102`),
todėl iš kompiuterio ji pasiekiama tik per SSH tunelį.

```bash
cd ~/setup-server
sudo bin/install-ui --check        # paskyra, unit'as, sudo taisyklė, nustatymai (paslaptys paslėptos); nieko nekeičia
sudo bin/install-ui                # įdiegia, paleidžia ir vieną kartą parodo slaptažodį
```

- `--port` – kitas port'as (1024–65535). Kito adreso nėra: `--address` priima tik `127.0.0.1`, pati paslauga kitu
  adresu nepasileidžia.
- `--user` – prisijungimo vardas (numatytas `admin`), `--new-password` – naujas slaptažodis (senasis nebetinka).
- Pakartotinai paleistas palieka vardą, port'ą, slaptažodį ir token'us; paslaugą perkrauna tik pasikeitus failui.
  Įdiegtas kopijas atnaujina `bin/update` (žr. „Atnaujinimas“).

**Prisijungimas.** Kompiuteryje atidaryk tunelį ir naršyklėje – `http://localhost:9102/`:

```bash
ssh -L 9102:127.0.0.1:9102 <serveris>    # kol tunelis atidarytas, sąsaja pasiekiama
```

Tunelis eina per serverio SSH; jei jis neatsako, žr. `bin/install-ssh` („Eiga“, 7 žingsnis).

Naršyklė paklaus vardo ir slaptažodžio (Basic auth; srautą šifruoja SSH). Slaptažodis rodomas tik įdiegus, serveryje
laikomas tik jo PBKDF2 hash'as `/etc/setup-server/ui.env` (root, 0600). Pamiršus – `sudo bin/install-ui --new-password`.

**Ką rodo.**

- *Apžvalga* – setup-server versija (įdiegta ir administratoriaus klono), bendro setup checkout'o versija, Bitbucket ir
  GitHub runner'ių paslaugos, naktinių ir savaitinių pilnų kopijų laikmačiai (paskutinis paleidimas, rezultatas, kitas), metrikų paslauga,
  pati sąsaja, laisva vieta diske, kuriame `/srv/setup-server`. Prie kiekvienos įdiegtos paslaugos – jos mygtukai
  (paleisti, sustabdyti, perkrauti; kopijoms – „Paleisti dabar“), kurie veda į tą pačią peržiūrą kaip kiti veiksmai.
- *Projektai* – kiekvienas build katalogas: repo URL, aplinkos (stage/prod), taikinys (`target*.env`, ne paslaptis),
  paskutinis deploy'us (laikas, commit'as, pavyko ar ne, rollback), naujausia naktinė ir savaitinė pilna kopija (amžius,
  dydis, kiek laikoma; projekto puslapyje – ir paskutinė „fresh“),
  ar veikia `<PROJECT_NAME>-<env>-secrets`, konteineriai (būsena, sveikata) ir paskutiniai Bitbucket pipeline'ai.
  Projekto puslapyje – ir visos aplinkos kopijos (naktinės, pilnos, šviežios, prieš deploy'ų: failas, laikas, dydis),
  kiekvieno konteinerio `docker logs` (200, 500 ar 2000 paskutinių eilučių) ir „Kopija dabar serveryje“.
- *Metrikos* – worker'io `/metrics`, kaip jį mato Prometheus (`setup-server-metrics --once`, be token'o), ir kurios
  grupės surinktos.
- *Žurnalai* – `ui-actions.log`, `backup-pulls.log`, `access.log`, setup-server paslaugų journal (po 50 eilučių) ir paskutiniai
  veiksmai su jų išvestimi.
- *Instrukcijos* – kaip paruošti kitą serverį projekto taikiniu (`bin/add-project --target ENV=ssh://…`): pasirenki OS
  (Ubuntu, Debian; CentOS/AlmaLinux/Rocky – netestuota), valdymo skydelį (DirectAdmin ar jokio), aplinką (prod, stage) ir
  SSL (Let's Encrypt, savas, be SSL – tik VPN), įrašai serverį, vartotoją, katalogą, domeną, port'ą ir gauni
  sunumeruotus žingsnius su komandomis: paketai, Docker, vartotojas ir `docker` grupė, katalogas, worker'io raktas
  (tikras, iš `/srv/setup-server/.ssh/id_ed25519.pub`), ugniasienė, portai, DirectAdmin domenas, sertifikatas ir proxy,
  `env/<env>.env.example`, slaptažodžiai, registracija, host key palyginimas ir pirmas deploy'us. Kiekvienas žingsnis
  nurodo šaltinį; kur setup dar nieko neaprašo (DirectAdmin proxy, sertifikatas be skydelio, ne Debian OS), tai
  pažymėta įspėjimu. Puslapis nieko nevykdo; mygtukas atidaro „Pridėti projektą“ su įrašytu taikiniu.

**Veiksmai.** Kiekvienas pirmiausia paleidžiamas su `--check`: puslapis parodo komandą ir jos išvestį. Tik tada
„Vykdyti“ paleidžia **tą pačią** komandą be `--check` (peržiūra galioja 15 min. ir vieną kartą; jei `--check` baigėsi
klaida, „Vykdyti“ nėra). Vienu metu vykdomas vienas veiksmas; jo puslapis atsinaujina, kol jis baigsis.

| Veiksmas | Ką vykdo (`L` = `/usr/local/lib/setup-server/bin`) |
| --- | --- |
| Pridėti projektą | `sudo -n L/add-project [--shared-key] --target … [VARDAS] URL [KODO_URL]` (naujam projektui taikinys privalomas) |
| Pašalinti projektą (projekto puslapyje) | `sudo -n L/remove-project [--with-backups] VARDAS` |
| Įdiegti runner'į | `sudo -n L/install-runner --provider bitbucket\|github [--name …] [--tarball URL\|FAILAS [--sha256 …]]`; dialogo tekstas – per stdin |
| DB kopijų laikmačiai (naktinės ir savaitinės pilnos) | `sudo -n L/install-backups [--at HH:MM] [--full-at "DAY HH:MM"]` |
| Kopija dabar serveryje (ir projekto puslapyje) | `sudo -n L/backup-projects [--full] [PROJEKTAS [ENV]]` – be Bitbucket, veikia ir GitHub projektams |
| SSH iš VPN | `sudo -n L/install-ssh [--from TINKLAI]` |
| Paslaugos (ir Apžvalgoje) | `sudo -n L/server-unit start\|stop\|restart UNIT` (žr. žemiau) |
| Worker'io metrikos | `sudo -n L/install-metrics [--address …] [--allow …] [--port …] [--new-token\|--no-token]` |
| Kopijų ir prieigos raktai | `sudo -n L/add-backup-key - --name … --from … [--allow-fresh] [--grant PROJEKTAS[:ENV]=LYGIS …]` (raktas per stdin), `--remove VARDAS`, `--list` |
| Paruošti serverį | `sudo -n L/prepare-host [--setup-ref …]` (`--check` kodas 1 – „Needs attention“ – vykdyti leidžia) |
| Atnaujinti setup | `sudo -n L/update-setup [vX.Y.Z]` (tik release tag'ai, be slaptažodžio) |
| Atnaujinti | `sudo -n -H -u <klono savininkas> <klonas>/bin/update [--server-ref …] [--setup-ref …]` |
| Deploy'us, „backup dabar“, runner'io testas | Bitbucket API (žr. žemiau) |

- Vardai, URL, taikiniai, ref'ai, laikas ir adresai tikrinami griežtais šablonais, komanda paduodama argumentų sąrašu
  (ne shell eilute): `;`, `$(…)` ar `--parinktis` vietoje reikšmės atmetami dar prieš `--check`.
- **Runner'io dialogas.** Bitbucket „Runner installation“ dialogo ar GitHub „New self-hosted runner“ puslapio tekstas
  įklijuojamas į laukelį ir komandai perduodamas per stdin – kaip `sudo bin/install-runner < dialogas.txt`. Jis niekur
  neįrašomas; `--OAuthClientSecret` ir `--token` reikšmės išvestyje keičiamos į `<hidden>`. Tarp peržiūros ir „Vykdyti“
  tekstas laikomas tik sąsajos atmintyje (persikrovus – įklijuok iš naujo).
- **Atnaujinimas** vykdomas kaip klono, iš kurio paleistas `install-ui`, savininkas; jam sudo turi veikti be
  slaptažodžio (žr. „Atnaujinimas“), kitaip – terminale.
- **Paslaugos.** `bin/server-unit` valdo tik setup-server unit'us ir tik leidžiamais veiksmais; ką ir su kuo daryti,
  nustato jo paties sąrašas, ne kvietėjas. `--check` parodo unit'o būseną ir ar vyksta pipeline'o žingsnis (sustabdžius
  ar perkrovus runner'į jis nutrūktų).

  | UNIT | Veiksmai | systemd unit'as |
  | --- | --- | --- |
  | `runner` | start, stop, restart | `setup-server-runner.service` |
  | `github-runner@SCOPE` | start, stop, restart | `setup-server-github-runner@SCOPE.service` (tik užregistruotas SCOPE) |
  | `metrics` | start, stop, restart | `setup-server-metrics.service` |
  | `backup`, `backup-full` | start (laukia, kol kopija baigsis) | `setup-server-backup.service`, `setup-server-backup-full.service` |
  | `ui` | restart (perskaito `ui.env`, pvz. naujus token'us) | `setup-server-ui.service` |

  Ranka: `sudo bin/server-unit --check restart runner`.
- **Ko sąsaja nedaro:** `bin/install-ui` (port'as, vardas, slaptažodis) ir rankinis atstatymas po nepavykusio
  `bin/update` (žr. „Rankinis atstatymas“) lieka terminale: jie keičia pačią sąsają arba reikalingi, kai ji neveikia.

**Deploy'ai per Bitbucket.** Projekto puslapyje – „Deploy į stage“ ir „Deploy į prod“ (custom pipeline'ai `deploy-stage`,
`deploy-production` su kintamaisiais `REF`, `IMAGES`, `BACKUP`, `ROLLBACK`), „Backup dabar“ (`backup-now`, pažymėjus
FULL – su kintamuoju `FULL=yes`; nepažymėjus kintamasis nesiunčiamas) ir „Runner'io
testas“ (`runner-test`). Sąsaja juos paleidžia per Bitbucket API, todėl istorija lieka Bitbucket'e (Pipelines,
Deployments), o darbą atlieka runner'is šiame serveryje. Repo – build katalogo `app/` `origin` URL, šaka – jo
`origin/HEAD` (formoje galima pakeisti). Peržiūra parodo užklausą (JSON), „Vykdyti“ ją išsiunčia ir parodo nuorodą į
pipeline'ą. GitHub projektams pipeline'ų paleidimas dar nepalaikomas.

```bash
sudo nano /etc/setup-server/ui.env        # įrašyk token'ą (žr. žemiau)
sudo systemctl restart setup-server-ui    # paslauga token'us skaito pasileisdama (arba Apžvalgoje: Valdymo sąsaja → Perkrauti)
```

```ini
BITBUCKET_ACCESS_TOKEN_ASANA_TRACKER=…    # vienam build katalogui: vardas didžiosiomis, - keičiamas į _
BITBUCKET_ACCESS_TOKEN=…                  # visiems kitiems (pvz. workspace access token)
```

Token'as – Bitbucket *Repository* arba *Workspace access token* (Bearer) su teisėmis **Repositories: Read** ir
**Pipelines: Read, Write**. Jis laikomas tik `ui.env` (root, 0600), paslauga jį gauna per aplinką; komandoms jis
neperduodamas, puslapiuose nerodomas, į žurnalus nerašomas (žurnale – tik kintamojo vardas). Siunčiamas tik į
`api.bitbucket.org`, nukreipimai (redirect) nesekami.

**Teisės ir kodėl taip.**

- Sąsaja veikia **atskira paskyra `setup-ui`** (be shell'o ir namų katalogo), ne `setup`: pipeline žingsniai vykdomi kaip
  `setup`, o jiems sąsajos teisių duoti negalima. `setup-ui` yra grupėje `setup` (pasiekia `/srv/setup-server`, 0750, ir
  savo žurnalą), bet **ne** `docker` grupėje (jei yra, `install-ui` atsisako).
- Tai, kam reikia root, sąsaja daro tik per `sudo -n` pagal `/etc/sudoers.d/setup-server-ui` (tikrinama `visudo -cf`):
  root teisėmis – tik root'ui priklausančios kopijos `/usr/local/lib/setup-server/bin/` (`add-project`, `remove-project`,
  `install-backups`, `install-metrics`, `install-runner`, `install-ssh`, `add-backup-key`, `backup-projects`, `prepare-host`,
  `server-status`, `server-unit`, `update-setup`), niekada
  administratoriaus klonas, kurį gali keisti jo savininkas. `bin/update` – tik klono savininko teisėmis (root'u jis nevyksta).
- Argumentų sudo neriboja: ribą nustato sąsajos patikra ir pačios komandos. Todėl `setup-ui` per šias komandas
  (pvz. `install-runner`, `prepare-host`) turi administratoriaus galią; ją saugo `127.0.0.1`, slaptažodis ir CSRF token'as.
- Būseną skaito viena tik skaitanti komanda `server-status` (JSON): `systemctl show`, `journalctl`, versijas ir diską –
  root teisėmis, o build katalogus, Docker konteinerius ir secret store – paskyros `setup` teisėmis. Pipeline'o pakištas
  simlink'as todėl nieko daugiau neatskleidžia, o iš env failų išeina tik taikinio reikšmės. Konteinerio log'ai –
  tik to projekto aplinkos konteinerio (to paties sąrašo, kurį rodo apžvalga), jo Docker context'e. Ranka:
  `sudo /usr/local/lib/setup-server/bin/server-status | less` (`--logs 100` – žurnalai,
  `--container-logs PROJEKTAS ENV KONTEINERIS [EILUTĖS]`, `--metrics`).
- **systemd.** `NoNewPrivileges` išjungtas: su juo neveiktų nė vienas `sudo`. `ProtectSystem`, `ProtectHome`, `PrivateTmp`
  taip pat išjungti: veiksmai vyksta paslaugos vardų erdvėje ir rašo `/usr`, `/etc` (diegimai), administratoriaus kloną
  (`bin/update`) ir `/tmp` (ankstesnės kopijos rankiniam grąžinimui). Palikta, kas netrukdo: `ProtectKernelModules`,
  `ProtectControlGroups`, `LockPersonality`, `RestrictRealtime`. `KillMode=process`: perkraunama tik sąsaja, vykdomas
  veiksmas tęsiasi.
- **CSRF.** Kiekviena forma turi token'ą – vartotojo vardo HMAC su raktu, kurį paslauga sukuria pasileisdama
  (persikrovus atidarytos formos nebetinka: atnaujink puslapį). POST be jo ar su svetimu `Origin` – `403`. `Host` turi būti
  `127.0.0.1` ar `localhost` (apsauga nuo DNS rebinding), kitaip – `400`. Vardas, slaptažodis ir token'ai lyginami
  pastovaus laiko palyginimu.

**Žurnalas.** Kiekvienas veiksmas įrašomas į `/srv/setup-server/ui-actions.log` (tik pridedama): laikas, vartotojas,
klientas, komanda (argv) ir išėjimo kodas; pipeline'ams – repo, pipeline'as, kintamieji, HTTP atsakymas ir token'o
kintamojo vardas. Stdin (runner'io dialogas, raktas) ir token'ai neįrašomi. Neįrašius eilutės, veiksmas nevykdomas.
Kiekvieną komandą registruoja ir pats sudo. Veiksmų išvestis – `/var/lib/setup-server-ui/jobs/` (`setup-ui`, 0700;
laikoma 100 paskutinių).

```bash
systemctl status setup-server-ui
journalctl -u setup-server-ui -n 50                 # atmesti prisijungimai ir CSRF, klaidos
sudo tail -n 20 /srv/setup-server/ui-actions.log
```

## Projektų konteineriai serveryje

- **Kuris Docker.** Projektus paleidžia paskyra `setup` per `DEPLOY_CONTEXT=default`, t. y. serverio sistemos
  Docker (`/var/run/docker.sock`). Ubuntu Desktop'e administratoriaus `docker` dažnai rodo į Docker Desktop
  (`docker context ls` žvaigždute pažymėtas `desktop-linux`) – tai kita VM su kitais konteineriais, todėl
  projektų ten nematyti. Projektų konteineriai, log'ai ir `exec`:
  ```sh
  sudo docker --context default ps -a --filter name=<projektas>-prod
  sudo docker --context default logs --tail 50 <konteineris>
  ```
  zsh nekaido kintamojo į žodžius: `D="sudo docker --context default"; $D ps` neveikia, rašyk komandą visą.
- **Prievadai 80/443.** Jei taikinyje juos laiko kompiuterio web serveris (pvz. ankstesnio stack'o nginx),
  projekto nginx-proxy jų negauna. Naujesnis nei 2.7.1 setup tai patikrina prieš paleisdamas konteinerius ir sustoja;
  atlaisvink prievadą (`sudo ss -ltnp 'sport = :80'`, tada pvz. `sudo systemctl disable --now nginx`) ir deploy'ink
  iš naujo.
- **Duomenų katalogų keitimas.** Prieš keisdamas ar pervadindamas projekto duomenų katalogą
  (`data/<env>/mysql`, `…/meilisearch` ir pan.), sustabdyk tą konteinerį: veikiantis konteineris lieka prijungtas
  prie pervadinto katalogo (ištrynus jį – `No such file or directory`), o naują katalogą pamato tik po
  `docker restart`.

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
- Slaptažodžiai į šį repo, į projekto katalogus ir į serverio failus nerašomi: jie tik Bitbucket'e ir veikiančių
  konteinerių aplinkoje (Docker konfigūracija, kaip ir DB slaptažodžiai DB konteineryje); naktinės kopijos juos ima
  iš projekto `secrets` konteinerio. Sudo taisyklė `setup` paskyrai naujų teisių neprideda: ji ir taip Docker grupėje.
- SSH (`bin/install-ssh`) iš už VPN tinklo ir ne iš šio serverio priima tik raktus; slaptažodžiu jungtis galima tik
  iš VPN (jei serverio nustatymai tai leidžia). Port'as iš interneto neatidaromas: tai lemia maršrutizatorius ar ufw.
- Kopijų atsisiuntimo raktai (`bin/add-backup-key`) prisijungia prie paskyros `setup`, bet tik iš nurodyto tinklo
  ir tik su priverstine komanda, kuri atiduoda vien kopijas (`restrict`: be terminalo ir persiuntimų). Kopijose
  yra tikri klientų duomenys; kiekvienas atsisiuntimas registruojamas `backup-pulls.log`.
- Raktai su `--grant` per šį serverį pasiekia projektų taikinius tuo pačiu keliu, kaip deploy'us (žr. „Eiga“, 8
  žingsnį): leidimai įrašyti rakto eilutėje, juos tikrina priverstinė komanda, persiuntimų nėra (`restrict,pty`),
  kiekvienas prašymas – `access.log`. `full` local taikiniui ir `docker` (`exec` kaip root į projekto konteinerius su
  jų prijungtais katalogais) – didelės teisės; `phpfpm-readonly` saugo tik failus, ne duomenų bazę. `authorized_keys`
  priklauso paskyrai `setup`, todėl pipeline žingsnis galėtų jį keisti – bet jis ir taip yra `setup` su Docker.
- Worker'io `/metrics` token'as (jei naudojamas) yra tik `/etc/setup-server/metrics.env` (root, 0600); paslauga
  jį gauna per aplinką. Endpoint'as klauso tik nurodytu adresu (VPN), atsako tik leidžiamiems adresams (ir, kai
  token'as nustatytas, tik su juo) ir nieko nekeičia.
- Valdymo sąsaja klauso tik `127.0.0.1` (iš kitur – per SSH tunelį), reikalauja slaptažodžio ir kiekvienai formai –
  CSRF token'o. Ji veikia atskira paskyra `setup-ui` (ne `setup`, ne `docker` grupėje) ir root teisėmis vykdo tik
  root'ui priklausančias komandų kopijas `/usr/local/lib/setup-server/bin/` pagal savo sudo taisyklę. Per jas ji turi
  administratoriaus galią, todėl jos slaptažodį saugok kaip administratoriaus. Bitbucket token'ai – tik
  `/etc/setup-server/ui.env` (root, 0600); kiekvienas veiksmas – `ui-actions.log`.
- `bin/update` iš sąsajos veikia tik tada, kai klono savininkas gali naudoti sudo be slaptažodžio. Tokios teisės
  setup-server nesuteikia: jos suteikimas – administratoriaus sprendimas (savininko paskyra tada prilygsta root be
  slaptažodžio).

## Dar neįtraukta

Šios dalys dar daromos (žemėlapis „Map — setup-server“):

- Prometheus prijungimas per VPN ir Grafana įspėjimai (kopija senesnė nei 26 val., „no data“);
- GitHub Actions workflow'ų paleidimas iš valdymo sąsajos (Bitbucket pipeline'ai jau paleidžiami);

## Testai

```bash
python3 -m unittest discover -s tests                                    # be root ir be tinklo
SETUP_SERVER_UBUNTU_IMAGE=ubuntu:24.04 python3 -m unittest discover -s tests   # + tikras Ubuntu konteineris
SETUP_PROMTOOL_IMAGE=prom/prometheus:latest python3 -m unittest discover -s tests  # + metrikų formato patikra (be promtool)
```
