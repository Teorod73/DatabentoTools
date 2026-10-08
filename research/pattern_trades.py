"""Economic test of the selected event patterns on the live candidates (step 6).

    python pattern_trades.py <resultsDir> <researchDataDir> <outDir>

resultsDir: ResearchData results (pivots/, candidates/), researchDataDir: the ResearchData clone (pivots/ and
candidates/pivots/ order flow, seconds/ bars). The candidates are the ZigZag swing candidates (all, their order flow
from the swing windows) and a 10% sample of the other candidates (weight 10).

For every candidate the curves and the events are computed as in pivot_curves.py / pivot_sequences.py, with the
event thresholds of the swing windows of the previous 60 days (sequences/thresholds.csv), the large series threshold
of the day (sequences/large_limits.csv) and the volume normalization of the 2024 swing windows. A selected pattern
(candidates/selected_patterns.csv, chosen on 2024 only) holds when its events happen in this order (first
occurrences); it completes at the second of its last event.

Trade (rules fixed before the results, CLAUDE.md): short at a high, long at a low. Entry at the mid at the end of the
completion second, at the earliest at the close of the first lower bar (confirm_minute), and before the window ends
(after its end the candidate is either invalid or already resolved). Stop: the candidate extreme + 2 ticks, fixed.
No trade when the risk is above 2 x ATR1. Targets 1R, 2R, 3R separately; exit at the close after 60 minutes or at the
session end (17:00 ET). Stop and target in the same second count as the stop. Costs: 1 tick slip at the stop, 0.08
points commission. Also MFE / MAE in R over the trade, the precision (weighted share of swings among the trades) and
the same trade on every candidate at its confirmation (no pattern) as the baseline.

Means are weighted (sample weight), the 95% band clusters by session. Outputs: trades.csv.gz, summary.md.
"""
from __future__ import annotations

import glob
import os
import sys

import numpy as np
import pandas as pd

import pivot_curves as pc
import pivot_sequences as ps

TICK = 0.25
STOP_TICKS = 2
MAX_RISK_ATR = 2.0
TARGETS = (1, 2, 3)
HORIZON = 3600
COMMISSION = 0.08
SAMPLE_WEIGHT = 10.0
YEARS = (2024, 2025)


def candidate_windows(results: str) -> pd.DataFrame:
    """The tested candidates in the windows.csv layout of pivot_curves.load: the swing candidates under their swing
    window id (their order flow is in the swing run), the sample under its own id."""
    c = pd.read_csv(os.path.join(results, "candidates", "candidates.csv"))
    c = c[c.valid & (c["pivot"] | c["sample"])].copy()
    c["window_id"] = np.where(c["pivot"], c.window_id.str[1:], c.window_id)
    pivots = pd.read_csv(os.path.join(results, "pivots", "windows.csv"))
    have = set(pivots.loc[pivots.valid, "window_id"])
    c = c[~c["pivot"] | c.window_id.isin(have)]
    c["weight"] = np.where(c["pivot"], 1.0, SAMPLE_WEIGHT)
    return c


