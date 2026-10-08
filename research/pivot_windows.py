"""Study windows around the ZigZag swings (TimeOfDay, 0.3 and 0.5) for the order flow sequence study.

    python pivot_windows.py <resultsDir> <extractDir> <outDir> [atrMultiple] [lastSession]

For a high (a low is mirrored):
    ATR1     ATR(20) of the 1 minute front contract bars (NinjaTrader smoothing) at the close of the bar before the
             high bar, so it is known before the high and does not depend on the window.
    level    high - atrMultiple x ATR1 (default 2).
    entry    going back from the high bar, the bar after the last bar whose high is below the level. T0 = its open
             time (= the close of the last bar below the level). The search stops at the previous swing of the same
             ZigZag level; when no bar is below the level there, the window has no start (no_start).
    end      the first bar after the high bar whose high is below the level, at the latest the next swing of the
             same ZigZag level (a bar that is both the swing and the reversal can leave the price at the level, then
             there is no end: no_end). Tend = its close time.
    Tx       the first second of the high bar whose high equals the swing price (from the 1 second bars).
    k        (high - entry open + 20 ticks) / ATR1: the multiple of ATR1 that the book band above the entry open must
             reach so that it is at least 20 ticks above the high. The band multiple N is chosen from its
             distribution (N >= every k).

A swing found by both ZigZag levels is one window (flags m03, m05). The part of the day is decided by Tx:
night 20:00-09:30, rth 09:30-16:00, the rest (16:00-20:00) is not studied. Only the development period is used
(sessions up to lastSession, default 2025-12-31).

Outputs: windows.csv (one row per swing) and summary.md.
"""
from __future__ import annotations

import glob
import os
import sys

import numpy as np
import pandas as pd

import zigzag as zz

TICK = 0.25
ATR_PERIOD = 20
BAND_TICKS = 20
NIGHT_START, RTH_START, RTH_END = 20 * 60, 9 * 60 + 30, 16 * 60


def atr_ninja(high: np.ndarray, low: np.ndarray, close: np.ndarray, symbol: np.ndarray, period: int) -> np.ndarray:
    """NinjaTrader ATR: simple average of the true range for the first bars, then (prev x (n-1) + tr) / n.
    Restarts at a contract change (the bars are the front contract)."""
    out = np.empty(len(high))
    count = 0
    value = 0.0
    for i in range(len(high)):
        new_series = i == 0 or symbol[i] != symbol[i - 1]
        if new_series:
            count = 0
            tr = high[i] - low[i]
        else:
            tr = max(high[i], close[i - 1]) - min(low[i], close[i - 1])
        count += 1
        value = value + (tr - value) / count if count < period else (value * (period - 1) + tr) / period
        out[i] = value
    return out


def part_of_day(minute_of_day: int) -> str:
    if RTH_START <= minute_of_day < RTH_END:
        return "rth"
    if minute_of_day >= NIGHT_START or minute_of_day < RTH_START:
        return "night"
    return "none"


