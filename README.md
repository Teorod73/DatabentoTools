# DatabentoTools

## DatabentoExtract

Databento MBO (`.dbn` / `.dbn.zst`, DBN v1-v3) kivonatoló.

```
.\DatabentoExtract.exe diag <file.dbn.zst> <outDir>
.\DatabentoExtract.exe extract <inputDir|file> <outDir> [--source trades|fills] [--min-share 0.01] [--parallel 2] [--force]
.\DatabentoExtract.exe book <inputDir|file> <book_events.csv> <outDir> [--parallel 2] [--force]
.\DatabentoExtract.exe pivots <inputDir|file> <pivot_events.csv> <outDir> [--parallel 2] [--force]
```

PowerShellben az aktuális mappában lévő programot `.\` előtaggal kell indítani.

Kimenet fájlonként:

- `minute/<name>.csv.gz`: utc_minute, instrument_id, price, buy, sell, unknown (agresszív forgalom árszintenként)
- `seconds/<name>.csv.gz`: utc_second, instrument_id, open, high, low, close, volume, buy, sell
- `instruments/<name>.csv`: a fájl összes kontraktusa szimbólummal és rekordstatisztikával
- `diag` módban még: `<name>_diag_summary.txt`, `<name>_diag_compare.csv` (trade és fill forgalom árszintenként),
  `<name>_diag_hidden.txt` (a látható nyugvó méretnél nagyobb kötések: natív iceberg vagy implied volumen; hány ilyen
  van, van-e hozzájuk fill, a látható méretnél nagyobb fill, méretnövelő M, C vagy új A az eseményben, és az első 40
  ilyen esemény rekordjai a következő eseménnyel együtt)

Az árszintenkénti forgalom alapból a trade (T) rekordokból számolódik: a GLBX.MDP3 adatban egy söprő megbízás
árszintenként külön trade rekord, és a trade forgalom egyezik a tőzsdei forgalommal. A fill (F) rekordok összege
kb. 1%-kal több (2026-05-29-es diagnosztika), ezért azok csak összehasonlításra valók (`--source fills`).
A Databento napi fájljai UTC napok (00:00-24:00 UTC), nem CME seanszok.
Az időpontok UTC unix másodpercben vannak.

`book` mód: a zónaérintések (ResearchData `results/events/book_events.csv`, a `research/export_book_events.py`
készíti) körül a könyv- és orderflow-jellemzők fájlonként a `book/<name>.csv`-be: a védekező oldal nyugvó mérete a
zónán az érintés percének elején, 60 és 180 másodperc után; az agresszív forgalom a zónán és összesen; a védekező
oldal hozzáadott, visszavont és teljesült mérete; újratöltés (hozzáadás 1 másodpercen belül egy támadó kötés után
ugyanazon az áron); nagy kötések; az előző 5 perc forgalma. A trade és fill rekordok nem változtatják a könyvet, a
teljesülés a következő cancel vagy modify rekordban jelenik meg, ezt a fill méretével levonva különválasztja a
visszavonástól. Teszt: `tests/make_book_test.py` (független Python számítással bájtra egyező).

`pivots` mód: a ZigZag fordulók ablakaiban (ResearchData `results/pivots/pivot_events.csv`, a
`research/pivot_windows.py` készíti) másodpercenkénti orderflow fájlonként a `pivots/<name>.csv.gz`-be. Egy sor egy
ablak egy másodperce (UTC), minden forgalmi oszlop az abban a másodpercben történt változás:

- `buy`, `sell`, `trades`: agresszív vétel / eladás volumene és a kötések száma; `last`, `high`, `low`: kötésárak,
  `bid`, `ask`: a legjobb árak a másodperc végén.
- `ask_*`, `bid_*`: az adott oldal betett (`add`), teljesülés nélkül kivett (`cancel`) és teljesült (`fill`) mérete
  a sávban; `_w1` súlya 1/(1+d), `_wl` súlya max(0, 1 - d/H), ahol d a változás előtti legjobb ártól mért tick, H a
  sáv fele tickben. `rest`: nyugvó méret a sávban a másodperc végén.
- `ask_refill`, `bid_refill`: újratöltés a legjobb áron. Egy epizód addig tart, amíg az oldal legjobb ára nem
  változik; Q0 = a kezdő nyugvó méret, A = az ott teljesült agresszív volumen, a számláló a lezárt epizódok
  max(0, A - Q0) összege plusz a nyitotté (a másodpercben a növekmény). `_ep_up` / `_ep_down`: lezárt epizódok,
  amelyeknél az ár felfelé / lefelé lépett.
- `ask_hidden`, `bid_hidden`: a látható méret fölötti agresszív volumen eseményenként és áranként, max(0, kötött
  volumen - az oldal nyugvó mérete az áron az esemény első ottani kötése előtt). Natív iceberg és implied likviditás
  (ez utóbbi nincs az MBO könyvben), a kettő nem választható szét. `ask_refill_visible`, `bid_refill_visible`: a
  refill a rejtett volumen nélkül, epizódonként max(0, A - H - Q0), H = az epizód árán mért rejtett volumen.
- `large_buy_20`, `large_sell_20`, `large_buy_60`, `large_sell_60`: legalább 20 / 60 kontraktusos agresszív sorozatok
  volumene az AgressiveDetector logikájával (azonos oldali kötések az első kötéstől 10 ms-on belül), abban a
  másodpercben, amikor a sorozat lezárul (ellentétes vagy oldal nélküli kötés, vagy a kontraktus 10 ms-nál későbbi
  rekordja).
- `crossed`: 1, ha a legjobb bid nem kisebb a legjobb asknál (adatminőség).

A legjobb árak egy esemény végén értékelődnek ki (a last flagű rekord vagy egy későbbi időbélyeg), nem az esemény
minden rekordja után. A könyv minden fájlban a nap eleji snapshotból épül, ezért az UTC napon átnyúló ablakok nincsenek
az eseményfájlban. Teszt: `tests/make_pivot_test.py` (független Python számítás, bájtra egyező).

### Fordítás

```
dotnet build -c Release DatabentoExtract
dotnet publish DatabentoExtract -c Release -r win-x64 --self-contained true -p:PublishSingleFile=true -o publish
```

### Teszt

`tests/make_test.py` a hivatalos `databento-dbn` csomaggal szintetikus MBO fájlt készít (v1-v3) és a várt kivonatot,
a program kimenete ezzel bájtra egyezik:

```
pip install databento-dbn zstandard
python tests/make_test.py 3
DatabentoExtract extract test_v3.mbo.dbn.zst out
python tests/make_pivot_test.py 1
DatabentoExtract pivots pivot_test.mbo.dbn.zst pivot_test_windows.csv out
```

A `pivots` teszt várt kimenete `pivot_test_expected.csv`, ezzel kell egyeznie a kicsomagolt
`out/pivots/pivot_test.csv.gz`-nek.
