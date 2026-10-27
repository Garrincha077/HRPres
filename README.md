# HRPres — hrvatske sudske odluke

Neovisan projekt za preuzimanje, indeksiranje i pretraživanje javnih
anonimiziranih odluka s https://odluke.sudovi.hr/. Korpus i indeksi su
prenosivi: mogu ih koristiti LexHR, druga pravna AI aplikacija i lokalni RAG,
bez slanja privatnih spisa u oblak.

**Izvor:** Tražilica odluka sudova Republike Hrvatske —
https://odluke.sudovi.hr/. Neslužbeni, neovisni projekt.

## Što je spremno

- Kontinuirani lokalni način: preuzimanje i lokalni ONNX vektori bez količinskog limita.
- Novije odluke prve, deduplikacija stabilnog UUID-a,
  SQLite checkpoint za nastavak, izdvajanje teksta i metapodataka.
- Komprimirani JSONL tekstovi, punotekstni SQLite FTS5 i SHA-256 manifest.
- **Vektori na GitHubu**: ručni Actions proces na besplatnom CPU runneru,
  s višejezičnim MiniLM modelom (384 dimenzije), izborno BGE-M3
  (1024 dimenzije, znatno sporiji). Bez lokalne Ollame.
- FAISS točni indeks i float16 arhiv vektora po verziji, s modelom u manifestu.
- Povezivanje s Google Driveom putem rclone konfiguracije u GitHub Secrets.

## Kontinuirani način na Windowsu (0.3)

U izborniku `POKRENI-HRPRES.cmd` odaberi **6 — KONTINUIRANO** ili otvori
`KONTINUIRANO-HRPRES.cmd`. Jedno pokretanje nastavlja preuzimanje i
vektorizaciju do pauze, zatvaranja prozora ili pogreške koja zahtijeva pregled.
Nema zaustavljanja nakon 1.000 odluka ili 200 stranica. Opcije 1 i 2 ostaju
za ograničene probe. Ako drugi collector već radi, prvo ga pauziraj;
kontinuirani način čuva njegov spremljeni red čekanja.

Koristi se prethodno preuzet kvantizirani ONNX model. `.venv-vectors` ima
CPU ovisnosti, a `vectors/pilot-25-minilm-onnx/model` ima provjereni model.
Kad taj paket postoji, instalacijska skripta priprema i vektorsko okruženje;
ne preuzima dodatne modele. Preuzimanje i vektori rade lokalno u dva radnika.
Vektori se dopisuju u `data/vectors.sqlite3`; svaki odlomak i njegova
potvrda obrade spremaju se u istoj transakciji. Nakon prekida obrađuje se
samo nedovršeno. Postojeća 202 vektora pilotnih 25 odluka koriste se ponovno.

**Pauza:** `PAUZIRAJ-HRPRES.cmd` ili Ctrl+C zaustavlja oba posla. Sljedeće
pokretanje nastavlja spremljene stranice i odlomke. Privremena mrežna greška
aktivira automatski ponovni pokušaj uz čekanje od 30 do 300 sekundi, odnosno
dulje ako to traži `Retry-After`. Manjak prostora, promjena izgleda izvora,
neslaganje brojeva ili greška modela zaustavljaju rad uz sačuvane podatke.

Datum završetka proširuje se na današnji datum. Svakih 30 minuta ponovno se
pregleda jedan dovršeni raspon iz zadnjih 90 dana, što otkriva nove UUID-ove
objavljene naknadno. Odjednom se ponovno otvara samo jedan raspon kako se
staro preuzimanje ne bi stalno prekidalo. Nakon pregleda svih raspona program
čeka sljedeću provjeru. To nije potpuna provjera ispravaka teksta postojećih
UUID-ova, nedatiranih odluka ili naknadnih objava starijih od 90 dana.

Opcija **7 — Pretraži po značenju** i `PRETRAZI-VEKTORE.cmd` pretražuju sve
obrađene odluke, uključujući nove. Status pokazuje odvojeno broj preuzetih i
vektoriziranih odlomaka. Pretraga čita vektore u malim blokovima i ne učitava
cijeli korpus u RAM; vrijeme raste s veličinom indeksa. Za interaktivnu
pretragu milijuna odluka kasnije treba komprimirani indeks.

    python -m hrpres.continuous run --root D:\HRPres
    python -m hrpres.continuous status --root D:\HRPres
    python -m hrpres.continuous search --root D:\HRPres

