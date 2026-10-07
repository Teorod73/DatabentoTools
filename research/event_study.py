"""Main event study: every touch of a zone as a simulated reversal trade.

    python event_study.py <resultsDir> <extractDir> <outDir> [lastSession]

Event: the 1 minute bars of the front contract reach the touch band of a zone [low - d, high + d], d = 0.03 ATR,
after a bar that was entirely at least 0.15 ATR away from the zone. The side of that bar is the approach side,
the trade is against the approach: from below short (the zone as resistance), from above long (as support).

Trade A: limit at the near edge of the zone, filled when a bar trades one tick through it within 30 minutes of the
touch (before that a bar reaching the stop cancels it). Stop: the far edge + buffer (0.05 or 0.10 ATR) + 1 tick
slippage. Targets 1R, 2R, 3R from the entry. On the fill bar only the stop is checked (the order inside a bar is
unknown), later a bar reaching both the stop and the target counts as a loss. No target or stop in 240 minutes or
until 16:59 ET: exit at the close of the last bar. R results are net of 1 tick slippage on the stops and
0.08 point commission per trade.

Zones known before the touch: previous complete session (high, low, VAH, VAL, POC, HVNs with both
classifications, LVNs), naked older levels (sessions 2-10 back, untraded since: high, low, POC, HVNs), the
overnight range (20:00-09:30) after 09:30 and the initial balance (09:30-10:30) after 10:30. Older contract levels
are shifted by the roll spread. Trade C (conf_*): entry on a confirmation close, see simulate_confirmed.
The HVNs are in the events twice, with the weighted (weighting w) and the unweighted (u) classification:
an analysis uses one of them. Baseline: the same zones shifted by a random +-(0.1..0.5) ATR per session.
"""
from __future__ import annotations

import os
import sys
import time

import numpy as np
import pandas as pd

import reverse_study as rs
import zigzag as zz

TICK = 0.25
TOLERANCE = 0.03          # touch band in ATR
REARM = 0.15              # distance that arms a zone again, ATR
FILL_WINDOW = 30          # minutes
MAX_BARS = 240
STOP_BUFFERS = (0.05, 0.10, 0.20, 0.30)
CONFIRM_WINDOW = 15       # minutes
CONFIRM_DISTANCE = 0.05   # close back beyond the near edge, ATR
ACCEPTANCE = 0.30         # beyond the far edge before the confirmation: no trade, ATR
TARGETS = (1, 2, 3)
COMMISSION = 0.08
RNG = np.random.default_rng(11)
NIGHT_START, CASH_OPEN, IB_END, CASH_CLOSE = 20 * 60, 9 * 60 + 30, 10 * 60 + 30, 16 * 60

# expected trade side of the zone types: resistance (short from below), support (long from above), both
EXPECTED = {"high": "short", "vah": "short", "onh": "short", "ibh": "short", "hvn_upper": "short",
            "low": "long", "val": "long", "onl": "long", "ibl": "long", "hvn_lower": "long"}


def base_type(zone: str) -> str:
    """prev_hvn_upper_w -> hvn_upper, old_high -> high"""
    z = zone.split("_", 1)[1] if zone.startswith(("prev_", "old_")) else zone
    return z[:-2] if z.endswith(("_w", "_u")) else z


# ---------------------------------------------------------------------------------------------- zones