def build_windows(bars: pd.DataFrame, multiple: float) -> pd.DataFrame:
    bars = zz.prepare_bars(bars).sort_values("minute").reset_index(drop=True)
    swings = zz.swings(bars)
    high, low, open_ = bars.high.to_numpy(float), bars.low.to_numpy(float), bars.open.to_numpy(float)
    symbol = bars.instrument_id.to_numpy()
    session = bars.session.to_numpy()
    atr1 = atr_ninja(high, low, bars.close.to_numpy(float), symbol, ATR_PERIOD)

    # one window per swing bar and type, flags for the ZigZag levels that found it
    swings["flag"] = np.where(swings.multiplier == 0.3, "m03", "m05")
    by_level = swings.groupby("multiplier")
    swings["next_index"] = by_level["index"].shift(-1).where(by_level.prev_index.shift(-1) == swings["index"])
    grouped = (swings.groupby(["index", "type"])
               .agg(price=("price", "first"), prev_index=("prev_index", "max"), next_index=("next_index", "min"),
                    flags=("flag", lambda f: set(f)))
               .reset_index())

    rows = []
    n = len(bars)
    for r in grouped.itertuples():
        i, sign = r.index, (1 if r.type == "high" else -1)
        if i < 1 or symbol[i - 1] != symbol[i]:
            continue
        a = atr1[i - 1]
        level = r.price - sign * multiple * a
        below = (lambda j: high[j] < level) if sign == 1 else (lambda j: low[j] > level)

        # entry: the bar after the last bar beyond the level, not before the previous swing
        j = i - 1
        stop = max(r.prev_index, 0)
        while j >= stop and not below(j) and symbol[j] == symbol[i]:
            j -= 1
        no_start = j < stop or symbol[j] != symbol[i] or not below(j)
        entry = j + 1

        # end: the first bar after the swing beyond the level, in the same contract, not after the next swing
        e = i + 1
        last = int(r.next_index) if not np.isnan(r.next_index) else n - 1
        while e <= last and symbol[e] == symbol[i] and not below(e):
            e += 1
        no_end = e > last or symbol[e] != symbol[i]

        k = (sign * (r.price - open_[entry]) + BAND_TICKS * TICK) / a if a > 0 else np.nan
        rows.append({
            "session": session[i], "instrument_id": symbol[i], "type": r.type, "price": r.price,
            "m03": "m03" in r.flags, "m05": "m05" in r.flags,
            "atr1": a, "level": level,
            "entry_minute": int(bars.minute[entry]), "swing_minute": int(bars.minute[i]),
            "end_minute": int(bars.minute[min(e, n - 1)]) + 60,
            "entry_open": open_[entry], "bars_before": i - entry, "bars_after": e - i,
            "no_start": no_start, "no_end": no_end,
            "entry_session": session[entry],
            "part": part_of_day(int(bars.minute_of_day[i])),
            "entry_part": part_of_day(int(bars.minute_of_day[entry])),
            "move_atr": sign * (r.price - open_[entry]) / a if a > 0 else np.nan,
            "k": k,
        })
    return pd.DataFrame(rows)


def add_swing_second(df: pd.DataFrame, extract: str) -> pd.DataFrame:
    """Tx: the first second of the swing bar at the swing price (the 1 second bars of the swing contract)."""
    tx = np.full(len(df), np.nan)
    df = df.reset_index(drop=True)
    day = pd.to_datetime(df.swing_minute, unit="s", utc=True).dt.strftime("%Y%m%d")
    for d, idx in df.groupby(day).groups.items():
        files = glob.glob(os.path.join(extract, "seconds", f"*{d}.csv.gz"))
        if not files:
            continue
        sec = pd.read_csv(files[0], usecols=["utc_second", "instrument_id", "high", "low"])
        for k in idx:
            r = df.loc[k]
            s = sec[(sec.instrument_id == r.instrument_id) & (sec.utc_second >= r.swing_minute)
                    & (sec.utc_second < r.swing_minute + 60)]
            hit = s[s.high >= r.price] if r.type == "high" else s[s.low <= r.price]
            if len(hit):
                tx[k] = hit.utc_second.iloc[0]
    df["swing_second"] = tx
    return df


def quantiles(x: pd.Series) -> str:
    q = x.quantile([0.5, 0.9, 0.95, 0.99])
    return f"{len(x)} | {q[0.5]:.2f} | {q[0.9]:.2f} | {q[0.95]:.2f} | {q[0.99]:.2f} | {x.max():.2f}"


