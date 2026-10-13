"""Windows of the NinjaTrader order flow indicator (Custom Indicators/GM/FlowCurves.cs).

    python flow_windows.py <resultsDir> <researchDataDir>

The indicator draws the order flow curves of pivot_curves.py from the per second files of DatabentoExtract pivots,
window by window. This script writes
  results/blue_sweep/blue_events.csv  input of DatabentoExtract pivots for the blue sweep processes (window_id,
                                      instrument_id, start_second, end_second, band_low, band_high); the output goes to
                                      <researchDataDir>/blue (blue/pivots/<name>.csv.gz)
  results/flow/windows.csv            every window with order flow data: set (pivot / candidate / blue), flow (the
                                      directory of its per second files under researchDataDir), type (high / low),
                                      part, session, atr1, entry_second (T0), swing_second, end_second, large_limit
                                      (empty without a threshold), cls, zigzag, signal_seconds (blue: the entries)
  results/flow/norms.csv              per part of day: n_volume (N of the volume windows), n_eff (N of the
                                      efficiency), from the 2024 swing windows as in pattern_trades.norms

Sets:
  pivot      the ZigZag swing windows (results/pivots/windows.csv, valid ones), cls = pivot, zigzag 0.3 / 0.5
  candidate  the 10% sample of the candidate peaks (results/templates/windows.csv, ids ch / cl), cls = resolved /
             invalidated / pivot
  blue       the blue sweep processes of blue_sweep.py (2024-2025): from the profile start (14 bars before the touch)
             to the end of the process, written for a high (a low mirrored). The band is centred on the open of the
             first bar and reaches 20 ticks beyond the extreme of the whole process (as the swing windows: the
             extreme is known only afterwards). T0 = the profile start, swing_second = the first second of the touch
             bar at the level (the post curves start after it). The 30 s before T0 are added for the rolling
             curves; a window over a UTC midnight starts at the midnight (the pivots mode reads one UTC day per file).
"""
from __future__ import annotations

import glob
import os
import sys
import tempfile

import numpy as np
import pandas as pd

import blue_sweep as bs
import pattern_trades as pt
import pivot_windows as pw
import reverse_study as rs
import zigzag as zz

TICK = 0.25
BAND_TICKS = 20
WARM_UP = pw.WARM_UP


