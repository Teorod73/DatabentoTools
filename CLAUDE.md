# Projekt-emlékeztető Claude-nak

Ez a fájl az előző beszélgetések összefoglalója, hogy egy új beszélgetés onnan folytathassa. Olvasd el, mielőtt
bármit csinálsz. Ha valami ellentmond a kódnak, a kód az igaz, és ezt jelezd. Ha a projekt állása változik
(új eredmény, új döntés), frissítsd ezt a fájlt is.

## A felhasználó és a munkamód

- Magyarul beszélünk. A kód, a commit üzenetek és a kódkommentek angolul vannak, a README-k magyarul.
- **Csak akkor kódolj, ha kifejezetten kéri.** Kérdésre válaszolj, tervet írj le, de ne kezdj kódolni magadtól.
- Nem használ ágakat: **minden közvetlenül a `master`-re megy** (DatabentoTools, ResearchData, Custom).
- Windowson dolgozik PowerShellben (`.\` prefix kell a programokhoz). Útvonalak nála:
  - nyers Databento fájlok: `C:\Users\istva\Documents\NinjaDataBridge\Databento_ES` (2026: `...\Databento_ES\2026`)
  - ResearchData klón: `C:\Users\istva\Documents\NinjaDataBridge\Databento_ES\research\extract`
  - DatabentoTools klón: `C:\Users\istva\Documents\NinjaDataBridge\Databento_ES\DatabentoTools`
- A nyers `.dbn.zst` fájlok csak nála vannak: a C# futtatásokat (extract, book) ő végzi és tölti fel.
- Őszinte értékelést vár: a bizonytalanságot, a lookaheadet és azt, hogy egy év független-e, mindig mondd meg.

## Repók

- **Teorod73/Custom** (NinjaTrader 8 `bin\Custom`): NinjaScript. `Indicators/GM/ZigZagAtr.cs` (Atr / NightDay /
  TimeOfDay referencia), `AddOns/GM/...` (VolumeProfileTool: ValueArea, WindowClusters, VolumeProfileRenderer).
  A NinjaTrader minden `.cs`-t lefordít, ezért konzolprogram nem kerülhet ide. A "NinjaScript generated code"
  régiót soha ne írd kézzel.
- **Teorod73/DatabentoTools** (ez a repó): `DatabentoExtract/` C# konzolprogram (.NET 8, RollForward Major,
  ZstdSharp), `research/` Python elemzés, `tests/` szintetikus bájtpontos tesztek. Részletek: `README.md`,
  `research/README.md`.
- **Teorod73/ResearchData**: `minute/`, `seconds/`, `instruments/` (az extract kimenete) és `results/`
  (`sessions.csv`, `levels.csv`, `bars_1min.csv.gz`, `reverse/`, `events/`, `book/`, `book_analysis/`, `refine/`,
  `holdout/`, `pivots/`). A cloud környezetben `/home/user/researchdata`-ba klónozva használtuk.

## Adat

- ES, Databento GLBX MDP3 MBO, napi UTC fájlok minden kontraktussal, 2024-01-01 – 2026-10-04.
- Seansz 18:00–17:00 ET, front kontraktus forgalom szerint, roll spread a perces adatból.
- Éjszaka (overnight) 20:00–09:30, IB 09:30–10:30 ET.
- **Fejlesztési időszak: 2024–2025. 2026 = holdout, egyszer lefuttatva (2026-10-07), már elhasználva.**
  Új, tiszta teszt csak a 2026 októberétől érkező élő adat lehet.

## A folyamat (szkriptek)

1. `DatabentoExtract extract` → perc/másodperc gyertyák, árankénti agresszor-volumen.
2. `build_sessions.py` → seanszok, volume profile (VAH/VAL/POC, value area 68%), Prominence HVN/LVN,
   Upper/Lower HVN besorolás kétféleképpen (`_w` forgalommal súlyozott, `_u` súlyozatlan; az elemzések `_u`-t
   használnak, `weighting != "w"`). NinjaTraderrel egyeztetve.
3. `zigzag.py`, `reverse_study.py` → fordított vizsgálat: a ZigZag (TimeOfDay, 0,3 és 0,5) fordulói mennyivel
   gyakrabban esnek zónára, mint eltolt zónára (lift ~1,13–1,17; irányfüggő: VAH/high/ONH csúcsnál, VAL/low/ONL/
   Lower HVN aljnál 1,4–1,9; régi szintek és kerek számok ≈ véletlen).
4. `event_study.py` → minden zónaérintés szimulált fordulós kötés (limit és megerősítéses belépés), nettó R,
   összevetve véletlenszerűen ±0,1–0,5 ATR-rel eltolt zónákkal. Önmagában minden változat enyhén negatív.
5. `export_book_events.py` → `DatabentoExtract book` → `book_analysis.py`: könyv- és orderflow-jellemzők az
   érintések körül (elő-ablak 5 perc, touch-perc W1, 3 perc W3, sáv = zóna ± 0,03 ATR).
6. `refine_study.py` / `analyze_refine.py`: másodperces belépés, strukturális célok, hírszűrő → egyik sem javított.
7. `holdout.py` → a rögzített szabály egyszeri próbája 2026-on. `position_study.py` → egy pozíció egyszerre.
8. `plot_rule.py` → a szabály ábrája (a teszt függvényeiből számolva), `research/figures/`.

## A rögzített szabály (holdout.py, 2026-10-07)

- Zóna a „jó oldalán” (`event_study.EXPECTED`: high/vah/onh/ibh/hvn_upper short, low/val/onl/ibl/hvn_lower long,
  a többi mindkét irány), teljes seansz, nem 17:00 után.
- Érintés: perces gyertya eléri a zóna ± 0,03 ATR sávot, miután egy gyertya teljesen ≥ 0,15 ATR-re volt.
  ATR = az előző 14 seansz true range átlaga.
- Konfluencia ≥ 3 (legalább 3 másik különböző zóna 0,1 ATR-en belül).
- Kizáró könyvszabály (bármelyik teljesül → nincs kötés; hiányzó adat nem zár ki), küszöbök a 2024-es valódi
  érintések q80/q20 értékei:
  - `attack_vs_resting_1` (támadó volumen a sávban a touch-percben / (nyugvó védekező méret T0-kor + 1)) > 0,8242
  - `large_att_1` (≥ 20 kontraktusos támadó kötések száma a sávban) ≥ 13
  - `pre_attack_delta` (az előző 5 perc deltája a támadás irányában) > 0,1361
  - `added_vs_filled_1` (betett védekező méret / (teljesült + 1)) < 4,835
  - `defense_kept_1` (nyugvó védekező méret a perc végén / T0-kor) < 0,7007
- Belépés: az érintés percétől 15 percen belül az első perc, amely legalább 0,05 ATR-rel a közeli szél mögé zár,
  annak záróárán. Ha előtte az ár 0,30 ATR-rel túlmegy a túlsó szélen: nincs kötés.
- Stop: a szélsőérték az érintéstől a megerősítésig ± (0,05 ATR + 1 tick), nem mozdul. Cél: 2R.
  240 perc vagy seansz vége után kilépés záróáron. Költség: stopnál 1 tick csúszás, 0,08 pont jutalék.
- Az ATR-szorzók (0,03 / 0,05 / 0,15 / 0,30) előre választottak, nem optimalizáltak. Érzékenységvizsgálat még
  nem készült (csak 2024–2025-ön lenne szabad).

## Eredmények (nettó R/kötés, ± 95%, seanszonként klaszterezve)

| | 2024 | 2025 | 2026 (holdout) |
|---|---|---|---|
| szabály, minden jel | +0,01 | +0,11 | +0,11 ±0,09 (szigorú kritérium: „strong pass”) |
| szabály, egy pozíció egyszerre | +0,07 ±0,13 | +0,10 ±0,10 | +0,15 ±0,11, +88R, max DD 15R |
| ugyanez eltolt zónákon | −0,11 | −0,01 | −0,06 |

- 2025 nem független ellenőrzés (a jellemzőket mindkét év alapján választottam), 2026 az.
- Short minden évben erősebb (2026: short +0,21, long +0,10, egy pozícióval). Utólagos bontás, nem hangolni rá.
- Kockázat mediánja kb. 10 pont (0,11 ATR); nyerési arány (2R) kb. 31–34%.

## Új vizsgálat: orderflow-sorrend a ZigZag fordulóknál (2026-10-08-tól)

Cél: a fordulók (csak ezek, jelöltek / kontrollcsoport nélkül, a felhasználó döntése) ablakában másodpercenként mért
görbék eseményeinek **sorrendjében** szabályosságot találni (Absorption / Exhaustion). Az idő nem számít, csak a
sorrend, és hogy minden esemény az ablakon belül történjen.

- Fordulók: ZigZag TimeOfDay 0,3 (sárga) és 0,5 (kék). Ablak 2 x ATR1-gyel kezdve (később 2,5 / 3 / 3,5 / 4 is).
  ATR1 = ATR(20) 1 perces, a High előtti gyertyán (T0-é körkörös lenne). High-nál: szint = High - 2 x ATR1, T0 = visszafelé az utolsó szint alatti gyertya zárása,
  vége = a High utáni első szint alatti gyertya. Low tükrözve.
- Külön: éjszaka 20:00-09:30 és RTH 09:30-16:00 (Tx szerint); 16:00-20:00 nem vizsgált. Nagy agresszív küszöb:
  RTH 60, éjszaka 20, az AgressiveDetector logikájával (Custom: `Indicators/GM/AgressiveDetector.cs`, azonos oldali
  kötések az első kötéstől 10 ms-on belül összegezve).
- Görbék (30 s gördülő, másodpercenként): delta%, Ask/Bid cancel a sávban (távolsággal súlyozva: 1/(1+tick) és
  lineáris is), Ask/Bid refill a BestAsk/BestBid-en (folyamatosan: max(0, eddigi agresszív - kezdő passzív)),
  vevő/eladó hatékonyság (pont / kontraktus fix volumenkosárral), nagy vevők/eladók aránya, könyv balansz
  (A-B)/(A+B). A könyvsáv két fix ár (lásd lent).
- Események: kvantilis-szintek (q20/q80, 2024-ből, ülésszakonként), 5 s hiszterézis, első előfordulás, CUSUM
  meredekségváltozás, keresztezések, divergenciák (új csúcs gyengébb görbével), agresszív volumen tetőzés.
- Mintakeresés: rendezett részsorozatok gyakorisága; véletlen szint permutációs próbával (sima: fordulón belüli
  keverés; szigorú: keverés csak a High előtti és utáni részen belül), 99%-os küszöb. Rangsor: gyakoriság, holtversenynél
  a lefutás elején teljesülő. Párok -> hármasok -> négyesek csak átment mintákból. 2024-en keresés, 2025-ön ellenőrzés,
  utána gazdasági próba mindkét keverés mintáira (a teljes seanszokon is, a téves jelek miatt).
- Könyvsáv (2026-10-08 döntés): belépő gyertya nyitó ± (High - belépő nyitó + 20 tick), esetenként, közös N nincs.
  Élőben ugyanez a jelölt csúcsból: egy gyertya, amelyet alacsonyabb High-ú követ (egyenlő High-nál az első
  szigorúan alacsonyabbig várunk, a csúcs az első gyertya). A sávos görbéket élőben visszamenőleg kell számolni.
- Kizárva: a High gyertyája a belépő gyertya (nincs egész perc a High előtt), és az UTC napon átnyúló ablak.
- 1. lépés kész (`research/pivot_windows.py`, ResearchData `results/pivots/`): 2024-2025-ben **4668 vizsgált
  ablak** (éjszaka 2978, RTH 1690). A láb rövidebb 2 x ATR1-nél (nincs kezdet vagy vég) 1202 esetben, a High előtt 0
  perc 516, UTC napon átnyúló 19. Medián ablak 4-5 perc a High előtt, 5-6 perc utána. Sávfél medián 39 tick
  (éjszaka 33, RTH 52), q99 130.
- 2. lépés: `DatabentoExtract pivots` (C#, `PivotFlow.cs`, teszt `tests/make_pivot_test.py`) a
  `results/pivots/pivot_events.csv`-ből másodpercenkénti nyers számlálókat ír (`pivots/<name>.csv.gz`, oszlopok a
  README-ben). A felhasználónak kell lefuttatnia a nyers fájlokon és feltöltenie.
- Refill kettébontva (2026-10-08): `*_hidden` = a látható méret fölötti kötött volumen, `*_refill_visible` = refill a
  rejtett rész nélkül (szintetikus újratöltés). Diagnosztika (`diag` → `<name>_diag_hidden.txt`, 2025-03-05): a
  rejtett volumen az agresszív volumen 1,04%-a, szinte csak **natív iceberg** (implied gyakorlatilag nincs). A natív
  iceberg: F a látható méretnél nagyobb, utána M ugyanazon a rendelésen visszaállítja a látható részt (új A nincs).
- Ezért a `pivots` könyvkezelése (2026-10-08 javítás): a C/M-nél az összes függő fill teljesült, csak a fill után
  megmaradónál kisebb méret cancel, a nagyobb `*_reload` (nem add). A `pivots` futtatás most jöhet.
- `pivots` futtatás kész (2026-10-08): 4668 ablak, minden sor megvan, a buy/sell 100%-ban egyezik az `extract`
  másodperceivel. Keresztben álló könyv: 4 ablak 10:00:00 ET-kor 5-10 s-ig (valószínűleg CME velocity logic hírkor),
  és 1 ablak a 17:00-18:00 szüneten át (kizárva, `crosses_break`) → **4667 ablak**. 9 pár ablaknak azonos a kezdete
  és a vége (két közeli csúcs), bent hagyva. Arányok az agresszív volumenhez: fill 50,5% oldalanként, refill 20%,
  ebből látható 19,7%, natív iceberg 0,5%, reload 0,7%.
- 3. lépés (`research/pivot_curves.py`, ResearchData `results/pivots/curves/`, csak 2024): a High és a tükrözött
  Low görbéi szinte azonosak (összevonhatók). A csúcs előtt „gyorsulás” (delta, hatékonyság, volumen, nagy támadók
  felfut, a védő refill a csúcsig csökken), a fordulás jelei a csúcs után jönnek. A sávok szélesek. Mechanikus
  (az ár mozgásából következő) görbék: delta, hatékonyság, és a fix sávú **balansz** (V alakú, mert a csúcson a
  sáv teteje közel van: a sáv geometriája, nem orderflow; ár-szintenkénti sűrűséggel kell újradefiniálni).
- Balansz átírva szintenkénti sűrűségre (nyugvó méret / árszintek száma a legjobb ártól a sáv széléig): éjszaka
  lapos, RTH-ban a csúcs felé nő; részben még mindig geometria lehet (a mélység a legjobb ár közelében sűrűbb).
- **Balansz újra (2026-10-09, felhasználói döntés):** High-nál az ask oldal teteje a belépéstől rögzített (csúcs +
  20 tick, a sáv teteje), a védő méret a legjobb asktól eddig; a bid oldalon mindig annyi szint, ahány a legjobb asktól
  a rögzített tetőig van, a legjobb bidtől lefelé. Low tükrözve. Balansz = (védő − támadó) / összeg, azonos szintszámon,
  sűrűség nélkül. C#: új `bid_rest_top`, `ask_rest_bottom` oszlopok (teszt bájtra egyezik). A felhasználónak újra kell
  futtatnia a `pivots`-ot (`--force`) a `pivot_events.csv`-re és a `candidate_events.csv`-re; utána a 4-6. lépés
  balansz-eseményei újraszámolandók.
- Új balansz futtatva (2026-10-09, a többi oszlop bájtra azonos a régi futással), 3. lépés újraszámolva: az RTH-s
  púp (a geometria) eltűnt, a görbe közel lapos, medián 0 és +0,05 között (High kb. 0,03-mal a tükrözött Low fölött).
  Kis kiugrás közvetlenül a csúcson (RTH Low 0,0 → 0,03), utána gyors visszaesés; ez részben még mechanikus lehet
  (gyors mozgás után a támadó oldal könyve a legjobb ár mögött még nem töltődött vissza).
- 4-6. lépés újrafuttatva az új balansszal (2026-10-10): csak a balansz-események változtak (RTH-ban a `balance_neg`
  / `balance_lo` a csúcs utánról a csúcs elé került, u ≈ -0,3 / -0,4). Az átment balanszos minták jóval kevesebbek
  (RTH hármas 54 → 29, négyes 79 → 23, éjszakai hármas 200 → 79): a régiek egy része a geometriából jött; a
  megmaradók 2025-ben is nagyrészt átmennek, de ritkák (RTH hármas az ablakok kb. 21%-a). A 70 kiválasztott minta
  azonos (egyikben sincs balansz), a 6. lépés eredménye bájtra azonos.
- 4. lépés (`research/pivot_sequences.py`, ResearchData `results/pivots/sequences/`): 64 eseménytípus. Az első,
  laza definícióval (q20/q80, 5 s) szinte minden esemény az ablakok 90-100%-ában és rögtön T0 után történt, ezért
  szigorítva: q95/q5, 15 s, CUSUM H = 10 (csak az esemény-gyakoriságok alapján, eredményre nem hangolva). Medián 37
  esemény ablakonként, 19 esemény mindkét évben ≥ 90%. **Gond:** 2025-ben sok arány-esemény ritkább (pl. delta_hi éjszaka
  33% → 17%, def_refill_hi 39% → 14%), mert 2025-ben több a kötés (éjszaka +25%, RTH +29% / s), és a 30 s-os
  arányok kevésbé szélsőségesek. Javaslat: az arány-görbék gördülő ablaka fix idő helyett fix kontraktusszám legyen.
- Kontraktus-alapú ablak (2026-10-08): az arány-görbék az utolsó N agresszív kontraktuson (N = 2024 30 s-os medián
  volumene ülésszakonként), a meglévő másodperces adatból (C# újrafuttatás nem kellett). **Nem oldotta meg** a
  2025-ös eltolódást (még mindig 36 esemény > 10 pont eltérés). Az ok: a kötésméret csökken (átlag éjszaka 2,88 → 2,38,
  RTH 3,62 → 2,83 kontraktus / kötés), és a görbék szélsőértékei már 2024-en belül is folyamatosan húzódnak össze
  (éjszakai def_refill q95: 2024 Q1 0,87 → Q4 0,75 → 2025 Q2 0,52). A fix 2024-es küszöb és a fix 20/60 kontraktusos
  nagy-küszöb ezért rezsimfüggő. Javaslat: csúszó küszöbök (az előző kb. 60 nap ablakaiból, ülésszakonként).
- **Csúszó küszöbök (2026-10-08 döntés):** minden ablak küszöbei (q95/q5, a meredekség szórása) az előző 60 naptári
  nap ablakaiból, ülésszakonként, a saját seansz nélkül; legalább 20 korábbi seansz kell (2024 eleje kimarad:
  vizsgált 2024 night 1367, rth 765, 2025 night 1511, rth 862). Ezzel a két év esemény-gyakorisága közti eltérés
  mediánja 2,1 pont, egy esemény sem tér el 10 pontnál többel (előtte 36). Élőben is így számolható.
- **Relatív nagy-küszöb (döntés):** a fix 60/20 helyett naponta relatív. A `pivots` mód most 20 küszöbbel (5-400)
  írja a nagy sorozatok volumenét és a `series/<name>.csv.gz`-be a teljes nap sorozatméret-eloszlását. Terv: a
  küszöb az a méret, amely fölött az előző 60 nap front kontraktusának agresszív volumenéből ugyanakkora rész esik,
  mint 2025 utolsó 60 napján a 60-as (RTH) / 20-as (éjszaka) küszöb fölött (a felhasználó a 60/20-at a mostani piachoz
  állította be). A felhasználónak újra kell futtatnia a `pivots`-ot (`--force`).
- Relatív nagy-küszöb kész (`pivot_curves.large_limits`, `sequences/large_limits.csv`): a 60/20 fölötti volumen
  része 2025 végén RTH 9,0%, éjszaka 19,2%; ugyanez a rész az előző 60 napon adja a napi küszöböt. Medián küszöb
  negyedévenként: éjszaka 40 (2024 Q1) → 25 (2025), RTH 100 → 60-80. Esemény-gyakoriság eltérése 2024 és 2025 között
  medián 2,1 pont, 2 esemény > 10 pont (cross_large, div_att_large éjszaka, +11). A `*_large_lo` szinte mindig
  megtörténik (a sok nulla miatt), önmagában nem informatív. **A 4. lépés kész, jöhet az 5. (mintakeresés).**
- 5. lépés (`research/pivot_patterns.py`, ResearchData `results/pivots/patterns/`, 1000 keverés): az első
  próbában a sima és a szigorú keverésen szinte minden minta átment, mert az események tipikus időzítése (korai /
  késői) és az azonos görbéből vett események (emelkedés → esés → tetőzés) mechanikusan rendezettek. Ezért a döntő
  szint az „időzítés” keverés (minden esemény megtartja saját időzítését és gyakoriságát, csak az ablakok
  keverednek, hasonló eseményszámúak között), azonos görbéből két esemény nem lehet egy mintában, és a mindkét
  évben ≥ 90%-os események kimaradnak. Eredmény: sok minta átmegy 2024-ben és 2025-ben is (éjszaka 384 pár, RTH 201),
  de a gyakori párok (50-65%) a vártnál csak 1,1-1,3-szor gyakoribbak; a négyesek 3-4,5-szörösek, viszont csak az
  ablakok 9-16%-ában. RTH legerősebb (csak könyv/orderflow): cancel-esés → nagy támadók tetőzése → volumen-tetőzés,
  a csúcs előtt (u ≈ -0,3): climax / exhaustion kép. Éjszaka: nagy támadók nőnek → védő cancel csökken → támadó
  cancel nő → nagy védők csökkennek, a csúcs körül (u ≈ 0,05). Nincs minta, amely a fordulók nagy részét lefedné.
  Fontos korlát: az átmenés csak azt mutatja, hogy a fordulókon az események együtt járnak; azt nem, hogy nem
  fordulón ritkábbak. Ehhez a gazdasági próbához nem forduló jelölt-csúcsokon is kell C# adat.
- 6. lépés előkészítve (gazdasági próba, `research/candidate_windows.py`, ResearchData `results/candidates/`):
  jelölt = gyertya, amelyet alacsonyabb High-ú követ és a belépő óta a legmagasabb (Low tükrözve), ablak a
  fordulókéval azonos, vége az első szint alatti vagy az első magasabb gyertya. 2024-2025: 84 923 érvényes jelölt
  (éjszaka 58 030, ebből 5,5% forduló; RTH 26 893, 6,6%); a fordulóablakok 4623 / 4667-e jelöltként is megvan,
  azonos ablakkal. Gyors teszt (felhasználói döntés): a ZigZag-jelöltek mind (adatuk a `pivots` futásból) és a nem
  ZigZag-jelöltek 10%-a (7905 jelölt, 6,4 M másodperc, súly 10); később teljes teszt minden jelöltre. A nem
  ZigZag-jelölten teljesülő minta önmagában nem cáfolja a mintát, a gazdasági próba (nettó R) dönt. Kiválasztott minták
  (`selected_patterns.csv`, csak 2024-ből): 70. A felhasználónak le kell futtatnia a `pivots`-ot a
  `candidate_events.csv`-re.
- **A gazdasági próba szabályai (2026-10-08 döntés, az eredmények előtt rögzítve):** High-nál short, Low-nál long.
  Belépés a minta utolsó eseményének másodpercében, középáron, legkorábban a jelölt megerősítésekor (az első
  alacsonyabb gyertya zárása, `confirm_minute`). Stop: a jelölt csúcsa + 2 tick, nem mozdul. Kockázati korlát: ha a
  kockázat > 2 x ATR1, nincs kötés. Cél: 1R, 2R és 3R külön. 60 perc vagy seansz vége után kilépés záróáron. Költség:
  stopnál 1 tick csúszás, 0,08 pont jutalék. Mellette MAE / MFE (R-ben és ATR1-ben), precizitás (tájékoztató),
  összevetés minden jelölttel minta nélkül. Mintánként, éjszaka / RTH, 2024 / 2025 külön (2025 az ellenőrzés), a nem
  ZigZag-jelöltek 10-es súllyal.
- **6. lépés eredménye (gyors teszt, `research/pattern_trades.py`, ResearchData `results/candidates/trades/`):**
  alap (minden jelölt a megerősítéskor, minta nélkül): éjszaka 2024 −0,03 / −0,16 / −0,12 R (1R / 2R / 3R cél), 2025
  −0,04 / −0,11 / −0,08; RTH 2024 +0,14 ±0,07 / +0,12 / +0,14, 2025 +0,04 ±0,07 / +0,02 / +0,05. A minták RTH-ban
  megduplázzák a pontosságot (ZigZag-forduló a kötések között 7,5% → 13-17%, és ez 2025-ben is megmarad: 10-15%),
  2024-ben +0,2…+0,37 R (1R), de **2025-ben szinte mind 0 körül vagy negatív**: egyik RTH minta sem megy át az
  ellenőrzésen. Éjszaka a minták 0 körül; néhány csak könyv/orderflow négyes 2025-ben +0,26…+0,34 R, de 2024-ben 0
  körül, kb. 100 kötéssel és ±0,25-0,5 sávval (70 minta x 3 cél: a véletlen is ad ilyet). Az ár-követő (hatékonyság,
  rejtett) éjszakai minták pontossága az alap alatt van. Összegzés: a sorrendminták a fordulók felé dúsítanak, de ez
  ezzel a belépés / stop / cél mechanikával nem ad igazolt nettó előnyt. A teljes teszt (minden jelölt) csak akkor
  érdemes, ha új ötlet ad rá okot.

## Nyitott kérdések, következő lépések

- Élő próba NinjaTrader stratégiaként, sim számlán, egy pozícióval, 2026 októberétől.
- Gond: a NinjaTrader Level 2 csak ~10 árszintet ad, a sáv 21–45 tick is lehet, a C# viszont teljes MBO könyvből
  számolt. Dönteni kell: mélyebb adatforrás, vagy a jellemzők átdefiniálása a látható szintekre (ez új szabály,
  2024–2025-ön újra kell ellenőrizni).
- A régi `book` mód (a rögzített szabály `added_vs_filled_1`, `defense_kept_1` jellemzői) a natív icebergeket
  hibásan könyveli: a függő fill csak a csökkenés erejéig fill, az iceberg-újratöltés betett méretnek számít. A
  szabály és a holdout ezzel a definícióval készült, utólag nem változtatható; az élő NinjaTrader-változatnak ugyanezt
  kell követnie, vagy új szabályként 2024–2025-ön újra kell ellenőrizni.
- Érzékenységvizsgálat az ATR-szorzókra 2024–2025-ön.
- NQ még nem volt vizsgálva.
