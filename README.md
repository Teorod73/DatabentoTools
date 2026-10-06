# DatabentoTools

## DatabentoExtract

Databento MBO (`.dbn` / `.dbn.zst`, DBN v1-v3) kivonatoló.

```
.\DatabentoExtract.exe diag <file.dbn.zst> <outDir>
.\DatabentoExtract.exe extract <inputDir|file> <outDir> [--source trades|fills] [--min-share 0.01] [--parallel 2] [--force]
```

PowerShellben az aktuális mappában lévő programot `.\` előtaggal kell indítani.

Kimenet fájlonként:

- `minute/<name>.csv.gz`: utc_minute, instrument_id, price, buy, sell, unknown (agresszív forgalom árszintenként)
- `seconds/<name>.csv.gz`: utc_second, instrument_id, open, high, low, close, volume, buy, sell
- `instruments/<name>.csv`: a fájl összes kontraktusa szimbólummal és rekordstatisztikával
- `diag` módban még: `<name>_diag_summary.txt`, `<name>_diag_compare.csv` (trade és fill forgalom árszintenként)

Az árszintenkénti forgalom alapból a trade (T) rekordokból számolódik: a GLBX.MDP3 adatban egy söprő megbízás
árszintenként külön trade rekord, és a trade forgalom egyezik a tőzsdei forgalommal. A fill (F) rekordok összege
kb. 1%-kal több (2026-05-29-es diagnosztika), ezért azok csak összehasonlításra valók (`--source fills`).
A Databento napi fájljai UTC napok (00:00-24:00 UTC), nem CME seanszok.
Az időpontok UTC unix másodpercben vannak.

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
```
