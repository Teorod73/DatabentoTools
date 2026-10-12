"""Level sweeps with a local HVN, step 1: price and volume only (no order flow).

    python sweep_study.py <resultsDir> <researchDataDir> <outDir> [lastSession] [proximity]

Written for a high (short), a low is mirrored. Reference levels, every group evaluated on its own:
    prev_hl    previous session (18:00-17:00) profile high / low
    prev_va    previous session VAH / VAL
    prev_hvn   previous session HVNs (zones; the near edge is the level)
    on_hl      today's 20:00-09:30 profile high / low, from 09:30
    on_va      today's 20:00-09:30 profile VAH / VAL, from 09:30
    zzN        0.5 ZigZag highs / lows of the previous N sessions (N = 1, 5, 10, 15), from their confirmation
    week_hl    previous week (Sunday 18:00 - Friday 17:00) high / low
Older contract levels are shifted by the roll spread. A level is a short setup while nothing traded above it since
it is known (the session end for the previous session levels, 09:30 for the overnight ones, the confirmation for a
ZigZag point, the week end for the week); a long setup while nothing traded below it.

Event: a bar comes within PROXIMITY x ATR1 of an untouched level (ATR1 = 1 minute ATR(20) of the bar before). T0 is
the bar after the last bar whose high was 2 x ATR1 below that bar's high (as the candidate windows). From then on the
local volume profile of T0 .. the previous bar (front contract, traded prices, Prominence HVNs with Sigma Multiplier
0.41) is taken every minute: the highest HVN whose band is within PROXIMITY x ATR1 of the level (or overlaps it).
Entry: the first bar that closes below the low edge of that HVN. The event ends without a trade when the price moves
2 x ATR1 away below the level, after MAX_EVENT bars or at the session end; one event per level and side per session.

Trade (as the economic test): short at the close of the entry bar, stop = the highest high since T0 + 2 ticks, no trade
when the risk is above 2 x ATR1, targets 1R / 2R / 3R, exit after 60 minutes or at 17:00 ET, 1 tick slippage at the
stop, 0.08 point commission; the exit from the 1 second bars.
Volume: the relative volume from T0 to the entry bar (the minute volume / the median of the same minute of day over the
previous 20 sessions), high = above the top fifth of the 2024 candidates (night 2.04, RTH 1.84).
Labels: the bar of the stop extreme is a ZigZag swing of the same type (0.3 or 0.5; 0.5 = blue).
Placebo: every level of a session shifted by a random +-(0.1 .. 0.5) daily ATR, the same detection.
Outputs: events.csv.gz, summary.md.
"""
from __future__ import annotations

import glob
import os
import sys
import time

import numpy as np
import pandas as pd

import pattern_trades as pt
import pivot_windows as pw
import profiles as pf
import reverse_study as rs
import zigzag as zz

TICK = 0.25
PROXIMITY = 0.5
AWAY = 2.0
MAX_EVENT = 120
LOOKBACK = 240
LOOKBACKS = (1, 5, 10, 15)
HIGH_VOLUME = {"night": 2.04, "rth": 1.84}
SETTINGS = pf.Settings(sigma_multiplier=0.41)
RNG = np.random.default_rng(20261012)
NIGHT_START, CASH_OPEN, CASH_CLOSE = 20 * 60, 9 * 60 + 30, 16 * 60
GROUPS = ["prev_hl", "prev_va", "prev_hvn", "on_hl", "on_va"] + [f"zz{n}" for n in LOOKBACKS] + ["week_hl"]