def summary(df: pd.DataFrame, multiple: float, last_session: str) -> str:
    lines = [f"# ZigZag forduló ablakok ({multiple:g} x ATR1, fejlesztési időszak {last_session}-ig)", ""]
    lines += ["Minden sor egy forduló (a 0,3 és 0,5 közös fordulója egyszer számít).", ""]
    lines += ["## Esetszám", "", "| év | rész | típus | összes | csak 0,3 | 0,5 is | nincs kezdet | nincs vég |",
              "|---|---|---|---|---|---|---|---|"]
    df = df.assign(year=pd.to_datetime(df.session).dt.year)
    for (y, p, t), g in df.groupby(["year", "part", "type"]):
        lines.append(f"| {y} | {p} | {t} | {len(g)} | {(g.m03 & ~g.m05).sum()} | {g.m05.sum()} | "
                     f"{g.no_start.sum()} | {g.no_end.sum()} |")
    ok = df[(df.part != "none") & ~df.no_start & ~df.no_end]
    lines += ["", f"Vizsgálható (night / rth, van kezdet és vég): {len(ok)}. "
              f"Ebből az ablak 09:30 előtt kezdődik, de a forduló 09:30 után van: "
              f"{((ok.part == 'rth') & (ok.entry_part == 'night')).sum()}.", ""]

    lines += ["## Az ablak hossza (perc)", "", "| rész | n | medián előtte | q90 előtte | medián utána | q90 utána |",
              "|---|---|---|---|---|---|"]
    for p, g in ok.groupby("part"):
        lines.append(f"| {p} | {len(g)} | {g.bars_before.median():.0f} | {g.bars_before.quantile(0.9):.0f} | "
                     f"{g.bars_after.median():.0f} | {g.bars_after.quantile(0.9):.0f} |")

    lines += ["", "## k = (High - belépő nyitó + 20 tick) / ATR1", "",
              "A könyvsáv szorzója (N) legalább ekkora kell legyen, hogy a sáv minden esetben 20 tickkel a High fölé "
              "(Low alá) érjen.", "",
              "| csoport | n | medián | q90 | q95 | q99 | max |", "|---|---|---|---|---|---|---|"]
    for name, g in [("összes", ok)] + [(p, g) for p, g in ok.groupby("part")] + \
                   [(f"{p} {t}", g) for (p, t), g in ok.groupby(["part", "type"])]:
        lines.append(f"| {name} | {quantiles(g.k)} |")
    lines += ["", "A legnagyobb k értékek:", "", "| session | rész | típus | k | ATR1 | elmozdulás (ATR1) | perc előtte |",
              "|---|---|---|---|---|---|---|"]
    for r in ok.nlargest(10, "k").itertuples():
        lines.append(f"| {r.session} | {r.part} | {r.type} | {r.k:.2f} | {r.atr1:.2f} | {r.move_atr:.2f} | "
                     f"{r.bars_before} |")
    lines += ["", "ATR1 (pont):", "", "| rész | medián | q10 | q90 |", "|---|---|---|---|"]
    for p, g in ok.groupby("part"):
        lines.append(f"| {p} | {g.atr1.median():.2f} | {g.atr1.quantile(0.1):.2f} | {g.atr1.quantile(0.9):.2f} |")
    lines.append(f"\nTx (másodperc) megtalálva: {ok.swing_second.notna().sum()} / {len(ok)}.")
    return "\n".join(lines) + "\n"


def main(results: str, extract: str, out: str, multiple: float = 2.0, last_session: str = "2025-12-31") -> None:
    bars = pd.read_csv(os.path.join(results, "bars_1min.csv.gz"))
    bars = bars[bars.session <= last_session]
    df = build_windows(bars, multiple)
    df = add_swing_second(df, extract)
    os.makedirs(out, exist_ok=True)
    df.to_csv(os.path.join(out, "windows.csv"), index=False)
    with open(os.path.join(out, "summary.md"), "w", encoding="utf-8") as f:
        f.write(summary(df, multiple, last_session))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3],
         float(sys.argv[4]) if len(sys.argv) > 4 else 2.0,
         sys.argv[5] if len(sys.argv) > 5 else "2025-12-31")