def session_zones(session, stats: pd.DataFrame, levels: dict, spreads: dict, naked_ranges) -> list[dict]:
    complete = stats[stats.complete & (stats.index < session)]
    if complete.empty:
        return []
    symbol = stats.loc[session, "symbol"]
    zones = []

    def shift_for(row_session, row_symbol):
        if row_symbol == symbol:
            return 0.0, False
        return sum(sp for rd, sp in spreads.items() if row_session < rd <= session), True

    def add(name, low, high, shift, rolled, age, **extra):
        zones.append({"zone": name, "low": low + shift, "high": high + shift, "shift": shift, "rolled": rolled,
                      "age": age, "active_from": 0, **extra})

    prior = complete.tail(rs.OLD_SESSIONS)
    for age, (prev, row) in enumerate(reversed(list(prior.iterrows())), start=1):
        shift, rolled = shift_for(prev, row.symbol)
        lv = levels.get(prev)
        high_rth = CASH_OPEN <= row.high_time < CASH_CLOSE
        low_rth = CASH_OPEN <= row.low_time < CASH_CLOSE
        if age == 1:
            add("prev_high", row.high, row.high, shift, rolled, age, formed="rth" if high_rth else "night")
            add("prev_low", row.low, row.low, shift, rolled, age, formed="rth" if low_rth else "night")
            add("prev_vah", row.vah, row.vah, shift, rolled, age)
            add("prev_val", row.val, row.val, shift, rolled, age)
            add("prev_poc", row.poc, row.poc, shift, rolled, age)
            if lv is not None:
                for _, h in lv.iterrows():
                    if h.type == "hvn":
                        add(f"prev_hvn_{h.kind_w}_w", h.low, h.high, shift, rolled, age,
                            asymmetry_w=h.asymmetry_w, asymmetry_u=h.asymmetry_u)
                        add(f"prev_hvn_{h.kind_u}_u", h.low, h.high, shift, rolled, age,
                            asymmetry_w=h.asymmetry_w, asymmetry_u=h.asymmetry_u)
                    else:
                        add("prev_lvn", h.low, h.high, shift, rolled, age)
        else:
            # naked: not traded by the sessions after it (the ones before this session)
            candidates = [("old_high", row.high, row.high), ("old_low", row.low, row.low), ("old_poc", row.poc, row.poc)]
            if lv is not None:
                candidates += [(f"old_hvn_{h.kind_u}_u", h.low, h.high) for _, h in lv[lv.type == "hvn"].iterrows()]
            later = naked_ranges[(naked_ranges.index > prev) & (naked_ranges.index < session)]
            for name, low, high in candidates:
                if not ((later.high + 0 >= low) & (later.low <= high)).any():
                    add(name, low, high, shift, rolled, age)

    st = stats.loc[session]
    if not np.isnan(st.onh):
        zones.append({"zone": "onh", "low": st.onh, "high": st.onh, "shift": 0.0, "rolled": False, "age": 0, "active_from": CASH_OPEN})
        zones.append({"zone": "onl", "low": st.onl, "high": st.onl, "shift": 0.0, "rolled": False, "age": 0, "active_from": CASH_OPEN})
    if not np.isnan(st.ibh):
        zones.append({"zone": "ibh", "low": st.ibh, "high": st.ibh, "shift": 0.0, "rolled": False, "age": 0, "active_from": IB_END})
        zones.append({"zone": "ibl", "low": st.ibl, "high": st.ibl, "shift": 0.0, "rolled": False, "age": 0, "active_from": IB_END})
    return zones


# ---------------------------------------------------------------------------------------------- events

def find_events(high, low, close, active, zlow, zhigh, atr):
    """Bar indexes of the touches and their approach side (+1 from above, -1 from below)."""
    d, z = TOLERANCE * atr, REARM * atr
    touching = (high >= zlow - d) & (low <= zhigh + d) & active
    above = (low > zhigh + z) & active
    below = (high < zlow - z) & active
    state = np.where(touching, 1, np.where(above, 2, np.where(below, -2, 0)))
    nz = np.where(state != 0, np.arange(len(state)), -1)
    last = np.maximum.accumulate(nz)
    filled = np.where(last >= 0, state[np.maximum(last, 0)], 0)
    prev = np.concatenate(([0], filled[:-1]))
    idx = np.where((filled == 1) & (np.abs(prev) == 2) & touching)[0]
    return idx, np.where(prev[idx] == 2, 1, -1)