class MinuteProfiles:
    """Aggressor volume per price and minute of one instrument (DatabentoExtract minute/), per UTC day on demand."""

    def __init__(self, directory: str):
        self.files = {os.path.basename(f)[-15:-7]: f for f in glob.glob(os.path.join(directory, "minute", "*.csv.gz"))}
        self.cache: dict[str, pd.DataFrame] = {}

    def rows(self, instrument: int, start: int, end: int) -> pd.DataFrame:
        parts = []
        for d in pd.date_range(pd.to_datetime(start, unit="s").normalize(), pd.to_datetime(end, unit="s"),
                               freq="D").strftime("%Y%m%d"):
            if d not in self.files:
                continue
            if d not in self.cache:
                if len(self.cache) > 3:
                    self.cache.pop(next(iter(self.cache)))
                df = pd.read_csv(self.files[d])
                df["volume"] = df.buy + df.sell + df.unknown
                self.cache[d] = df[["utc_minute", "instrument_id", "price", "volume"]]
            df = self.cache[d]
            parts.append(df[(df.instrument_id == instrument) & (df.utc_minute >= start) & (df.utc_minute < end)])
        if not parts:
            return pd.DataFrame(columns=["utc_minute", "price", "volume"])
        return pd.concat(parts).sort_values("utc_minute")


def overnight_profile(rows: pd.DataFrame, start: int, end: int) -> tuple[float, float, float, float]:
    """High, low, VAH, VAL of the rows of [start, end)."""
    r = rows[(rows.utc_minute >= start) & (rows.utc_minute < end)]
    if r.empty:
        return (np.nan,) * 4
    p = r.groupby("price").volume.sum().sort_index()
    _, vah, val = pf.value_area(p.index.to_numpy(float), p.to_numpy(float), SETTINGS.value_area_percentage)
    return float(p.index.max()), float(p.index.min()), vah, val


def top_hvn(prices: np.ndarray, volumes: np.ndarray, side: int):
    """The highest HVN (lowest at a low) of a profile: (low, high, peak) or None."""
    if len(prices) < 3 or volumes.sum() <= 0:
        return None
    _, hvns, _ = pf.prominence_clusters(prices, volumes, SETTINGS)
    if not hvns:
        return None
    h = max(hvns, key=lambda x: x["high"]) if side == -1 else min(hvns, key=lambda x: x["low"])
    return h["low"], h["high"], h["peak"]


