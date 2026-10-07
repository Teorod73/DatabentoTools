"""Refined simulation of the good side touches: second bars, structural targets, news windows.

    python refine_study.py <resultsDir> <extractDir> <outDir>

Input: book_analysis/book_events_features.csv.gz (the good side touches with the book features), the zones of the
event study and the second bars of the extract.

Entry (second bars): the touch is the first second in the touch minute that reaches the touch band. From the end of
the touch minute (so the touch minute book features are known) up to 15 minutes, the first second closing
CONFIRM_DISTANCE ATR back beyond the near edge on the approach side; the price going ACCEPTANCE ATR beyond the far
edge before cancels. Market entry at that close with 1 tick slippage. Stop: the extreme since the touch + 0.05 ATR
+ 1 tick, 1 tick slippage when hit. A second reaching both the stop and a target counts as a loss.

Targets: 1R, 2R, 3R and structural ones, the near edge of the first zone in the trade direction at least 1R
(zone1) or 2R (zone2) away from the entry (limit, filled one tick through). room = distance of the first zone in the
trade direction / R. Exit after 240 minutes or at the last second of the session.

News: a 08:30, 10:00, 14:00 or 14:30 ET minute with at least NEWS_VOLUME x its usual volume (median of the same
minute of day) is a release; touches from 10 minutes before to 20 minutes after are flagged (news = True).
"""
from __future__ import annotations

import os
import sys
import time

import numpy as np
import pandas as pd

import event_study as es
import profiles as pr
import reverse_study as rs
import zigzag as zz

TICK = 0.25
COMMISSION = 0.08
CONFIRM_DISTANCE = 0.05
ACCEPTANCE = 0.30
CONFIRM_WINDOW = 15 * 60
MAX_HOLD = 240 * 60
RELEASE_MINUTES = (8 * 60 + 30, 10 * 60, 14 * 60, 14 * 60 + 30)
NEWS_VOLUME = 3.0


def touch_minute_utc(df: pd.DataFrame) -> pd.Series:
    day = df.session - pd.to_timedelta((df.minute_of_day >= 18 * 60).astype(int), unit="D")
    local = day + pd.to_timedelta(df.minute_of_day, unit="m")
    utc = local.dt.tz_localize(es.zz.ET, ambiguous=True, nonexistent="shift_forward").dt.tz_convert("UTC")
    return (utc - pd.Timestamp("1970-01-01", tz="UTC")) // pd.Timedelta(seconds=1)


def news_releases(bars: pd.DataFrame) -> pd.DataFrame:
    """Release minutes found from the volume: (session, minute_of_day) with the relative volume."""
    b = zz.prepare_bars(bars)
    usual = b.groupby("minute_of_day").volume.median()
    b["relative"] = b.volume / b.minute_of_day.map(usual)
    r = b[b.minute_of_day.isin(RELEASE_MINUTES) & (b.relative >= NEWS_VOLUME)]
    return r[["session", "minute_of_day", "relative"]].reset_index(drop=True)


def simulate(sec: dict, touch_t0: int, side: int, zlow: float, zhigh: float, band: float, atr: float,
             targets_zone: list[float], session_end: int) -> dict | None:
    t, high, low, close = sec["t"], sec["high"], sec["low"], sec["close"]
    i0 = np.searchsorted(t, touch_t0)
    i1 = np.searchsorted(t, touch_t0 + 60)
    in_band = (high[i0:i1] >= zlow - band) & (low[i0:i1] <= zhigh + band)
    touch = i0 + int(np.argmax(in_band)) if in_band.any() else i0
    if touch >= len(t):
        return None

    near, far = (zhigh, zlow) if side == 1 else (zlow, zhigh)
    end_confirm = np.searchsorted(t, touch_t0 + CONFIRM_WINDOW)
    extreme = low[touch] if side == 1 else high[touch]
    entry_i = None
    for k in range(touch, min(end_confirm, len(t))):
        extreme = min(extreme, low[k]) if side == 1 else max(extreme, high[k])
        if (side == 1 and extreme <= far - ACCEPTANCE * atr) or (side == -1 and extreme >= far + ACCEPTANCE * atr):
            return None
        if k < i1:
            continue   # the book features of the touch minute are known only at its end
        if (side == 1 and close[k] >= near + CONFIRM_DISTANCE * atr) or (side == -1 and close[k] <= near - CONFIRM_DISTANCE * atr):
            entry_i = k
            break
    if entry_i is None:
        return None

    entry = close[entry_i] + side * TICK
    stop = extreme - side * (0.05 * atr + TICK)
    risk = abs(entry - stop)
    end = min(np.searchsorted(t, min(t[entry_i] + MAX_HOLD, session_end)), len(t))
    h, l = high[entry_i + 1:end], low[entry_i + 1:end]
    if len(h) == 0:
        return None
    hit_stop = (l <= stop) if side == 1 else (h >= stop)
    stop_at = int(np.argmax(hit_stop)) if hit_stop.any() else None
    last_close = close[end - 1]

    # the zones in the trade direction (their near edge to the entry)
    ahead = sorted(z for z in targets_zone if side * (z - entry) > 0)
    ahead = ahead if side == 1 else ahead[::-1]
    room = abs(ahead[0] - entry) / risk if ahead else np.inf
    out = {"entry": entry, "stop": stop, "risk": risk, "entry_delay": int(t[entry_i] - touch_t0), "room": room}

    def outcome(target: float | None) -> float:
        if target is None:
            reach = None
        else:
            hit = (h >= target + TICK) if side == 1 else (l <= target - TICK)
            reach = int(np.argmax(hit)) if hit.any() else None
        if stop_at is not None and (reach is None or stop_at <= reach):
            r = -(risk + TICK) / risk
        elif reach is not None:
            r = abs(target - entry) / risk
        else:
            r = side * (last_close - entry) / risk
        return r - COMMISSION / risk

    for m in (1, 2, 3):
        out[f"r{m}"] = outcome(entry + side * m * risk)
    for name, minimum in (("zone1", 1.0), ("zone2", 2.0)):
        target = next((z for z in ahead if abs(z - entry) >= minimum * risk), None)
        out[f"{name}_rr"] = abs(target - entry) / risk if target is not None else np.nan
        out[f"r_{name}"] = outcome(target) if target is not None else np.nan
    return out