def simulate(high, low, close, minute_of_day, i, side, zlow, zhigh, atr, buffer):
    """One trade. side +1 long (from above), -1 short (from below). Returns dict of results or None (no fill)."""
    n = len(high)
    if side == 1:
        entry, stop = zhigh, zlow - buffer * atr - TICK
    else:
        entry, stop = zlow, zhigh + buffer * atr + TICK
    risk = abs(entry - stop)
    if risk <= 0:
        return None

    fill = None
    for k in range(i, min(n, i + FILL_WINDOW)):
        through = low[k] <= entry - TICK if side == 1 else high[k] >= entry + TICK
        stopped = low[k] <= stop if side == 1 else high[k] >= stop
        if through:
            fill = k
            break
        if stopped:
            return None
    if fill is None:
        return None

    end = min(n, fill + MAX_BARS)
    # the session ends at 16:59, the bars after the last one of the session are not in this array
    h, l = high[fill:end], low[fill:end]
    hit_stop = (l <= stop) if side == 1 else (h >= stop)
    stop_at = int(np.argmax(hit_stop)) if hit_stop.any() else None
    favourable = (h - entry) if side == 1 else (entry - l)
    result = {"entry": entry, "stop": stop, "risk": risk, "fill_delay": fill - i}
    for t in TARGETS:
        target = entry + side * t * risk
        hit_target = (h >= target) if side == 1 else (l <= target)
        hit_target[0] = False          # on the fill bar only the stop counts
        target_at = int(np.argmax(hit_target)) if hit_target.any() else None
        if stop_at is not None and (target_at is None or stop_at <= target_at):
            r = -1.0 - TICK / risk
            bars = stop_at
        elif target_at is not None:
            r = float(t)
            bars = target_at
        else:
            r = side * (close[end - 1] - entry) / risk
            bars = end - 1 - fill
        result[f"r{t}"] = r - COMMISSION / risk
        result[f"bars{t}"] = bars
    upto = stop_at if stop_at is not None else len(h)
    result["mfe"] = float(favourable[:max(upto, 1)].max()) / risk
    return result


def simulate_confirmed(high, low, close, i, side, zlow, zhigh, atr):
    """Entry on the close of the first bar (within CONFIRM_WINDOW of the touch) that closes CONFIRM_DISTANCE back
    beyond the near edge on the approach side. Stop: the extreme since the touch + 0.05 ATR + 1 tick.
    The price going ACCEPTANCE beyond the far edge before the confirmation cancels the trade."""
    n = len(high)
    near = zhigh if side == 1 else zlow
    far = zlow if side == 1 else zhigh
    extreme = low[i] if side == 1 else high[i]
    for k in range(i, min(n, i + CONFIRM_WINDOW)):
        extreme = min(extreme, low[k]) if side == 1 else max(extreme, high[k])
        if (side == 1 and extreme <= far - ACCEPTANCE * atr) or (side == -1 and extreme >= far + ACCEPTANCE * atr):
            return None
        confirmed = close[k] >= near + CONFIRM_DISTANCE * atr if side == 1 else close[k] <= near - CONFIRM_DISTANCE * atr
        if confirmed and k + 1 < n:
            entry = close[k]
            stop = extreme - 0.05 * atr - TICK if side == 1 else extreme + 0.05 * atr + TICK
            risk = abs(entry - stop)
            end = min(n, k + 1 + MAX_BARS)
            h, l = high[k + 1:end], low[k + 1:end]
            hit_stop = (l <= stop) if side == 1 else (h >= stop)
            stop_at = int(np.argmax(hit_stop)) if hit_stop.any() else None
            result = {"entry": entry, "stop": stop, "risk": risk, "fill_delay": k - i}
            for t in TARGETS:
                target = entry + side * t * risk
                hit_target = (h >= target) if side == 1 else (l <= target)
                target_at = int(np.argmax(hit_target)) if hit_target.any() else None
                if stop_at is not None and (target_at is None or stop_at <= target_at):
                    r = -1.0 - TICK / risk
                elif target_at is not None:
                    r = float(t)
                else:
                    r = side * (close[end - 1] - entry) / risk
                result[f"r{t}"] = r - COMMISSION / risk
            return result
    return None