def norms(results: str, flow: str, limits: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """The volume window and the efficiency N of the 2024 swing windows (as in pivot_sequences.py)."""
    windows, df = pc.load(os.path.join(results, "pivots"), flow, 2024, limits)
    cur = pc.curves(pc.mirrored(df, windows), windows)
    return cur.attrs["norms"]


def events_of(windows: pd.DataFrame, cur: pd.DataFrame, mid: np.ndarray, th: pd.DataFrame) -> pd.DataFrame:
    by_day = {key: g.set_index("curve") for key, g in th.groupby(["session", "part"])}
    rows = []
    for wid, idx in cur.groupby("window_id", sort=False).indices.items():
        w = windows.loc[wid]
        idx = idx[cur.second.to_numpy()[idx] >= w.entry_minute]
        key = (str(w.session), w.part)
        if len(idx) == 0 or key not in by_day:
            continue
        for name, i in ps.window_events(cur.iloc[idx], mid[idx], by_day[key]).items():
            rows.append({"window_id": wid, "event": name, "second": int(cur.second.iat[idx[i]])})
    return pd.DataFrame(rows)


def completion(ev: dict[str, int], pattern: list[str]) -> int | None:
    """The second of the last event when the events happen in this order, otherwise None."""
    last = -1
    for e in pattern:
        t = ev.get(e)
        if t is None or t <= last:
            return None
        last = t
    return last


class Seconds:
    """1 second bars of the extract (seconds/<day>.csv.gz), loaded per UTC day on demand."""

    def __init__(self, directory: str):
        self.files = {os.path.basename(f)[-15:-7]: f for f in glob.glob(os.path.join(directory, "seconds", "*.csv.gz"))}
        self.cache: dict[str, pd.DataFrame] = {}

    def bars(self, instrument: int, start: int, end: int) -> pd.DataFrame:
        days = pd.date_range(pd.to_datetime(start, unit="s").normalize(), pd.to_datetime(end, unit="s"), freq="D")
        parts = []
        for d in days.strftime("%Y%m%d"):
            if d not in self.files:
                continue
            if d not in self.cache:
                if len(self.cache) > 4:
                    self.cache.pop(next(iter(self.cache)))
                self.cache[d] = pd.read_csv(self.files[d], usecols=["utc_second", "instrument_id", "high", "low",
                                                                    "close"])
            df = self.cache[d]
            parts.append(df[(df.instrument_id == instrument) & (df.utc_second >= start) & (df.utc_second < end)])
        return pd.concat(parts) if parts else pd.DataFrame(columns=["utc_second", "high", "low", "close"])


def simulate(bars: pd.DataFrame, side: int, entry: float, stop: float) -> dict:
    """side +1 long, -1 short. Net R for every target, MFE / MAE in R."""
    risk = abs(entry - stop)
    fav = (bars.high.to_numpy() - entry) * side if side == 1 else (entry - bars.low.to_numpy())
    adv = (entry - bars.low.to_numpy()) if side == 1 else (bars.high.to_numpy() - entry)
    stop_hit = adv >= risk
    first_stop = int(np.argmax(stop_hit)) if stop_hit.any() else len(bars)
    out = {"mfe_r": float(fav[:first_stop + 1].max() / risk) if len(bars) else 0.0,
           "mae_r": float(min(adv[:first_stop + 1].max(), risk) / risk) if len(bars) else 0.0}
    cost = COMMISSION / risk
    for k in TARGETS:
        hit = fav >= k * risk
        first_target = int(np.argmax(hit)) if hit.any() else len(bars)
        if first_stop <= first_target and first_stop < len(bars):
            r = -1 - TICK / risk - cost
        elif first_target < len(bars):
            r = k - cost
        elif len(bars):
            r = (bars.close.to_numpy()[-1] - entry) * side / risk - cost
        else:
            r = np.nan
        out[f"r{k}"] = r
    return out


def trade(c: pd.Series, entry_second: int, mid: float, seconds: Seconds) -> dict | None:
    side = 1 if c.type == "low" else -1
    stop = c.price - side * STOP_TICKS * TICK
    risk = (mid - stop) * side
    if not (risk > 0) or risk > MAX_RISK_ATR * c.atr1:
        return None
    session_end = int(pd.Timestamp(f"{c.session} 17:00", tz="America/New_York").timestamp())
    bars = seconds.bars(int(c.instrument_id), entry_second + 1, min(entry_second + 1 + HORIZON, session_end))
    return {"entry_second": entry_second, "entry": mid, "risk": risk, "risk_atr": risk / c.atr1,
            **simulate(bars.sort_values("utc_second"), side, mid, stop)}


def weighted(g: pd.DataFrame, column: str) -> tuple[float, float]:
    """Weighted mean and the 95% half band clustered by session."""
    g = g[g[column].notna()]
    w, x = g.weight.to_numpy(), g[column].to_numpy()
    if w.sum() == 0:
        return np.nan, np.nan
    mean = (w * x).sum() / w.sum()
    by = pd.Series(w * (x - mean)).groupby(g.session.to_numpy()).sum()
    return mean, 1.96 * np.sqrt((by ** 2).sum()) / w.sum()


def main(results: str, data: str, out_dir: str) -> None:
    seq = os.path.join(results, "pivots", "sequences")
    limits = pd.read_csv(os.path.join(seq, "large_limits.csv"))
    th = pd.read_csv(os.path.join(seq, "thresholds.csv"))
    th["session"] = th.session.astype(str)
    patterns = pd.read_csv(os.path.join(results, "candidates", "selected_patterns.csv"))
    n_total, n_att = norms(results, os.path.join(data, "pivots"), limits)

    cands = candidate_windows(results)
    os.makedirs(out_dir, exist_ok=True)
    cands.assign(valid=True).to_csv(os.path.join(out_dir, "windows.csv"), index=False)
    frames = []
    for flow in (os.path.join(data, "pivots"), os.path.join(data, "candidates", "pivots")):
        windows, df = pc.load(out_dir, flow, list(YEARS), limits)
        frames.append(df)
    df = pd.concat(frames, ignore_index=True)
    m = pc.mirrored(df, windows).sort_values(["window_id", "second"]).reset_index(drop=True)
    cur = pc.curves(m, windows, norms=(n_total, n_att))
    mid_att = m.mid_att.to_numpy()
    ev = events_of(windows, cur, mid_att, th)
    ev_by = {w: dict(zip(g.event, g.second)) for w, g in ev.groupby("window_id")}
    # the plain mid at the end of every second (mid_att is negated at the highs)
    mid = np.where(m.window_id.map(windows.type).eq("high"), mid_att, -mid_att)
    mid_at = pd.Series(mid, index=pd.MultiIndex.from_arrays([m.window_id, m.second]))

    seconds = Seconds(data)
    rows = []
    with_thresholds = set(zip(th.session, th.part))
    for wid in windows.sort_values("entry_minute").index:
        c = windows.loc[wid]
        if (str(c.session), c.part) not in with_thresholds:
            continue
        confirm, end = int(c.confirm_minute), int(c.end_minute)
        base = {"window_id": wid, "session": c.session, "year": pd.Timestamp(c.session).year, "part": c.part,
                "type": c.type, "pivot": bool(c["pivot"]), "weight": c.weight}
        tries = [("(minden jelölt)", confirm)]
        evw = ev_by.get(wid, {})
        for p in patterns[patterns.part == c.part].pattern:
            done = completion(evw, p.split(" > "))
            if done is not None:
                tries.append((p, max(done, confirm)))
        for name, entry_second in tries:
            if entry_second >= end or (wid, entry_second) not in mid_at.index:
                continue
            t = trade(c, entry_second, float(mid_at[(wid, entry_second)]), seconds)
            if t is not None:
                rows.append({**base, "pattern": name, **t})
    trades = pd.DataFrame(rows)
    trades.to_csv(os.path.join(out_dir, "trades.csv.gz"), index=False)
    with open(os.path.join(out_dir, "summary.md"), "w", encoding="utf-8") as f:
        f.write(summary(trades, patterns))


def summary(t: pd.DataFrame, patterns: pd.DataFrame) -> str:
    lines = ["# Gazdasági próba: a kiválasztott minták a jelölteken", "",
             "Short a csúcson, long az aljon; belépés a minta teljesülésekor, legkorábban a jelölt megerősítésekor; "
             "stop a szélsőérték + 2 tick; kockázat legfeljebb 2 x ATR1; 1R / 2R / 3R cél; 60 perc. Nettó R kötésenként "
             "(súlyozva: a nem ZigZag-jelöltek 10-es súllyal), ± 95% seanszonként klaszterezve. pontosság = a "
             "kötések (súlyozott) hány %-a ZigZag-forduló. A minták csak 2024-ből választva, 2025 az ellenőrzés.", ""]
    for part in ("night", "rth"):
        lines += [f"## {'Éjszaka' if part == 'night' else 'RTH'}", "",
                  "| minta | év | kötés | súlyozott | pontosság | 1R | 2R | 3R | MFE (R, medián) | kockázat (ATR1) |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
        g = t[t.part == part]
        order = ["(minden jelölt)"] + list(patterns[patterns.part == part].pattern)
        for p in order:
            for y in YEARS:
                x = g[(g.pattern == p) & (g.year == y)]
                if x.empty:
                    continue
                prec = (x.weight * x["pivot"]).sum() / x.weight.sum() * 100
                cells = []
                for k in TARGETS:
                    mean, band = weighted(x, f"r{k}")
                    cells.append(f"{mean:+.3f} ±{band:.3f}")
                lines.append(f"| {p} | {y} | {len(x)} | {x.weight.sum():.0f} | {prec:.1f}% | " + " | ".join(cells) +
                             f" | {x.mfe_r.median():.2f} | {x.risk_atr.median():.2f} |")
        lines.append("")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3])
