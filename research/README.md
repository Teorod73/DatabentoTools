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
