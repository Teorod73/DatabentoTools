"""Reverse study: how many of the ZigZag swings are at a zone, compared to the same zones shifted randomly.

    python reverse_study.py <resultsDir> <extractDir> <outDir> [lastSession]

Inputs from build_sessions.py: sessions.csv, levels.csv, bars_1min.csv.gz. The swings are calculated here.
Only the development period is used (sessions up to lastSession, default 2025-12-31), 2026 stays locked.

A swing is at a zone when its extreme is in [zone low - d, zone high + d], d = tolerance x daily ATR.
Every zone is known before the swing: previous sessions, the overnight range after 09:30, the initial balance after
10:30, the session VWAP of the previous minute. The baseline shifts every zone of the session by a random
+-(0.1..0.5) ATR and counts again, lift = hit rate / shifted hit rate.
"""
from __future__ import annotations

import glob
import os
import sys

import numpy as np
import pandas as pd

import zigzag as zz

ET = zz.ET
RNG = np.random.default_rng(7)
SHIFTS = 200
TOLERANCES = (0.02, 0.03, 0.05)
OLD_SESSIONS = 10


# ---------------------------------------------------------------------------------------------- inputs

def load(results: str):
    sessions = pd.read_csv(os.path.join(results, "sessions.csv"), parse_dates=["session"])
    levels = pd.read_csv(os.path.join(results, "levels.csv"), parse_dates=["session"])
    bars = pd.read_csv(os.path.join(results, "bars_1min.csv.gz"), parse_dates=["session"])
    return sessions, levels, bars


def session_stats(sessions: pd.DataFrame, bars: pd.DataFrame) -> pd.DataFrame:
    """Close, daily ATR(14) of the previous sessions, times of the high and the low, RTH open, overnight and IB."""
    b = zz.prepare_bars(bars)
    m = b.minute_of_day
    g = b.groupby("session")
    stats = pd.DataFrame({"close": g.close.last(), "open": g.open.first()})
    stats["high_time"] = b.loc[g.high.idxmax(), ["session", "minute_of_day"]].set_index("session").minute_of_day
    stats["low_time"] = b.loc[g.low.idxmin(), ["session", "minute_of_day"]].set_index("session").minute_of_day

    night = b[(m >= 20 * 60) | (m < 9 * 60 + 30)].groupby("session")
    stats["onh"], stats["onl"] = night.high.max(), night.low.min()
    ib = b[(m >= 9 * 60 + 30) & (m < 10 * 60 + 30)].groupby("session")
    stats["ibh"], stats["ibl"] = ib.high.max(), ib.low.min()
    rth_open = b[m == 9 * 60 + 30].groupby("session").open.first()
    stats["rth_open"] = rth_open

    s = sessions.set_index("session").join(stats)
    prev_close = s.close.shift(1).where(s.symbol == s.symbol.shift(1))
    tr = np.where(prev_close.isna(), s.high - s.low,
                  np.maximum(s.high, prev_close) - np.minimum(s.low, prev_close))
    s["tr"] = tr
    s["atr"] = s.tr.rolling(14).mean().shift(1)
    return s


def roll_spreads(sessions: pd.DataFrame, extract: str) -> dict[pd.Timestamp, float]:
    """New minus old contract price on the session the front contract changes (median over the common minutes)."""
    spreads = {}
    changes = sessions[(sessions.symbol != sessions.symbol.shift(1)) & sessions.symbol.shift(1).notna()]
    for _, row in changes.iterrows():
        old_id = sessions.loc[sessions.session < row.session].iloc[-1].instrument_id
        day = row.session.strftime("%Y%m%d")
        files = glob.glob(os.path.join(extract, "minute", f"*{day}.csv.gz"))
        if not files:
            continue
        df = pd.read_csv(files[0])
        df["volume"] = df.buy + df.sell + df.unknown
        vwap = (df.assign(pv=df.price * df.volume).groupby(["instrument_id", "utc_minute"])
                [["pv", "volume"]].sum())
        vwap = (vwap.pv / vwap.volume).unstack(0)
        if row.instrument_id in vwap and old_id in vwap:
            diff = (vwap[row.instrument_id] - vwap[old_id]).dropna()
            if len(diff) >= 30:
                spreads[row.session] = float(diff.median())
    return spreads


# ---------------------------------------------------------------------------------------------- zones

