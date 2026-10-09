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
| `bin/add-project` | Projekto build katalogas `/srv/setup-server/projects/<vardas>`: aplinkos repo `app/`, atskiras kodo repo `app/public/`. |

Visi scriptai yra idempotentiški, juos galima kartoti. Su `--check` jie tik parodo, ką keistų,
ir nieko nekeičia. Pirmiausia paleisk būtent su `--check`.

```text
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
   git clone git@bitbucket.org:<workspace>/setup-server.git ~/setup-server
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
   Shell*, pridėk žymą `setup.build`. Dialogas parodo archyvo nuorodą ir „Run“ komandą. Įklijuok
   komandą į scriptą:

   ```bash
   sudo bin/install-runner --tarball '<archyvo nuoroda iš dialogo>' --check   # įklijuok Run komandą, Ctrl-D
   sudo bin/install-runner --tarball '<archyvo nuoroda iš dialogo>'           # tas pats, įdiegia
   ```

   OAuth duomenys įrašomi tik į `/etc/setup-server/runner.env` (root, 0600). Pats runner'is juos
   gauna kaip `start.sh` argumentus (taip jį sukūrė Atlassian), todėl šiame serveryje jie matomi `ps`.
   Po minutės Bitbucket'e runner'is turi būti **ONLINE**.

4. **Projektas:**

   ```bash
   sudo bin/add-project asana git@bitbucket.org:rsmaizys/asana_tracker.git
   sudo bin/add-project forsena git@bitbucket.org:rsmaizys/forsena_docker.git git@bitbucket.org:rsmaizys/forsena.git
   ```

   Bitbucket'e nustatyk repository variable `BUILD_ROOT`, kurį parodo scriptas. Slaptažodžius
   įrašyk į Deployment variables (Secured). Kiekvienas pipeline žingsnis turi turėti
   `runs-on: [self.hosted, linux.shell, setup.build]`, kitaip jis vyktų Bitbucket debesyje ir
   naudotų minutes.

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
- asana.local perjungimas į setup v2 tame pačiame serveryje;
- setup-server atnaujinimas per Pipelines.

## Testai

```bash
python3 -m unittest discover -s tests                                    # be root ir be tinklo
SETUP_SERVER_UBUNTU_IMAGE=ubuntu:24.04 python3 -m unittest discover -s tests   # + tikras Ubuntu konteineris
```