def main(results: str, data: str, out: str, last_session: str = "2025-12-31", proximity: float = PROXIMITY) -> None:
    started = time.time()
    os.makedirs(out, exist_ok=True)
    sessions, levels, bars = rs.load(results)
    stats = rs.session_stats(sessions, bars)
    spreads = rs.roll_spreads(sessions, data)
    levels_by = {k: v for k, v in levels[levels.type == "hvn"].groupby("session")}

    b = zz.prepare_bars(bars).sort_values("minute").reset_index(drop=True)
    b = b[b.session <= pd.Timestamp(last_session)].reset_index(drop=True)
    high, low, close = b.high.to_numpy(float), b.low.to_numpy(float), b.close.to_numpy(float)
    minute = b.minute.to_numpy(np.int64)
    mod = b.minute_of_day.to_numpy()
    symbol = b.instrument_id.to_numpy()
    sess_of = b.session.to_numpy()
    atr1 = pw.atr_ninja(high, low, close, symbol, pw.ATR_PERIOD)

    # relative volume: the minute volume / the median of the same minute of day over the previous 20 sessions
    vol = b.pivot_table(index="session", columns="minute_of_day", values="volume", aggfunc="sum").sort_index()
    usual = vol.shift(1).rolling(20, min_periods=15).median()
    usual_long = usual.stack().rename("usual").reset_index()
    rel = (b[["session", "minute_of_day", "volume"]].merge(usual_long, on=["session", "minute_of_day"], how="left")
           .eval("volume / usual").to_numpy(float))

    swings = zz.swings(b)
    swing_keys = {(m, int(i), t) for m, i, t in zip(swings.multiplier, swings["index"], swings.type)}
    zz05 = swings[swings.multiplier == 0.5].copy()
    zz05["session"] = sess_of[zz05["index"].to_numpy()]

    session_list = [s for s in stats.index if s <= pd.Timestamp(last_session)]
    idx = pd.Series(np.arange(len(b))).groupby(sess_of)
    first, last = idx.min(), idx.max()

    def shift_between(a, c) -> float:
        """Price shift from the contract of session a to the contract of session c."""
        return sum(sp for rd, sp in spreads.items() if a < rd <= c)

    def range_between(g0: int, g1: int, session) -> tuple[float, float]:
        """Max high and min low of the global bars [g0, g1), in the contract of the session."""
        hi, lo = -np.inf, np.inf
        g = g0
        while g < g1:
            s = sess_of[g]
            end = min(g1, int(last[s]) + 1)
            sh = shift_between(s, session)
            hi, lo = max(hi, high[g:end].max() + sh), min(lo, low[g:end].min() + sh)
            g = end
        return hi, lo

    minutes = MinuteProfiles(data)
    seconds = pt.Seconds(data)
    week_of = {d: (d - pd.Timedelta(days=d.weekday())) for d in session_list}
    records = []

    def scan(side, level, start, g_first, g_last, info, rows):
        """The first approach to the level from the untouched side, its local HVN and the entry, then the trade.
        Computed in the short space: at a low every price is negated."""
        sg = -1 if side == 1 else 1
        hx, lx, cx = (high, low, close) if side == -1 else (-low, -high, -close)
        lev = level * sg
        near = hx[start:g_last + 1] >= lev - proximity * atr1[start - 1:g_last]
        i0 = start + int(np.argmax(near)) if near.any() else None
        if i0 is None or i0 == start:
            return None                                   # never approached, or not from the untouched side
        a0 = atr1[i0 - 1]
        k = i0 - 1
        floor = max(g_first, i0 - LOOKBACK)
        while k >= floor and hx[k] >= hx[i0] - AWAY * a0:
            k -= 1
        t0 = k + 1
        um = rows.utc_minute.to_numpy(np.int64)
        ticks = np.round(rows.price.to_numpy(float) * sg / TICK).astype(np.int64)
        rv = rows.volume.to_numpy(float)
        base = ticks.min() if len(ticks) else 0
        acc = np.zeros((ticks.max() - base + 1) if len(ticks) else 1)

        def add(bar):
            lo_i, hi_i = np.searchsorted(um, minute[bar]), np.searchsorted(um, minute[bar] + 1)
            np.add.at(acc, ticks[lo_i:hi_i] - base, rv[lo_i:hi_i])

        for bar in range(t0, i0):
            add(bar)
        rec = {**info, "side": "short" if side == -1 else "long", "level": level, "approach_minute": int(minute[i0]),
               "t0_minute": int(minute[t0]), "atr1": a0, "traded": False}
        for t in range(i0, min(g_last, i0 + MAX_EVENT) + 1):
            nz = np.flatnonzero(acc)
            hvn = top_hvn((nz + base) * TICK, acc[nz], -1) if len(nz) else None
            if hvn is not None:
                h_lo, h_hi, h_peak = hvn
                dist = 0.0 if h_lo <= lev <= h_hi else min(abs(h_lo - lev), abs(h_hi - lev))
                if dist <= proximity * a0 and cx[t] < h_lo:
                    return trade(rec, side, sg, t, t0, a0, (h_lo * sg, h_hi * sg, h_peak * sg), dist, hx)
            add(t)
            if hx[t] < lev - AWAY * a0:
                break
        return rec

    def trade(rec, side, sg, t, t0, a0, hvn, dist, hx):
        ext = t0 + int(np.argmax(hx[t0:t + 1]))
        extreme = hx[ext] * sg
        entry = close[t]
        stop = extreme - side * 2 * TICK
        risk = (entry - stop) * side
        part = pw.part_of_day(int(mod[t]))
        typ = "high" if side == -1 else "low"
        rec.update({"traded": True, "entry_minute": int(minute[t]), "minute_of_day": int(mod[t]), "part": part,
                    "hvn_low": min(hvn[0], hvn[1]), "hvn_high": max(hvn[0], hvn[1]), "hvn_peak": hvn[2],
                    "hvn_dist_atr1": dist / a0, "extreme": extreme, "beyond_level_atr1": (extreme - rec["level"]) * -side / a0,
                    "rel_volume": float(np.nanmean(rel[t0:t + 1])), "entry": entry, "risk": risk,
                    "risk_atr1": risk / a0, "pivot03": (0.3, ext, typ) in swing_keys,
                    "pivot05": (0.5, ext, typ) in swing_keys})
        rec["high_volume"] = part in HIGH_VOLUME and rec["rel_volume"] > HIGH_VOLUME[part]
        if part == "none" or not (risk > 0) or risk > pt.MAX_RISK_ATR * a0:
            rec["traded"] = False
            return rec
        session_end = int(pd.Timestamp(f"{rec['session']} 17:00", tz="America/New_York").timestamp())
        es = int(minute[t]) + 60
        sb = seconds.bars(int(symbol[t]), es, min(es + pt.HORIZON, session_end))
        rec.update(pt.simulate(sb.sort_values("utc_second"), side, entry, stop))
        return rec

    for si, session in enumerate(session_list):
        if session not in first.index or si == 0:
            continue
        st = stats.loc[session]
        if np.isnan(st.atr):
            continue
        g_first, g_last = int(first[session]), int(last[session])
        instrument = int(symbol[g_first])
        prev = stats[(stats.index < session) & stats.complete]
        if prev.empty:
            continue
        prev_s, pr = prev.index[-1], prev.iloc[-1]
        rows = minutes.rows(instrument, int(minute[g_first]), int(minute[g_last]) + 60)
        sh_prev = shift_between(prev_s, session)
        opens = np.flatnonzero(mod[g_first:g_last + 1] == CASH_OPEN)
        open_bar = g_first + int(opens[0]) if len(opens) else None

        lv = [("prev_hl", "high", pr.high + sh_prev, pr.high + sh_prev, g_first),
              ("prev_hl", "low", pr.low + sh_prev, pr.low + sh_prev, g_first),
              ("prev_va", "vah", pr.vah + sh_prev, pr.vah + sh_prev, g_first),
              ("prev_va", "val", pr.val + sh_prev, pr.val + sh_prev, g_first)]
        for _, h in levels_by.get(prev_s, pd.DataFrame(columns=["low", "high"])).iterrows():
            lv.append(("prev_hvn", "hvn", h.low + sh_prev, h.high + sh_prev, g_first))
        if open_bar is not None:
            night = np.flatnonzero((mod[g_first:open_bar] >= NIGHT_START) | (mod[g_first:open_bar] < CASH_OPEN))
            if len(night):
                ohi, olo, ovah, oval = overnight_profile(rows, int(minute[g_first + night[0]]), int(minute[open_bar]))
                if not np.isnan(ohi):
                    lv += [("on_hl", "onh", ohi, ohi, open_bar), ("on_hl", "onl", olo, olo, open_bar),
                           ("on_va", "on_vah", ovah, ovah, open_bar), ("on_va", "on_val", oval, oval, open_bar)]
        for n in LOOKBACKS:
            back = session_list[max(0, si - n):si]
            sw = zz05[zz05.session.isin(back) & (zz05.confirm_index <= g_last)]
            for _, s in sw.iterrows():
                sh = shift_between(s.session, session)
                lv.append((f"zz{n}", s.type, s.price + sh, s.price + sh, int(s.confirm_index) + 1))
        prev_week = [d for d in session_list[:si] if week_of[d] < week_of[session]]
        if prev_week:
            wd = [d for d in prev_week if week_of[d] == week_of[prev_week[-1]]]
            whi = max(stats.loc[d, "high"] + shift_between(d, session) for d in wd)
            wlo = min(stats.loc[d, "low"] + shift_between(d, session) for d in wd)
            this_week = [d for d in session_list[:si + 1] if week_of[d] == week_of[session] and d in first.index]
            known = int(first[this_week[0]])
            lv += [("week_hl", "week_high", whi, whi, known), ("week_hl", "week_low", wlo, wlo, known)]

        placebo_shift = RNG.choice((-1.0, 1.0)) * RNG.uniform(0.1, 0.5) * st.atr
        for baseline in (False, True):
            d = placebo_shift if baseline else 0.0
            for group, name, zlo, zhi, known in lv:
                if np.isnan(zlo) or np.isnan(zhi):
                    continue
                zlo, zhi = zlo + d, zhi + d
                start = max(known, g_first)
                if start > g_last:
                    continue
                for side in (-1, 1):
                    level = zlo if side == -1 else zhi
                    if known < g_first:
                        hi, lo = range_between(known, g_first, session)
                        if (side == -1 and hi > level) or (side == 1 and lo < level):
                            continue
                    info = {"session": session.date(), "year": session.year, "baseline": baseline, "group": group,
                            "name": name, "zone_low": zlo, "zone_high": zhi, "atr": st.atr}
                    rec = scan(side, level, start, g_first, g_last, info, rows)
                    if rec is not None:
                        records.append(rec)
        if si % 25 == 0:
            print(f"{session.date()} {len(records)} events, {time.time() - started:.0f} s", flush=True)

    df = pd.DataFrame(records)
    df.to_csv(os.path.join(out, "events.csv.gz"), index=False)
    n_sessions = df.groupby("year").session.nunique().to_dict()
    with open(os.path.join(out, "summary.md"), "w", encoding="utf-8") as f:
        f.write(summary(df, proximity, n_sessions))
    print(f"{len(df)} events, {time.time() - started:.0f} s")