def profile_zones(session_row, levels_of_session, prefix: str, shift: float, rolled: bool, age: int) -> list[dict]:
    z = []

    def add(kind, low, high, **extra):
        z.append({"zone": f"{prefix}{kind}", "low": low + shift, "high": high + shift, "rolled": rolled, "age": age, **extra})

    r = session_row
    high_rth = 9 * 60 + 30 <= r.high_time < 16 * 60
    low_rth = 9 * 60 + 30 <= r.low_time < 16 * 60
    add("high", r.high, r.high, formed="rth" if high_rth else "night")
    add("low", r.low, r.low, formed="rth" if low_rth else "night")
    add("vah", r.vah, r.vah)
    add("val", r.val, r.val)
    add("poc", r.poc, r.poc)
    for _, h in levels_of_session.iterrows():
        if h.type == "hvn":
            add(f"hvn_{h.kind_w}_w", h.low, h.high)
            add(f"hvn_{h.kind_u}_u", h.low, h.high)
        else:
            add("lvn", h.low, h.high)
    return z


def build_zones(stats: pd.DataFrame, levels: pd.DataFrame, spreads: dict) -> dict[pd.Timestamp, pd.DataFrame]:
    """Zones known at the start of every session from the previous complete sessions."""
    complete = stats[stats.complete]
    by_session = {k: v for k, v in levels.groupby("session")}
    sessions = list(stats.index)
    zones = {}
    for i, session in enumerate(sessions):
        prior = complete[complete.index < session].tail(OLD_SESSIONS)
        if prior.empty:
            continue
        symbol = stats.loc[session, "symbol"]
        z = []
        for age, (prev, row) in enumerate(reversed(list(prior.iterrows())), start=1):
            # levels of an older contract are shifted by the roll spreads in between
            shift, rolled = 0.0, False
            if row.symbol != symbol:
                rolled = True
                for roll_session, spread in spreads.items():
                    if prev < roll_session <= session:
                        shift += spread
            prefix = "prev_" if age == 1 else "old_"
            z += profile_zones(row, by_session.get(prev, pd.DataFrame(columns=levels.columns)), prefix, shift, rolled, age)

        # previous week high/low (the sessions of the previous Monday-Friday week)
        week = session.to_period("W-SUN")
        prev_week = complete[complete.index.to_period("W-SUN") == week - 1]
        if not prev_week.empty and (prev_week.symbol == symbol).all():
            z.append({"zone": "week_high", "low": prev_week.high.max(), "high": prev_week.high.max(), "rolled": False, "age": 0})
            z.append({"zone": "week_low", "low": prev_week.low.min(), "high": prev_week.low.min(), "rolled": False, "age": 0})
        zones[session] = pd.DataFrame(z)
    return zones


def intraday_zones(stats_row, minute_of_day: int, vwap: float) -> list[dict]:
    z = []
    after_open = 9 * 60 + 30 <= minute_of_day < 17 * 60
    if after_open and not np.isnan(stats_row.onh):
        z += [{"zone": "onh", "low": stats_row.onh, "high": stats_row.onh},
              {"zone": "onl", "low": stats_row.onl, "high": stats_row.onl}]
    if 10 * 60 + 30 <= minute_of_day < 17 * 60 and not np.isnan(stats_row.ibh):
        z += [{"zone": "ibh", "low": stats_row.ibh, "high": stats_row.ibh},
              {"zone": "ibl", "low": stats_row.ibl, "high": stats_row.ibl}]
    if not np.isnan(vwap):
        z.append({"zone": "vwap", "low": vwap, "high": vwap})
    return z


def round_zones(price: float, atr: float, step: float = 50.0) -> list[dict]:
    """Round numbers (multiples of step) within 2 ATR of the price."""
    first = np.floor((price - 2 * atr) / step) * step
    return [{"zone": "round50", "low": p, "high": p} for p in np.arange(first, price + 2 * atr + step, step)]


# ---------------------------------------------------------------------------------------------- study

def time_bucket(m: int) -> str:
    if 18 * 60 <= m < 20 * 60: return "1 evening 18-20"
    if m >= 20 * 60 or m < 3 * 60: return "2 night 20-03"
    if m < 9 * 60 + 30: return "3 europe 03-09:30"
    if m < 10 * 60 + 30: return "4 ny open 09:30-10:30"
    if m < 15 * 60: return "5 ny day 10:30-15"
    if m < 16 * 60: return "6 ny close 15-16"
    return "7 after close 16-17"


def open_location(row) -> str:
    if np.isnan(row.prev_vah):
        return "unknown"
    price = row.open_price
    if price > row.prev_vah: return "above value"
    if price < row.prev_val: return "below value"
    return "inside value"


