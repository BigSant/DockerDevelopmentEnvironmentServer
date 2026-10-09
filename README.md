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

Visi scriptai yra idempotentiški, juos galima kartoti. Su `--check` jie tik parodo, ką keistų,
ir nieko nekeičia. Pirmiausia paleisk būtent su `--check`.

```text
~/setup-server/                 šis repo, administratoriaus klonas (iš jo leidžiami scriptai)
/srv/setup-server/              paskyros setup namai
├── setup/                      bendras setup checkout'as (serveriuose – ant release tag'o)
├── projects/<vardas>/app/      projekto build katalogas (BUILD_ROOT = projects/<vardas>)
├── runner/current/             runner'io versija; runner/temp – jo darbo katalogas
└── .ssh/id_ed25519             git raktas (tik skaitymui)
/etc/setup-server/runner.env    runner'io OAuth duomenys (root, 0600)
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

## Saugumas

- Paskyra `setup` yra Docker grupėje, o tai šiame serveryje prilygsta root teisėms. Tai priimta,
  nes serveris yra ir build serveris, ir taikinys. Nutolusiems taikiniams šis klausimas sprendžiamas
  atskirai.
- Esamas Docker niekada nepakeičiamas ir neperkraunamas, todėl jau veikiantys konteineriai
  (dabartinė asana.local) lieka veikti.
- Slaptažodžiai į šį repo ir į projekto katalogus nerašomi.

## Dar neįtraukta

Šios dalys dar sprendžiamos žemėlapyje „Map — setup-server“:

- išorinio Prometheus prieiga prie serverio `/metrics` ir TLS;
- atsarginės kopijos;
- asana.local perjungimas į setup v2 tame pačiame serveryje.

## Testai

```bash
python3 -m unittest discover -s tests                                    # be root ir be tinklo
SETUP_SERVER_UBUNTU_IMAGE=ubuntu:24.04 python3 -m unittest discover -s tests   # + tikras Ubuntu konteineris
```