def blue_windows(results: str, data: str) -> pd.DataFrame:
    bs.TRACE = []
    with tempfile.TemporaryDirectory() as tmp:          # the trades are already in results/blue_sweep
        bs.main(results, data, tmp, "2024-01-01", "2025-12-31")
    trace = pd.DataFrame(bs.TRACE)
    bs.TRACE = None

    _, _, bars = rs.load(results)
    b = zz.prepare_bars(bars).sort_values("minute").reset_index(drop=True)
    high, low, close = b.high.to_numpy(float), b.low.to_numpy(float), b.close.to_numpy(float)
    atr1 = pw.atr_ninja(high, low, close, b.instrument_id.to_numpy(), pw.ATR_PERIOD)
    index = pd.Series(np.arange(len(b)), index=b.minute.to_numpy())

    rows = []
    for pid, g in trace.groupby("process", sort=True):
        first = g.iloc[0]
        s, t, e = index[first.start_minute], index[first.touch_minute], index[g.minute.iat[-1]]
        is_high = first.side == -1
        extreme = high[s:e + 1].max() if is_high else low[s:e + 1].min()
        center = b.open.iat[s]
        half = (extreme - center if is_high else center - extreme) + BAND_TICKS * TICK
        start = int(b.minute.iat[s])
        end = int(b.minute.iat[e]) + 60
        clipped = max(start - WARM_UP, (end - 1) // 86400 * 86400)
        signals = g.minute[g.signal].to_numpy() + 60          # the entry: the open of the bar after the signal
        rows.append({
            "window_id": f"b{'h' if is_high else 'l'}{int(first.touch_minute)}",
            "instrument_id": int(b.instrument_id.iat[t]),
            "start_second": clipped, "end_second": end,
            "band_low": center - half, "band_high": center + half,
            "set": "blue", "flow": "blue/pivots", "type": "high" if is_high else "low",
            "part": bs.part_of(int(b.minute_of_day.iat[t])), "session": str(b.session.iat[t].date()),
            "atr1": atr1[t - 1] if t > 0 else atr1[t], "entry_second": max(start, clipped + WARM_UP),
            "touch_minute": int(first.touch_minute), "level": first.level, "cls": f"process {pid}",
            "zigzag": "0.5", "signal_seconds": ";".join(str(int(x)) for x in signals),
        })
    df = pd.DataFrame(rows)
    df["swing_second"] = touch_seconds(df, data)
    return df


def touch_seconds(df: pd.DataFrame, data: str) -> np.ndarray:
    """The first second of the touch bar at the level (the 1 second bars), the bar open without second data."""
    out = df.touch_minute.to_numpy(float).copy()
    day = pd.to_datetime(df.touch_minute, unit="s", utc=True).dt.strftime("%Y%m%d")
    for d, idx in df.groupby(day).groups.items():
        files = glob.glob(os.path.join(data, "seconds", f"*{d}.csv.gz"))
        if not files:
            continue
        sec = pd.read_csv(files[0], usecols=["utc_second", "instrument_id", "high", "low"])
        for k in idx:
            r = df.loc[k]
            s = sec[(sec.instrument_id == r.instrument_id) & (sec.utc_second >= r.touch_minute)
                    & (sec.utc_second < r.touch_minute + 60)]
            hit = s[s.high >= r.level] if r.type == "high" else s[s.low <= r.level]
            if len(hit):
                out[k] = hit.utc_second.iloc[0]
    return out


def main(results: str, data: str) -> None:
    out_dir = os.path.join(results, "flow")
    os.makedirs(out_dir, exist_ok=True)

    blue = blue_windows(results, data)
    blue[["window_id", "instrument_id", "start_second", "end_second", "band_low", "band_high"]].to_csv(
        os.path.join(results, "blue_sweep", "blue_events.csv"), index=False)

    piv = pd.read_csv(os.path.join(results, "pivots", "windows.csv"))
    piv = piv[piv.valid]
    piv = pd.DataFrame({"window_id": piv.window_id, "set": "pivot", "flow": "pivots", "type": piv.type,
                        "part": piv.part, "session": piv.session, "atr1": piv.atr1, "entry_second": piv.entry_minute,
                        "swing_second": piv.swing_second, "end_second": piv.end_minute, "cls": "pivot",
                        "zigzag": np.where(piv.m05, "0.5", "0.3"), "signal_seconds": ""})

    cand = pd.read_csv(os.path.join(results, "templates", "windows.csv"))
    cand = cand[cand.window_id.str.startswith("c")]
    cand = pd.DataFrame({"window_id": cand.window_id, "set": "candidate", "flow": "candidates/pivots",
                         "type": cand.type, "part": cand.part, "session": cand.session, "atr1": cand.atr1,
                         "entry_second": cand.entry_minute, "swing_second": cand.swing_second,
                         "end_second": cand.end_minute, "cls": cand.cls, "zigzag": "", "signal_seconds": ""})

    columns = ["window_id", "set", "flow", "type", "part", "session", "atr1", "entry_second", "swing_second",
               "end_second", "cls", "zigzag", "signal_seconds"]
    windows = pd.concat([piv[columns], cand[columns], blue[columns]], ignore_index=True)

    limits = pd.read_csv(os.path.join(results, "pivots", "sequences", "large_limits.csv"))
    key = limits.set_index([limits.session.astype(str), "part"]).large_limit
    windows["large_limit"] = [key.get((str(s), p), np.nan) for s, p in zip(windows.session, windows.part)]
    windows = windows[windows.part.isin(["night", "rth"])]
    windows.to_csv(os.path.join(out_dir, "windows.csv"), index=False)

    n_volume, n_eff = pt.norms(results, os.path.join(data, "pivots"), limits)
    pd.DataFrame({"part": n_volume.index, "n_volume": n_volume.to_numpy(), "n_eff": n_eff[n_volume.index].to_numpy()}
                 ).to_csv(os.path.join(out_dir, "norms.csv"), index=False)

    print(windows.groupby(["set", "part"]).size().to_string())
    minutes = (blue.end_second - blue.start_second) / 60
    print(f"blue windows {len(blue)}, minutes total {minutes.sum():.0f}, median {minutes.median():.0f}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
