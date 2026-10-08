# research

Python feldolgozás a DatabentoExtract kimenetén (`pip install numpy pandas matplotlib`).

- `profiles.py`: seanszok (18:00-17:00 ET), front kontraktus, volume profile, value area, POC,
  Prominence HVN/LVN és Upper/Lower HVN besorolás a NinjaTrader VolumeProfileTool logikája szerint
  (Custom repó: AddOns/GM/Models/ValueArea.cs, WindowClusters.cs, Helpers/VolumeProfileRenderer.cs).
- `build_sessions.py <extractDir> <outDir>`: a teljes időszak seanszai (`sessions.csv`), HVN/LVN szintjei
  mindkét besorolással (`levels.csv`: `kind_w` forgalommal súlyozva, `kind_u` súlyozás nélkül) és a front kontraktus
  1 perces gyertyái (`bars_1min.csv.gz`).
- `plot_profiles.py <extractDir> <outDir> <YYYY-MM-DD>...`: ábrák a NinjaTraderrel való összevetéshez.

Alapbeállítások (`Settings`): value area 68%, sigma multiplier 0.25, min prominence 5%, max valley 50%,
HVN min asymmetry 0.8, max overshoot 0.25, forgalommal súlyozva, a besorolás az 1 perces záróárakból.
- `zigzag.py`: a ZigZagAtr indikátor TimeOfDay üzemmódja (ATR 14 nap, profil 10 nap, 30 perces sávok) az 1 perces
  gyertyákon; kontraktusváltáskor újraindul. `plot_zigzag.py`: ábra az indikátorral való összevetéshez.
- `reverse_study.py <resultsDir> <extractDir> <outDir> [lastSession]`: fordított vizsgálat, a 0,3 és 0,5 fordulók
  hány százaléka esik egy zónára, a véletlenszerűen eltolt zónákhoz képest (csak a fejlesztési időszak, alapból
  2025-12-31-ig). `analyze_reverse.py <swing_hits.csv.gz> <summary.md>`: összefoglaló táblázatok.
- `event_study.py <resultsDir> <extractDir> <outDir> [lastSession]`: fő eseményvizsgálat, minden zónaérintés
  szimulált fordulós kereskedésként (limit a zóna szélén négy stop-pufferrel, illetve megerősítő záróárra belépés),
  1R/2R/3R célokkal, nettó R-ben, a véletlenszerűen eltolt zónákkal összevetve.
  `analyze_events.py <events.csv.gz> <summary.md>`: összefoglaló táblázatok.
- `export_book_events.py <resultsDir> <events.csv.gz> <book_events.csv>`: az érintések listája a DatabentoExtract
  `book` futtatásához. `book_analysis.py <resultsDir> <outDir>`: a könyv- és orderflow-jellemzők összevetése a
  megerősítéses belépés eredményével, 2024 / 2025 bontásban, a jelölt kizáró szabállyal.
- `refine_study.py <resultsDir> <extractDir> <outDir>`: a jó oldali érintések újraszimulálása másodperces
  gyertyákon (megerősítés a touch-perc vége után, 15 percen belül), strukturális célokkal (az első zóna legalább
  1R / 2R távolságra, `room` = a legközelebbi zóna távolsága R-ben) és hírablakkal (08:30, 10:00, 14:00, 14:30
  percek legalább 3x szokásos forgalommal, -10..+20 perc). `analyze_refine.py <refineDir>`: összefoglaló.
- `holdout.py export|evaluate <holdoutDir>`: a rögzített szabály (2026-10-07, a küszöbök számként befagyasztva) egyszeri
  próbája a 2026-os időszakon. Előtte `event_study.py <resultsDir> <extractDir> <holdoutDir> 2026-12-31`, az `export`
  után a DatabentoExtract `book` a 2026-os fájlokon a `book_events_2026.csv`-vel, a kimenet `<holdoutDir>/book`.
- `position_study.py <resultsDir>`: a rögzített szabály egyszerre egy pozícióval (seanszonként a belépés
  sorrendjében, nyitott pozíció alatt a jel kimarad), 2024-2026, valódi és eltolt zónák külön számlán.
- `pivot_windows.py <resultsDir> <extractDir> <outDir> [atrMultiple] [lastSession]`: az orderflow-sorrend vizsgálat
  első lépése. A ZigZag (TimeOfDay, 0,3 és 0,5) fordulói köré ablak: szint = High - N x ATR1 (ATR(20) 1 perces, a
  forduló előtti gyertyán), T0 = a szint alatti utolsó gyertya zárása, Tend = a forduló utáni első szint alatti gyertya
  zárása (legkésőbb a következő fordulóig), Tx = a High első másodperce. Kimenet: `windows.csv` és `summary.md`
  (esetszámok éjszaka / RTH bontásban, ablakhosszak, a könyvsáv szorzójához a k = (High - belépő nyitó + 20 tick) /
  ATR1 eloszlása). Csak a fejlesztési időszak (2025-12-31-ig).