def zone_edges(zones: list[dict], minute_of_day: int) -> list[float]:
    """Both edges of the zones active at the minute (distinct prices)."""
    from_start = (minute_of_day - 18 * 60) % (24 * 60)
    edges = set()
    for z in zones:
        active_from = z["active_from"]
        if active_from and from_start < (active_from - 18 * 60) % (24 * 60):
            continue
        edges.add(round(z["low"], 4))
        edges.add(round(z["high"], 4))
    return sorted(edges)


def run(results: str, extract: str, out: str) -> None:
    started = time.time()
    os.makedirs(out, exist_ok=True)
    ev = pd.read_csv(os.path.join(results, "book_analysis", "book_events_features.csv.gz"), parse_dates=["session"])
    ev["t0"] = touch_minute_utc(ev)
    sessions, levels, bars = rs.load(results)
    stats = rs.session_stats(sessions, bars)
    spreads = rs.roll_spreads(sessions, extract)
    levels_by = {k: v for k, v in levels.groupby("session")}
    naked = stats[["high", "low"]]

    releases = news_releases(bars[bars.session <= "2025-12-31"])
    releases.to_csv(os.path.join(out, "news_releases.csv"), index=False)
    by_session = {k: g.minute_of_day.tolist() for k, g in releases.groupby("session")}

    def is_news(row) -> bool:
        m = row.minute_of_day
        return any(-10 <= m - r <= 20 for r in by_session.get(row.session, []))

    ev["news"] = ev.apply(is_news, axis=1)
    wanted = set(ev.session.unique())
    symbols = pr.read_symbols(extract)
    records = []

    for session, _, seconds in pr.iter_sessions(extract):
        if session not in wanted:
            continue
        front = sessions.set_index("session").loc[session, "instrument_id"]
        s = seconds[seconds.instrument_id == front].sort_values("utc_second")
        sec = {"t": s.utc_second.to_numpy(np.int64), "high": s.high.to_numpy(float),
               "low": s.low.to_numpy(float), "close": s.close.to_numpy(float)}
        if len(sec["t"]) == 0:
            continue
        session_end = int(sec["t"][-1]) + 1
        zones = es.session_zones(session, stats, levels_by, spreads, naked)
        for _, e in ev[ev.session == session].iterrows():
            side = 1 if e.trade_side == "long" else -1
            res = simulate(sec, int(e.t0), side, e.zone_low, e.zone_high, 0.03 * e.atr, e.atr,
                           zone_edges(zones, int(e.minute_of_day)), session_end)
            rec = {"event_id": e.event_id, "sec_filled": res is not None}
            if res:
                rec.update({f"sec_{k}": v for k, v in res.items()})
            records.append(rec)

    out_df = ev.merge(pd.DataFrame(records), on="event_id", how="left")
    out_df.to_csv(os.path.join(out, "refined_events.csv.gz"), index=False)
    print(f"{len(out_df)} touches, {out_df.sec_filled.sum()} entries, {len(releases)} release minutes, "
          f"{time.time() - started:.0f} s")


if __name__ == "__main__":
    run(*sys.argv[1:4])