def summary(df: pd.DataFrame, proximity: float, n_sessions: dict) -> str:
    lines = ["# Szint-kisöprés helyi HVN-nel (1. lépés, ár és volumen)", "",
             f"Közelség {proximity} x ATR1, Sigma Multiplier {SETTINGS.sigma_multiplier}. Kötés: short a HVN alsó széle "
             "alatti záráson (Low tükrözve), stop a szélsőérték + 2 tick, kockázat ≤ 2 x ATR1, 60 perc. Nettó R ± 95% "
             "(seanszonként klaszterezve). forduló = a stop-szélsőérték gyertyája 0,3 vagy 0,5 ZigZag-pont, kék = 0,5. "
             "stop = 60 percen belül ütött. placebó = a szintek véletlenül eltolva (±0,1-0,5 napi ATR). nagy volumen = "
             "a T0-tól a belépésig mért relatív volumen a 2024-es jelöltek legfelső ötöde fölött.", ""]
    t = df[df.traded.fillna(False).astype(bool)].copy()
    t["weight"] = 1.0
    t["pivot"] = t.pivot03.astype(bool) | t.pivot05.astype(bool)
    t["stopped"] = t.mae_r >= 1
    for part in ("night", "rth"):
        lines += [f"## {'Éjszaka' if part == 'night' else 'RTH'}", "",
                  "| csoport | volumen | év | szint | kötés | / nap | forduló | kék | stop | 1R | 2R | 3R |",
                  "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for group in GROUPS:
            for vol_name, vol_mask in (("mind", None), ("nagy", True)):
                for year in sorted(t.year.unique()):
                    for baseline in (False, True):
                        g = t[(t.part == part) & (t.group == group) & (t.year == year) & (t.baseline == baseline)]
                        if vol_mask:
                            g = g[g.high_volume.astype(bool)]
                        if g.empty:
                            continue
                        cells = []
                        for k in pt.TARGETS:
                            m, band = pt.weighted(g, f"r{k}")
                            cells.append(f"{m:+.2f} ±{band:.2f}")
                        lines.append(f"| {group} | {vol_name} | {year} | {'placebó' if baseline else 'valódi'} | "
                                     f"{len(g)} | {len(g) / n_sessions.get(year, 1):.2f} | {g["pivot"].mean():.0%} | "
                                     f"{g.pivot05.astype(bool).mean():.0%} | {g.stopped.mean():.0%} | " + " | ".join(cells) + " |")
        lines.append("")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else "2025-12-31",
         float(sys.argv[5]) if len(sys.argv) > 5 else PROXIMITY)