def run(results: str, extract: str, out: str, last_session: str = "2025-12-31") -> None:
    os.makedirs(out, exist_ok=True)
    sessions, levels, bars = load(results)
    stats = session_stats(sessions, bars)
    spreads = roll_spreads(sessions, extract)
    zones = build_zones(stats, levels, spreads)

    sw = zz.swings(bars, zz.ZigZagSettings(multipliers=(0.3, 0.5)))
    sw = sw[sw.session <= last_session].copy()

    # session VWAP of the minute before the swing
    # (the same bar order as in zz.swings, the swing "index" points into it)
    b = zz.prepare_bars(bars).sort_values("minute").reset_index(drop=True)
    b["pv"] = (b.high + b.low + b.close) / 3 * b.volume
    b["cum_pv"] = b.groupby("session").pv.cumsum() - b.pv
    b["cum_v"] = b.groupby("session").volume.cumsum() - b.volume
    vwap_before = (b.cum_pv / b.cum_v.replace(0, np.nan)).to_numpy()

    # thin extremes: the volume of the extreme bar compared to the median of the same minute of day
    median_volume = b[b.session <= last_session].groupby("minute_of_day").volume.median()
    sw["relative_volume"] = sw.volume / sw.minute_of_day.map(median_volume)

    records = []
    for _, s in sw.iterrows():
        session = s.session
        st = stats.loc[session]
        if session not in zones or np.isnan(st.atr):
            continue
        z = zones[session]
        intraday = pd.DataFrame(intraday_zones(st, s.minute_of_day, vwap_before[int(s["index"])]) +
                                round_zones(st.open, st.atr))
        allz = pd.concat([z, intraday], ignore_index=True) if not intraday.empty else z
        prev = stats[(stats.index < session) & stats.complete].iloc[-1]
        before_open = s.minute_of_day >= 18 * 60 or s.minute_of_day < 9 * 60 + 30
        open_price = st.open if before_open or np.isnan(st.rth_open) else st.rth_open
        base = {"session": session, "multiplier": s.multiplier, "type": s.type, "price": s.price,
                "minute_of_day": s.minute_of_day, "bucket": time_bucket(s.minute_of_day),
                "atr": st.atr, "leg_before": s.leg_before, "leg_after": s.leg_after,
                "relative_volume": s.relative_volume, "complete": bool(st.complete),
                "prev_vah": prev.vah, "prev_val": prev.val, "open_price": open_price,
                "half": f"{session.year}H{1 if session.month <= 6 else 2}"}
        base["open_location"] = open_location(pd.Series(base))

        lows, highs = allz.low.to_numpy(float), allz.high.to_numpy(float)
        names = allz.zone.to_numpy()
        signs = RNG.choice((-1.0, 1.0), size=(SHIFTS, 1))
        shifts = signs * RNG.uniform(0.1, 0.5, size=(SHIFTS, 1)) * st.atr
        for tol in TOLERANCES:
            d = tol * st.atr
            hit = (s.price >= lows - d) & (s.price <= highs + d)
            shifted = (s.price >= lows + shifts - d) & (s.price <= highs + shifts + d)   # SHIFTS x zones
            rec = dict(base, tolerance=tol)
            for name in np.unique(names):
                mask = names == name
                rec[f"hit:{name}"] = bool(hit[mask].any())
                rec[f"base:{name}"] = float(shifted[:, mask].any(axis=1).mean())
            for group, predicate in GROUPS.items():
                mask = np.array([predicate(n) for n in names])
                if mask.any():
                    rec[f"hit:{group}"] = bool(hit[mask].any())
                    rec[f"base:{group}"] = float(shifted[:, mask].any(axis=1).mean())
                else:
                    rec[f"hit:{group}"], rec[f"base:{group}"] = False, 0.0
            records.append(rec)

    df = pd.DataFrame(records)
    df.to_csv(os.path.join(out, "swing_hits.csv.gz"), index=False)
    pd.Series(spreads).to_csv(os.path.join(out, "roll_spreads.csv"), header=["spread"])
    print(f"{len(df)} swing x tolerance rows, {df.session.nunique()} sessions")


GROUPS = {
    "any_prev_profile": lambda n: n.startswith("prev_"),
    "any_old_profile": lambda n: n.startswith("old_"),
    "any_profile": lambda n: n.startswith("prev_") or n.startswith("old_"),
    "any_extended": lambda n: n in ("onh", "onl", "ibh", "ibl", "vwap", "week_high", "week_low", "round50"),
    "any_zone": lambda n: True,
}


if __name__ == "__main__":
    run(*sys.argv[1:])