`continuous drain` jednokratno vektorizira odlomke koji su bili spremljeni
na početku poziva; može raditi dok postojeći collector preuzima dalje.
`download --continuous` uključuje samo kontinuirani collector. Zajednička
opcija 6 preporučena je za preuzimanje i vektore zajedno. Za rad nakon
gašenja računala treba ponovno otvoriti pokretač; automatsko pokretanje uz
Windows nije uključeno.

## Lokalno ograničeno preuzimanje

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
Kod ograničenog nastavka ostaje spremljeni završni datum. Kontinuirani način
proširuje ga i prati nove objave kako je opisano iznad.
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
vektorska usporedba i adapteri za aplikacije ostaju zaseban rad. Lokalni
kontinuirani način koristi inkrementalnu vektorsku bazu.

Poklapanje brojeva u pojedinom rasponu nije dokaz da su obuhvaćene sve
naknadne objave, nedatirane odluke ili ispravci cijelog izvora. Izvještaj
zato zadržava `coverage_independently_verified: false`.

## Provjereni vektorski pilot: točno 25 odluka

Workflow `Vector pilot: fixed 25 decisions on CPU` obrađuje zaključeni javni
paket u `examples/public-pilot-25`, bez dodatnog dohvata s ANON-a i bez Drivea.
Iz 25 odluka i 58 izvornih odlomaka izrađeno je **202 vektora od 384 dimenzije**.
[Stvarno uspješno pokretanje](https://github.com/Garrincha077/HRPres/actions/runs/40427909573).

Koristi se točno određena revizija višejezičnog MiniLM-a u kvantiziranom
ONNX CPU obliku. Tokenizer dijeli tekst na segmente do 128 tokena uz
preklapanje od 24 tokena. Provjera potvrđuje pokrivenost cijelog izvornog
teksta i SHA-256 svih datoteka. Isti model, tokenizer i izračun vektora rade
na GitHubu i lokalno; nema ovisnosti o Ollami ili generativnom modelu.

Instalacija i naredbe za reproduciranje:

    python -m pip install ".[onnx]" faiss-cpu
    python -m hrpres.onnx_pilot build --release examples/public-pilot-25 --out vector-pilot
    python -m hrpres.onnx_pilot verify --bundle vector-pilot
    python -m hrpres.onnx_pilot interactive --bundle vector-pilot

Za lokalne upite dovoljni su NumPy, ONNX Runtime i Tokenizers; račun se radi
nad spremljenim vektorima, a FAISS datoteka ostaje u paketu za druge čitače.
Artifact sadrži i lokalni model pa sama pretraga ne traži mrežni pristup.
Model se učitava jednom za više upita u interaktivnom načinu.

Na testiranom i3-8100 probni upiti nakon učitavanja trajali su približno
0,015–0,016 s, uz oko 438 MiB radne memorije procesa. To su mjerenja ovog
malog funkcionalnog pilota; nisu procjena brzine cijelog korpusa niti potvrda
pravne relevantnosti. Kontinuirani lokalni rad koristi isti provjereni model.

Windows pokretač `PRETRAZI-VEKTORE-25.cmd` očekuje instaliran paket u
`vectors/pilot-25-minilm-onnx`, zasebno okruženje `.venv-vectors` i modul u
`app/src/hrpres/onnx_pilot.py`, relativno prema vlastitoj mapi.

## Prva provjera tekstualnog pilota

Instalacija i offline testovi (Python 3.11 ili noviji):

    python -m pip install -e ".[test]"
    python -m pytest -q

Probni dohvat na vlastitom računalu nije obvezan; sve se može pokrenuti
GitHub workflowom nakon postavljanja Drive pristupa.

    python -m hrpres collect --db data/state.sqlite3 --max-new 25
    python -m hrpres export --db data/state.sqlite3 --out releases

Stara naredba `collect` i ograničeni `download` dopuštaju najviše
**1.000 novih presuda u jednom pokretanju**. Kontinuirani način nema taj limit.
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
