"""Candidate highs and lows as they appear live, with the same window as the swing windows (economic test).

    python candidate_windows.py <resultsDir> <outDir> [sample] [lastSession]

A candidate high is a 1 minute bar whose next bar with a different high is lower (an equal high waits for the first
lower one, the candidate is the first bar of the equal ones), and which is the highest bar since its window start:
    ATR1, level, entry, T0, band   as in pivot_windows.py (ATR(20) of the bar before, level = high - 2 x ATR1, the
                                   entry bar follows the last bar below the level, at most LOOKBACK bars back; the
                                   bars between the entry and the candidate are not higher than it)
    end                            the first bar after the candidate below the level (resolved), or the first bar
                                   above the candidate (invalidated, the window ends at its open: live a new
                                   candidate starts there)
A low is mirrored. pivot = the candidate is a ZigZag swing (0.3 or 0.5) of the same type on the same bar.
Excluded as in pivot_windows.py: no whole minute before the candidate, 16:00-20:00 ET, crossing a UTC day or the
17:00-18:00 break.

There are about 20 times more candidates than swings; the order flow data for all of them would be too large, so a
seeded uniform sample (default 10%) of the valid candidates that are not ZigZag swings goes to candidate_events.csv
for DatabentoExtract pivots; the swing candidates have the same windows as pivot_windows.py, their order flow is
already in the pivots run (later a full run of all candidates is planned).
confirm_minute: the close of the first lower bar after the candidate (live the candidate exists from then on).
Outputs: candidates.csv (all, with the sample flag), candidate_events.csv, summary.md.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

import pivot_windows as pw
import zigzag as zz

LOOKBACK = 240
SEED = 20261008


def candidates(bars: pd.DataFrame, multiple: float = 2.0) -> pd.DataFrame:
    bars = zz.prepare_bars(bars).sort_values("minute").reset_index(drop=True)
    high, low, open_ = bars.high.to_numpy(float), bars.low.to_numpy(float), bars.open.to_numpy(float)
    symbol = bars.instrument_id.to_numpy()
    minute = bars.minute.to_numpy()
    atr1 = pw.atr_ninja(high, low, bars.close.to_numpy(float), symbol, pw.ATR_PERIOD)
    swings = zz.swings(bars)
    swing_keys = set(zip(swings["index"], swings["type"]))
    n = len(bars)
    rows = []
    for kind, sign, x in (("high", 1, high), ("low", -1, -low)):
        # x: the high, or the negated low, so the low is a high of x
        for i in range(1, n - 1):
            if symbol[i] != symbol[i - 1] or x[i - 1] == x[i]:
                continue
            j = i + 1
            while j < n and x[j] == x[i] and symbol[j] == symbol[i]:
                j += 1
            if j >= n or symbol[j] != symbol[i] or x[j] > x[i]:
                continue
            level = x[i] - multiple * atr1[i - 1]
            k = i - 1
            while k >= 0 and i - k <= LOOKBACK and symbol[k] == symbol[i] and level <= x[k] <= x[i]:
                k -= 1
            if k < 0 or i - k > LOOKBACK or symbol[k] != symbol[i] or x[k] > x[i]:
                continue
            entry = k + 1
            if entry == i:
                continue
            e = i + 1
            while e < n and symbol[e] == symbol[i] and level <= x[e] <= x[i]:
                e += 1
            if e >= n or symbol[e] != symbol[i]:
                continue
            resolved = x[e] < level
            price = sign * x[i]
            half_band = sign * (price - open_[entry]) + pw.BAND_TICKS * pw.TICK
            rows.append({
                "window_id": f"c{kind[0]}{minute[i]}", "session": bars.session[i], "instrument_id": symbol[i],
                "type": kind, "price": price, "atr1": atr1[i - 1], "entry_minute": int(minute[entry]),
                "swing_minute": int(minute[i]),
                "end_minute": int(minute[e]) + (60 if resolved else 0),
                "confirm_minute": int(minute[j]) + 60,
                "resolved": resolved, "pivot": (i, kind) in swing_keys,
                "bars_before": i - entry, "bars_after": e - i,
                "part": pw.part_of_day(int(bars.minute_of_day[i])),
                "band_low": open_[entry] - half_band, "band_high": open_[entry] + half_band,
            })
    return pd.DataFrame(rows)


def main(results: str, out: str, sample: float = 0.1, last_session: str = "2025-12-31") -> None:
    bars = pd.read_csv(os.path.join(results, "bars_1min.csv.gz"))
    bars = bars[bars.session <= last_session]
    df = candidates(bars)
    et = lambda s: pd.to_datetime(s, unit="s", utc=True).dt.tz_convert(zz.ET)
    start = df.entry_minute - pw.WARM_UP
    df["crosses_day"] = start // 86400 != (df.end_minute - 1) // 86400
    session_of = lambda t: (t + pd.Timedelta(hours=6)).dt.date
    df["crosses_break"] = session_of(et(start)) != session_of(et(df.end_minute - 1))
    df["valid"] = (df.part != "none") & ~df.crosses_day & ~df.crosses_break & (df.end_minute > df.entry_minute)
    rng = np.random.default_rng(SEED)
    df["sample"] = df.valid & ~df["pivot"] & (rng.random(len(df)) < sample)
    df = df.sort_values(["entry_minute", "swing_minute", "window_id"]).reset_index(drop=True)

    os.makedirs(out, exist_ok=True)
    df.to_csv(os.path.join(out, "candidates.csv"), index=False)
    s = df[df["sample"]]
    pd.DataFrame({"window_id": s.window_id, "instrument_id": s.instrument_id, "start_second": s.entry_minute - pw.WARM_UP,
                  "end_second": s.end_minute, "band_low": s.band_low, "band_high": s.band_high}
                 ).to_csv(os.path.join(out, "candidate_events.csv"), index=False)

    v = df[df.valid]
    lines = [f"# Jelölt csúcsok és aljak (2 x ATR1, {last_session}-ig)", "",
             f"Összes jelölt: {len(df)}, érvényes: {len(v)}, minta (a nem ZigZag-jelöltek {sample:.0%}-a): {len(s)}, "
             f"a minta másodpercei: {int((s.end_minute - s.entry_minute + pw.WARM_UP).sum())}.", "",
             "| rész | érvényes | ebből ZigZag-forduló | lezárult (szint alá ment) | érvénytelenült (új szélsőérték) | minta |",
             "|---|---|---|---|---|---|"]
    for p, g in v.groupby("part"):
        lines.append(f"| {p} | {len(g)} | {int(g["pivot"].sum())} ({g["pivot"].mean():.1%}) | {int(g.resolved.sum())} | "
                     f"{int((~g.resolved).sum())} | {int(g['sample'].sum())} |")
    with open(os.path.join(out, "summary.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], float(sys.argv[3]) if len(sys.argv) > 3 else 0.1,
         sys.argv[4] if len(sys.argv) > 4 else "2025-12-31")