def run(results: str, extract: str, out: str, last_session: str = "2025-12-31") -> None:
    os.makedirs(out, exist_ok=True)
    started = time.time()
    sessions, levels, bars = rs.load(results)
    stats = rs.session_stats(sessions, bars)
    spreads = rs.roll_spreads(sessions, extract)
    levels_by = {k: v for k, v in levels.groupby("session")}
    naked_ranges = stats[["high", "low"]]

    b = zz.prepare_bars(bars).sort_values("minute").reset_index(drop=True)
    b = b[b.session <= last_session]
    median_volume = b.groupby("minute_of_day").volume.median()
    records = []

    for session, day in b.groupby("session", sort=True):
        st = stats.loc[session]
        if np.isnan(st.atr):
            continue
        atr = st.atr
        high, low, close = day.high.to_numpy(float), day.low.to_numpy(float), day.close.to_numpy(float)
        mod = day.minute_of_day.to_numpy()
        from_start = day.from_start.to_numpy()
        volume = day.volume.to_numpy(float)
        rel_volume = volume / day.minute_of_day.map(median_volume).to_numpy(float)
        run_high, run_low = np.maximum.accumulate(high), np.minimum.accumulate(low)
        prev = stats[(stats.index < session) & stats.complete]
        if prev.empty:
            continue
        prev = prev.iloc[-1]
        rth_open = st.rth_open
        zones = session_zones(session, stats, levels_by, spreads, naked_ranges)
        shift = RNG.choice((-1.0, 1.0)) * RNG.uniform(0.1, 0.5) * atr

        for baseline in (False, True):
            for zi, zn in enumerate(zones):
                zlow = zn["low"] + (shift if baseline else 0.0)
                zhigh = zn["high"] + (shift if baseline else 0.0)
                active_from = zn["active_from"]
                active = np.ones(len(high), dtype=bool) if active_from == 0 else \
                    (from_start >= (active_from - 18 * 60) % (24 * 60)) & (mod < 17 * 60)
                idx, sides = find_events(high, low, close, active, zlow, zhigh, atr)
                for test, (i, side_from) in enumerate(zip(idx, sides), start=1):
                    trade_side = 1 if side_from == 1 else -1      # from above long, from below short
                    expected = EXPECTED.get(base_type(zn["zone"]), "both")
                    good = expected == "both" or (expected == "long") == (trade_side == 1)
                    # other zones near this one at the touch
                    own = (round(zn["low"], 4), round(zn["high"], 4))
                    others = len({(round(o["low"], 4), round(o["high"], 4)) for o in zones
                                  if (round(o["low"], 4), round(o["high"], 4)) != own and
                                  o["low"] - 0.1 * atr <= zn["high"] and o["high"] + 0.1 * atr >= zn["low"]})
                    before = mod[i] >= 18 * 60 or mod[i] < CASH_OPEN
                    open_price = st.open if before or np.isnan(rth_open) else rth_open
                    location = ("above value" if open_price > prev.vah else
                                "below value" if open_price < prev.val else "inside value")
                    k0 = max(0, i - 15)
                    approach = (close[i - 1] - close[k0]) / atr if i > 0 else 0.0
                    weighting = zn["zone"][-1] if zn["zone"].endswith(("_w", "_u")) else ""
                    rec = {"session": session, "baseline": baseline, "zone": zn["zone"], "type": base_type(zn["zone"]),
                           "weighting": weighting,
                           "zone_low": zlow, "zone_high": zhigh, "width_atr": (zhigh - zlow) / atr,
                           "age": zn["age"], "rolled": zn["rolled"], "formed": zn.get("formed", ""),
                           "minute_of_day": int(mod[i]), "bucket": rs.time_bucket(int(mod[i])),
                           "trade_side": "long" if trade_side == 1 else "short", "good_side": good, "test": test,
                           "confluence": others, "open_location": location, "atr": atr,
                           "approach_speed": abs(approach), "range_so_far": (run_high[i] - run_low[i]) / atr,
                           "rel_volume5": float(np.nanmean(rel_volume[max(0, i - 4):i + 1])),
                           "complete": bool(st.complete), "half": f"{session.year}H{1 if session.month <= 6 else 2}"}
                    res = simulate_confirmed(high, low, close, i, trade_side, zlow, zhigh, atr)
                    rec["conf_filled"] = res is not None
                    if res is not None:
                        for name, value in res.items():
                            rec[f"conf_{name}"] = value
                    for buffer in STOP_BUFFERS:
                        res = simulate(high, low, close, mod, i, trade_side, zlow, zhigh, atr, buffer)
                        key = f"b{int(buffer * 100):02d}"
                        rec[f"{key}_filled"] = res is not None
                        if res is not None:
                            for name, value in res.items():
                                rec[f"{key}_{name}"] = value
                    records.append(rec)

    df = pd.DataFrame(records)
    df.to_csv(os.path.join(out, "events.csv.gz"), index=False)
    print(f"{len(df)} events ({(~df.baseline).sum()} real), {df.session.nunique()} sessions, {time.time() - started:.0f} s")


if __name__ == "__main__":
    run(*sys.argv[1:])
