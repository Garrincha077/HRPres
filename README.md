# HRPres — hrvatske sudske odluke

Neovisan projekt za preuzimanje, indeksiranje i pretraživanje javnih
anonimiziranih odluka s https://odluke.sudovi.hr/. Korpus i indeksi su
prenosivi: mogu ih koristiti LexHR, druga pravna AI aplikacija i lokalni RAG,
bez slanja privatnih spisa u oblak.

**Izvor:** Tražilica odluka sudova Republike Hrvatske —
https://odluke.sudovi.hr/. Neslužbeni, neovisni projekt.

## Što je spremno

- Ograničeni pilot: novije odluke prve, deduplikacija stabilnog UUID-a,
  SQLite checkpoint za nastavak, izdvajanje teksta i metapodataka.
- Komprimirani JSONL tekstovi, punotekstni SQLite FTS5 i SHA-256 manifest.
- **Vektori na GitHubu**: ručni Actions proces na besplatnom CPU runneru,
  s višejezičnim MiniLM modelom (384 dimenzije), izborno BGE-M3
  (1024 dimenzije, znatno sporiji). Bez lokalne Ollame.
- FAISS točni indeks i float16 arhiv vektora po verziji, s modelom u manifestu.
- Povezivanje s Google Driveom putem rclone konfiguracije u GitHub Secrets.

## Lokalno preuzimanje na Windowsu (0.2)

Za glavni korpus preporučen je **jedan lokalni collector**. Ne pokretati
istodobno lokalni collector i GitHub collector. Lokalni postupak nema
Ollamu, ne preuzima modele i ne šalje datoteke na Drive.

Iz provjerenog checkouta, uz instaliran Python 3.12:

    powershell -File windows/install-local.ps1 -Destination D:\HRPres

Skripta kopira aplikaciju u `app/`, priprema
izolirano okruženje i dodaje pokretače. Ne briše postojeće podatke.
Otvori `D:\HRPres\POKRENI-HRPRES.cmd` za izbornik:

- još 25 odluka za probu ili nastavak s najviše 1.000 novih odluka;
- pregled stanja i lokalna tekstualna pretraga;
- zaseban `PAUZIRAJ-HRPRES.cmd` za pauzu tijekom aktivnog preuzimanja.

Iz naredbenog retka:

    python -m hrpres download --db D:\HRPres\data\state.sqlite3 --max-new 25
    python -m hrpres status --db D:\HRPres\data\state.sqlite3
    python -m hrpres search --db D:\HRPres\data\state.sqlite3 --query "zastara"
    python -m hrpres pause --db D:\HRPres\data\state.sqlite3

`--json` daje strojno čitljiv rezultat. `status --verify` dodatno provjerava
integritet baze; ta provjera na velikoj bazi može potrajati.

Preuzimanje pamti **raspone datuma, stranice i red odluka** u SQLiteu.
Zadani početni raspon je od `1900-01-01` do datuma prvog pokretanja;
`--date-from YYYY-MM-DD` i `--date-to YYYY-MM-DD` mijenjaju početni raspon.
Kod nastavka ostaje spremljeni završni datum. Ova verzija još ne proširuje
automatski raspon na novije datume i ne provjerava sve naknadne ispravke.
Drugi raspon zahtijeva zasebnu bazu, a aplikacija odbija tihu promjenu plana.

Portal ograničava pojedinu pretragu na 10.000 rezultata. Collector mjesečne
raspone s tolikim brojem rezultata dijeli na manje raspone, novije prve.
Prije obrade odluka potvrđuje da je portal primijenio tražene datume i sort.
Popis UUID-ova stranice sprema prije prvog dohvaćanja odluke: prekid zato
ostavlja nastavak u redu čekanja. Za dovršetak raspona broj opaženih
jedinstvenih UUID-ova mora odgovarati prijavljenom broju. Ako se ne slaže,
pokušava do dva ponovna obilaska, a zatim staje uz sačuvane podatke.
Jedan dan s 10.000 rezultata također zaustavlja rad i zahtijeva dodatnu
podjelu po sudovima; ne označava se lažno dovršenim.

OS lock sprječava više lokalnih HRPres postupaka za istog korisnika, i za
različite baze. Ne može koordinirati druge uređaje ili dijeljeni izlazni IP.
Program poštuje `Retry-After` te čuva zadanu rezervu od **100 GiB** na disku
(`--min-free-gib` je podesiv). Tijekom rada privremeno sprečava automatsko
uspavljivanje Windowsa; plan napajanja ostaje nepromijenjen. Pauza čeka
završetak trenutačnog HTTP zahtjeva; možeš koristiti i Ctrl+C.

