# DatabentoTools

## DatabentoExtract

Databento MBO (`.dbn` / `.dbn.zst`, DBN v1-v3) kivonatoló.

```
DatabentoExtract diag <file.dbn.zst> <outDir>
DatabentoExtract extract <inputDir|file> <outDir> [--source fills|trades] [--min-share 0.01] [--parallel 2] [--force]
```

Kimenet fájlonként:

- `minute/<name>.csv.gz`: utc_minute, instrument_id, price, buy, sell, unknown (agresszív forgalom árszintenként)
- `seconds/<name>.csv.gz`: utc_second, instrument_id, open, high, low, close, volume, buy, sell
- `instruments/<name>.csv`: a fájl összes kontraktusa szimbólummal és rekordstatisztikával
- `diag` módban még: `<name>_diag_summary.txt`, `<name>_diag_compare.csv` (trade és fill forgalom árszintenként)

Az árszintenkénti forgalom alapból a fill (F) rekordokból számolódik, az agresszor a nyugvó megbízás ellentétes oldala.
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
