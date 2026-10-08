"""Event catalog of the order flow curves and the event sequence of every swing window (step 4).

    python pivot_sequences.py <pivotsDir> <flowDir> <outDir>

Curves of pivot_curves.py (mirrored to the swing side, the high and the low together), per part of day. The
thresholds come from the 2024 windows only (the search year); the events of 2025 use the same thresholds.

Events, scanned from T0 to the end of the window, each with its first second only:
    <curve>_hi / _lo     the curve is at least its q95 / at most its q5 for 15 seconds (the columns are called q80 /
                         q20 in the code and in thresholds.csv: the high and the low threshold)
    delta_neg, balance_neg, balance_pos   the curve is below / above 0 for 15 seconds
    <curve>_up / _down   CUSUM alarm of the slope: z = (c(t) - c(t - 5)) / sd, S+ = max(0, S+ + z - K), alarm when
                         S+ > H = 10 (and the same downwards); sd of the 5 s slope per curve and part of day
    <curve>_peak         (delta, att_eff, att_large, volume) the curve has been at least q80 and then falls by
                         half of q80 - q20 below its maximum since T0 for 15 seconds
    cross_eff, cross_large, cross_refill, cross_cancel   the defender curve is above the attacker curve for 5 s
                         for 15 s (efficiency, large series share, refill), and the attacking side cancels more
                         than the defending side
    div_delta, div_att_eff, div_att_large, div_volume   divergence: the mid makes a new extreme of the window at
                         least 15 s after the previous one and the curve is lower than at the previous one by at
                         least a tenth of q80 - q20
A condition that holds already at T0 counts only after it has been false once (an event is reaching a state, not
starting in it).

Outputs: thresholds.csv, events.csv.gz (window, event, second, normalized time), catalog.md (how often an event
happens, when, how often before the swing).

The first version (q20 / q80, 5 s, H = 5) made almost every event in 90-100% of the windows right after T0, so their
order said nothing; these stricter values were chosen on the event frequencies of 2024 only, not on any outcome.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

import pivot_curves as pc

HOLD = 15
Q_LOW, Q_HIGH = 0.05, 0.95
SLOPE = 5
K, H = 0.5, 10.0
PEAK_CURVES = ["delta", "att_eff", "att_large", "volume"]
SIGNED = {"delta": ("neg",), "balance": ("neg", "pos")}
CROSSES = {"cross_eff": ("def_eff", "att_eff"), "cross_large": ("def_large", "att_large"),
           "cross_refill": ("def_refill", "att_refill"), "cross_cancel": ("att_cancel", "def_cancel")}
DIVERGENCES = ["delta", "att_eff", "att_large", "volume"]
SEARCH_YEAR = 2024


def first_hold(cond: np.ndarray) -> int:
    """Index of the 5th second of the first run of HOLD true seconds that starts after a false second; -1 if none."""
    armed = False
    run = 0
    for i, c in enumerate(cond):
        if not c:
            armed = True
            run = 0
            continue
        if armed:
            run += 1
            if run >= HOLD:
                return i
    return -1


def first_cusum(z: np.ndarray) -> tuple[int, int]:
    """First alarm upwards and downwards of the two-sided CUSUM of z (NaN counts as 0)."""
    up = down = -1
    sp = sn = 0.0
    for i, v in enumerate(np.nan_to_num(z)):
        sp = max(0.0, sp + v - K)
        sn = max(0.0, sn - v - K)
        if up < 0 and sp > H:
            up = i
        if down < 0 and sn > H:
            down = i
        if up >= 0 and down >= 0:
            break
    return up, down


def first_peak(c: np.ndarray, q80: float, drop: float) -> int:
    top = -np.inf
    cond = np.zeros(len(c), bool)
    reached = False
    for i, v in enumerate(c):
        if np.isnan(v):
            continue
        reached |= v >= q80
        top = max(top, v)
        cond[i] = reached and v <= top - drop
    # the drop is a state reached after the peak, it is not armed by a false second before
    run = 0
    for i, ok in enumerate(cond):
        run = run + 1 if ok else 0
        if run >= HOLD:
            return i
    return -1


def first_divergence(mid: np.ndarray, c: np.ndarray, tolerance: float) -> int:
    best = -np.inf
    last_i, last_c = -1, np.nan
    for i, (m, v) in enumerate(zip(mid, c)):
        if np.isnan(m) or m <= best:
            continue
        best = m
        if last_i >= 0 and i - last_i >= HOLD and not np.isnan(v) and not np.isnan(last_c) and v < last_c - tolerance:
            return i
        if last_i < 0 or i - last_i >= HOLD:
            last_i, last_c = i, v
    return -1


def thresholds(cur: pd.DataFrame, part: pd.Series, year: pd.Series) -> pd.DataFrame:
    base = cur[year == SEARCH_YEAR]
    rows = []
    for p, g in base.groupby(part[year == SEARCH_YEAR]):
        for c in pc.CURVES:
            slope = g.groupby("window_id")[c].diff(SLOPE)
            rows.append({"part": p, "curve": c, "q20": g[c].quantile(Q_LOW), "q80": g[c].quantile(Q_HIGH),
                         "slope_sd": slope.std()})
    return pd.DataFrame(rows).set_index(["part", "curve"])


def window_events(c: pd.DataFrame, mid: np.ndarray, th: pd.DataFrame, part: str) -> dict[str, int]:
    ev = {}

    def add(name: str, i: int):
        if i >= 0:
            ev[name] = i

    for curve in pc.CURVES:
        x = c[curve].to_numpy(float)
        q20, q80, sd = th.loc[(part, curve), ["q20", "q80", "slope_sd"]]
        add(f"{curve}_hi", first_hold(x >= q80))
        add(f"{curve}_lo", first_hold(x <= q20))
        for sign in SIGNED.get(curve, ()):
            add(f"{curve}_{sign}", first_hold(x < 0 if sign == "neg" else x > 0))
        slope = np.full(len(x), np.nan)
        slope[SLOPE:] = x[SLOPE:] - x[:-SLOPE]
        up, down = first_cusum(slope / sd if sd > 0 else slope * 0)
        add(f"{curve}_up", up)
        add(f"{curve}_down", down)
        if curve in PEAK_CURVES:
            add(f"{curve}_peak", first_peak(x, q80, (q80 - q20) / 2))
        if curve in DIVERGENCES:
            add(f"div_{curve}", first_divergence(mid, x, (q80 - q20) / 10))
    for name, (a, b) in CROSSES.items():
        add(name, first_hold(c[a].to_numpy(float) > c[b].to_numpy(float)))
    return ev


def main(pivots: str, flow: str, out_dir: str) -> None:
    windows, df = pc.load(pivots, flow, [2024, 2025])
    m = pc.mirrored(df, windows).sort_values(["window_id", "second"]).reset_index(drop=True)
    search = (pd.to_datetime(m.window_id.map(windows.session)).dt.year == SEARCH_YEAR).to_numpy()
    cur = pc.curves(m, windows, search)
    u = pc.normalized_time(cur, windows)
    part = cur.window_id.map(windows.part)
    year = pd.to_datetime(cur.window_id.map(windows.session)).dt.year
    th = thresholds(cur, part, year)

    rows = []
    t0 = windows.entry_minute
    for wid, idx in cur.groupby("window_id", sort=False).indices.items():
        seconds = cur.second.to_numpy()[idx]
        keep = seconds >= t0[wid]
        idx = idx[keep]
        if len(idx) == 0:
            continue
        ev = window_events(cur.iloc[idx], m.mid_att.to_numpy()[idx], th, windows.part[wid])
        for name, i in ev.items():
            rows.append({"window_id": wid, "event": name, "second": int(cur.second.iat[idx[i]]),
                         "u": round(float(u.iat[idx[i]]), 4)})
    events = pd.DataFrame(rows)
    w = windows.loc[events.window_id]
    events["part"] = w.part.to_numpy()
    events["type"] = w.type.to_numpy()
    events["year"] = pd.to_datetime(w.session).dt.year.to_numpy()

    os.makedirs(out_dir, exist_ok=True)
    th.to_csv(os.path.join(out_dir, "thresholds.csv"))
    events.to_csv(os.path.join(out_dir, "events.csv.gz"), index=False)
    with open(os.path.join(out_dir, "catalog.md"), "w", encoding="utf-8") as f:
        f.write(catalog(events, windows))


def catalog(events: pd.DataFrame, windows: pd.DataFrame) -> str:
    n = windows.assign(year=pd.to_datetime(windows.session).dt.year).groupby(["year", "part"]).size()
    lines = ["# Eseménykatalógus (küszöbök 2024-ből)", "",
             "Ablakok: " + ", ".join(f"{y} {p}: {c}" for (y, p), c in n.items()), "",
             "gyak. = az ablakok hány %-ában történik meg; u = a medián normalizált idő (T0 = -1, csúcs = 0, vég = +1);",
             "előtte = hány %-ban a csúcs előtt. A 2025-ös gyakoriság az ellenőrzés: nagy eltérés instabil eseményt jelez.", ""]
    for p in ("night", "rth"):
        lines += [f"## {'Éjszaka' if p == 'night' else 'RTH'}", "",
                  "| esemény | gyak. 2024 | u 2024 | előtte 2024 | gyak. 2025 | u 2025 |", "|---|---|---|---|---|---|"]
        e = events[events.part == p]
        stats = {}
        for y in (2024, 2025):
            g = e[e.year == y].groupby("event")
            stats[y] = pd.DataFrame({"freq": g.size() / n[(y, p)] * 100, "u": g.u.median(),
                                     "before": g.u.apply(lambda s: (s < 0).mean() * 100)})
        names = stats[2024].sort_values("u").index
        for name in names:
            a = stats[2024].loc[name]
            b = stats[2025].loc[name] if name in stats[2025].index else pd.Series({"freq": 0, "u": np.nan})
            lines.append(f"| {name} | {a.freq:.0f} | {a.u:.2f} | {a.before:.0f} | {b.freq:.0f} | {b.u:.2f} |")
        lines.append("")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3])