`data/last-run.json` čuva rezultat zadnjeg pokretanja. Pri prekidu već
potvrđene odluke ostaju dostupne. Aktivnu SQLite bazu ne kopirati ručno;
koristiti eksplicitni izvoz kad collector ne radi. **Izvoz i vektorizacija
u starom workflowu i dalje su pune pilotne snimke.** Inkrementalni paketi,
vektorska usporedba i adapteri za aplikacije sljedeća su faza.

Poklapanje brojeva u pojedinom rasponu nije dokaz da su obuhvaćene sve
naknadne objave, nedatirane odluke ili ispravci cijelog izvora. Izvještaj
zato zadržava `coverage_independently_verified: false`.

## Razvoj i stari GitHub pilot

Instalacija i offline testovi (Python 3.11 ili noviji):

    python -m pip install -e ".[test]"
    python -m pytest -q

Probni dohvat na vlastitom računalu nije obvezan; sve se može pokrenuti
GitHub workflowom nakon postavljanja Drive pristupa.

    python -m hrpres collect --db data/state.sqlite3 --max-new 25
    python -m hrpres export --db data/state.sqlite3 --out releases

Program je zasad ograničen na **1.000 novih presuda u jednom pokretanju**.
Nema automatskog rasporeda ni masovnog paralelnog crawlera.

## GitHub Actions: bez vlastitog računala

Workflow: Actions > Manual ANON pilot: text + vectors to Drive > Run workflow.

Prethodno postaviti dvije repo postavke u Settings > Secrets and variables
> Actions:

1. Secret **HRPRES_RCLONE_CONFIG**: cjelokupni sadržaj rclone.conf (OAuth
   pristup isključivo namjenskom javnom korpusu; ne kopirati token u kod).
2. Variable **HRPRES_REMOTE**: na primjer
   gdrive:HR-Sudska-Praksa-ANON, gdje je gdrive naziv rclone remotea.

Root na Driveu:
https://drive.google.com/drive/folders/1d90ZxS9zdI-iDL_bJ1Es2pVgBiUvnVHa

Organizacija:
- 00-manifests/state.sqlite3 — checkpoint s već preuzetim UUID-ovima;
- 06-releases/[release]/ — JSONL tekstovi, FTS, manifest;
- 02-embeddings/[release]/ — float16 vektori, vektorski manifest;
- 03-vector-indexes/[release].faiss — FAISS indeks.

Collector preuzima isključivo javni ANON. Koristi jedan sekvencijalni
proces, razmak 0,7 sekundi između HTTP zahtjeva i GitHub concurrency lock.

**Ograničenje IP adrese:** GitHub-hosted runner može promijeniti izlazni IP
između pokretanja. Prije ozbiljnog povijesnog backfilla treba osigurati
poštivanje službenog uvjeta jedne IP adrese (npr. fiksni egress).
Samo ograničena proba pokreće se na običnom GitHub runneru.

## Zašto dva embedding modela?

- **MiniLM multilingual** je praktičan prvi model za besplatni GitHub CPU:
  mali vektori i kraće vrijeme obrade, ali ne nužno najbolja pravna relevantnost.
- **BAAI/bge-m3** je snažniji višejezični kandidat. GitHub može izračunati
  njegove vektore na CPU-u, ali to je sporije. Prije odabira treba usporediti
  pogodak na uzorku hrvatskih pravnih upita.

Za velike količine može se zadržati isti izlazni format i koristiti
udaljeni GPU ili Colab, bez promjene samog korpusa.

## Nakon pilota

Za više od 1,2 milijuna presuda potrebni su vremenski rasponi s kontrolama
potpunosti, otkrivanje retroaktivnih objava i ispravaka, nepromjenjivi
shardovi, komprimirani FAISS (IVF-PQ ili sličan) te središnji katalog koji
bilježi na kojem Google Drive računu leži svaki paket.

Sadašnji FTS+vektorski snapshot cijelog checkpointa pri svakom pokretanju
**pilotno je rješenje**. Nije pogodno za cijeli povijesni korpus.

## Izvor i sigurnost

Pravila: https://odluke.sudovi.hr/Home/Privacy. Dopušteno automatizirano
preuzimanje uz maksimalno tri poziva u sekundi **s jedne IP adrese** i
obavezno navođenje izvora u proizvodima, bazama i AI derivatima.

ANON odluke su javno anonimizirane, ali moguće su pogreške anonimizacije.
U repozitorij nikada ne unositi korisničke spise, podatke o strankama,
Drive tokene ni sadržaj privatne pravne platforme.

Licencija: AGPL-3.0-only.
