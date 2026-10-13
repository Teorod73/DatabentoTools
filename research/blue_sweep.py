"""Sweeps of the blue (0.5) ZigZag points with a local volume profile: the raw rule (price and volume only).

    python blue_sweep.py <resultsDir> <researchDataDir> <outDir> [firstSession] [lastSession]

Written for a high (short), a low is mirrored.
Levels: the 0.5 ZigZag highs and lows (TimeOfDay reference). A point is live from its confirmation until a trade above
the high (below the low); every old point stays live (shifted by the roll spread at a contract change).
Process: a bar touches a live high (high >= level; several in one bar: the highest). One process at a time, a new
touch replaces it. The local profile starts at the 15th bar before the touch (not before the session start) and
grows bar by bar (traded prices of the front contract, Prominence HVNs with Sigma Multiplier 0.41). Entry: a closed
bar below the low edge of the highest HVN of the profile up to the previous bar -> short at its close. Then the
signal pauses until a closed bar makes a high above the profile high at the last entry; at the next close below the
highest HVN: nothing while the position of this process is open, a new short when it was closed (the profile always covers the
start .. the current bar, only the signal pauses).
Stop: the highest high since the profile start + BUFFER ticks (5, and 10 / 15 / 20 to measure the stops), fixed.
Targets, each a separate run: 1R, 2R, 3R, the nearest live opposite blue point below the entry (none: no target),
none. Exit at the 16:00 ET close at the latest. Costs: 1 tick slippage at the stop, 0.08 point commission. Exits from
the 1 minute bars: a bar reaching the stop and the target counts as the stop.
The process ends when the price moves 10 x the 20 session average ATR1 of the part of day (20:00-09:30 and
09:30-16:00 separately) away from the level up or down, at another touch, or at 16:00; an open position stays until
its own exit, so positions of several processes can be open together. Touches and entries 20:00-16:00 ET only.
Outputs: trades.csv.gz (one row per trade and configuration), summary.md.
"""
from __future__ import annotations

import os
import sys
import time

import numpy as np
import pandas as pd

import pattern_trades as pt
import pivot_windows as pw
import profiles as pf
import reverse_study as rs
import sweep_study as sw
import zigzag as zz

TICK = 0.25
BUFFERS = (5, 10, 15, 20)
TARGETS = ("1R", "2R", "3R", "opposite", "none")
PRE_BARS = 15
AWAY = 10.0
ATR_SESSIONS = 20
COMMISSION = 0.08
SETTINGS = pf.Settings(sigma_multiplier=0.41)
NIGHT_START, CASH_OPEN, CLOSE = 20 * 60, 9 * 60 + 30, 16 * 60
TRACE: list | None = None               # set to a list to record every bar of every process (for drawings)


def part_of(minute_of_day: int) -> str:
    if CASH_OPEN <= minute_of_day < CLOSE:
        return "rth"
    if minute_of_day >= NIGHT_START or minute_of_day < CASH_OPEN:
        return "night"
    return "none"


def main(results: str, data: str, out: str, first_session: str = "2024-01-01", last_session: str = "2025-12-31") -> None:
    started = time.time()
    os.makedirs(out, exist_ok=True)
    sessions, _, bars = rs.load(results)
    spreads = rs.roll_spreads(sessions, data)

    b = zz.prepare_bars(bars).sort_values("minute").reset_index(drop=True)
    b = b[b.session <= pd.Timestamp(last_session)].reset_index(drop=True)
    high, low, close = b.high.to_numpy(float), b.low.to_numpy(float), b.close.to_numpy(float)
    minute = b.minute.to_numpy(np.int64)
    mod = b.minute_of_day.to_numpy()
    symbol = b.instrument_id.to_numpy()
    sess_of = np.array(list(b.session), dtype=object)
    from_start = b.from_start.to_numpy()
    atr1 = pw.atr_ninja(high, low, close, symbol, pw.ATR_PERIOD)
    parts = np.array([part_of(int(m)) for m in mod])

    # 20 session average ATR1 per part of day (previous sessions)
    per = pd.DataFrame({"session": sess_of, "part": parts, "atr1": atr1})
    per = per[per.part != "none"].groupby(["session", "part"]).atr1.mean().unstack()
    avg = per.shift(1).rolling(ATR_SESSIONS, min_periods=10).mean()
    avg_atr = np.array([avg.at[s, p] if p in avg.columns and s in avg.index else np.nan
                        for s, p in zip(sess_of, parts)])

    swings = zz.swings(b)
    swings = swings[swings.multiplier == 0.5]
    confirmed_at: dict[int, list] = {}
    for _, s in swings.iterrows():
        confirmed_at.setdefault(int(s.confirm_index), []).append((s.type, float(s.price), sess_of[int(s["index"])]))
    session_index = {s: i for i, s in enumerate(sorted(set(sess_of)))}

    minutes = sw.MinuteProfiles(data)
    configs = [(bf, tg) for bf in BUFFERS for tg in TARGETS]
    positions: dict = {}                 # (configuration, process id) -> open position
    trades = []
    live: list[dict] = []
    proc = None
    rows = None
    process_id = 0
    first = pd.Timestamp(first_session)
    session_first = 0

    def close_position(c, pos, g, price, reason):
        side = pos["side"]
        slip = TICK if reason == "stop" else 0.0
        exit_price = price + slip * (-side)
        r = ((exit_price - pos["entry"]) * side - COMMISSION) / pos["risk"]
        trades.append({**pos, "buffer": c[0][0], "target_type": c[0][1], "exit_minute": int(minute[g]), "exit": exit_price,
                       "reason": reason, "r": r, "mfe_r": pos["mfe"] / pos["risk"], "mae_r": pos["mae"] / pos["risk"]})
        del positions[c]

    for g in range(len(b)):
        s = sess_of[g]
        new_session = g == 0 or s != sess_of[g - 1]
        for typ, price, swing_session in confirmed_at.get(g - 1, []):
            live.append({"type": typ, "price": price, "session": swing_session})
        if new_session:
            session_first = g
            if s in spreads:
                for p in live:
                    p["price"] += spreads[s]
            proc = None
            rows = minutes.rows(int(symbol[g]), int(minute[g]),
                                int(minute[g]) + 24 * 3600) if s >= first else None
        trading = s >= first and parts[g] != "none"
        # the last bar before 16:00 (22 hours after the 18:00 session start)
        last_bar = from_start[g] < 22 * 60 and (g + 1 == len(b) or sess_of[g + 1] != s or from_start[g + 1] >= 22 * 60)
        if not trading:
            proc = None

        # exits of the open positions on this bar
        for c, pos in list(positions.items()):
            side = pos["side"]
            fav = (high[g] - pos["entry"]) if side == 1 else (pos["entry"] - low[g])
            adv = (pos["entry"] - low[g]) if side == 1 else (high[g] - pos["entry"])
            pos["mfe"] = max(pos["mfe"], fav)
            pos["mae"] = max(pos["mae"], adv)
            hit_stop = low[g] <= pos["stop"] if side == 1 else high[g] >= pos["stop"]
            hit_target = pos["target"] is not None and (high[g] >= pos["target"] if side == 1 else low[g] <= pos["target"])
            if hit_stop:
                close_position(c, pos, g, pos["stop"], "stop")
            elif hit_target:
                close_position(c, pos, g, pos["target"], "target")
            elif last_bar:
                close_position(c, pos, g, close[g], "close")

        # touches of live points: the highest high / the lowest low; every touched point dies
        touched_high = [p for p in live if p["type"] == "high" and high[g] >= p["price"]]
        touched_low = [p for p in live if p["type"] == "low" and low[g] <= p["price"]]
        if touched_high or touched_low:
            live = [p for p in live if p not in touched_high and p not in touched_low]
            if trading and rows is not None and not last_bar:
                if touched_high and (not touched_low or close[g] <= b.open.iat[g]):
                    p, side = max(touched_high, key=lambda p: p["price"]), -1
                else:
                    p, side = min(touched_low, key=lambda p: p["price"]), 1
                process_id += 1
                start = max(g - PRE_BARS, session_first)
                sg = 1 if side == -1 else -1                # short space: a low is negated
                um = rows.utc_minute.to_numpy(np.int64)
                ticks = np.round(rows.price.to_numpy(float) * sg / TICK).astype(np.int64)
                base = int(ticks.min()) if len(ticks) else 0
                proc = {"id": process_id, "side": side, "sg": sg, "level": p["price"], "touch": g, "start": start,
                        "age": session_index[s] - session_index[p["session"]], "armed": True, "freeze": None,
                        "entries": 0, "um": um, "ticks": ticks, "vol": rows.volume.to_numpy(float), "base": base,
                        "acc": np.zeros((int(ticks.max()) - base + 1) if len(ticks) else 1), "avg": avg_atr[g]}
                for k in range(start, g):
                    add_bar(proc, minute[k])

        if proc is not None and trading:
            sg, side = proc["sg"], proc["side"]
            hx_now = high[g] if side == -1 else -low[g]
            ext = (high[proc["start"]:g + 1].max() if side == -1 else -low[proc["start"]:g + 1].min())
            if not proc["armed"] and hx_now > proc["freeze"]:
                proc["armed"] = True
            nz = np.flatnonzero(proc["acc"])
            hvn = sw.top_hvn((nz + proc["base"]) * TICK, proc["acc"][nz], -1) if len(nz) >= 3 else None
            cx = close[g] * sg
            signal = proc["armed"] and hvn is not None and cx < hvn[0] and not last_bar
            if TRACE is not None:
                band = (None, None) if hvn is None else sorted((hvn[0] * sg, hvn[1] * sg))
                TRACE.append({"process": proc["id"], "side": side, "level": proc["level"],
                              "start_minute": int(minute[proc["start"]]), "touch_minute": int(minute[proc["touch"]]),
                              "minute": int(minute[g]), "hvn_low": band[0], "hvn_high": band[1],
                              "armed": proc["armed"], "signal": signal, "extreme": ext * sg})
            if signal:
                proc["armed"], proc["freeze"] = False, ext
                proc["entries"] += 1
                extreme = ext * sg
                for cfg in configs:
                    c = (cfg, proc["id"])
                    if c in positions:          # this process still holds a position in this configuration
                        continue
                    stop = extreme - side * cfg[0] * TICK
                    entry = close[g]
                    risk = (entry - stop) * side
                    if not risk > 0:
                        continue
                    if cfg[1] in ("1R", "2R", "3R"):
                        target = entry + side * int(cfg[1][0]) * risk
                    elif cfg[1] == "opposite":
                        opp = [q["price"] for q in live if q["type"] == ("high" if side == 1 else "low")
                               and (q["price"] - entry) * side > 0]
                        target = (min(opp) if side == 1 else max(opp)) if opp else None
                    else:
                        target = None
                    positions[c] = {"process": proc["id"], "n_in_process": proc["entries"], "session": s.date(),
                                    "year": s.year, "part": parts[g], "side": side, "level": proc["level"],
                                    "age": proc["age"], "touch_minute": int(minute[proc["touch"]]),
                                    "entry_minute": int(minute[g]), "wait_bars": g - proc["touch"], "entry": entry,
                                    "extreme": extreme, "stop": stop, "risk": risk, "target": target,
                                    "avg_atr1": proc["avg"], "risk_atr": risk / proc["avg"],
                                    "beyond_atr": (extreme - proc["level"]) * -side / proc["avg"],
                                    "entry_from_level_atr": (proc["level"] - entry) * -side / proc["avg"],
                                    "mfe": 0.0, "mae": 0.0}
            add_bar(proc, minute[g])
            away = AWAY * proc["avg"]
            if last_bar or (not np.isnan(away) and (high[g] >= proc["level"] + away or low[g] <= proc["level"] - away)):
                proc = None

        if new_session and session_index[s] % 25 == 0:
            print(f"{s.date()} {len(trades)} trades, {len(live)} live points, {time.time() - started:.0f} s", flush=True)

    t = pd.DataFrame(trades)
    t.to_csv(os.path.join(out, "trades.csv.gz"), index=False)
    n_sessions = pd.Series([s for s in set(sess_of) if s >= first]).dt.year.value_counts().to_dict()
    with open(os.path.join(out, "summary.md"), "w", encoding="utf-8") as f:
        f.write(summary(t, n_sessions))
    print(f"{len(t)} trade rows, {time.time() - started:.0f} s")


def add_bar(proc: dict, m: int) -> None:
    um = proc["um"]
    lo, hi = np.searchsorted(um, m), np.searchsorted(um, m + 1)
    np.add.at(proc["acc"], proc["ticks"][lo:hi] - proc["base"], proc["vol"][lo:hi])


def summary(t: pd.DataFrame, n_sessions: dict) -> str:
    lines = ["# Kék ZigZag-kisöprés helyi profillal (nyers szabály)", "",
             "Short a kék High-oknál, long a Low-oknál (a szabály a CLAUDE.md-ben és a szkript fejében). Nettó R ± 95% "
             "(seanszonként klaszterezve); stop = a profil-kezdet óta mért szélsőérték + puffer tick; zárás legkésőbb "
             "16:00. nyerés = R > 0.", ""]
    if t.empty:
        return "\n".join(lines) + "\nNincs kötés.\n"
    t = t.assign(weight=1.0)
    for part in ("night", "rth"):
        lines += [f"## {'Éjszaka' if part == 'night' else 'RTH'} (a belépés szerint)", "",
                  "| puffer | cél | év | kötés | / nap | nyerés | stop | átlag R | össz R |", "|---|---|---|---|---|---|---|---|---|"]
        for bf in BUFFERS:
            for tg in TARGETS:
                for y in sorted(t.year.unique()):
                    g = t[(t.part == part) & (t.buffer == bf) & (t.target_type == tg) & (t.year == y)]
                    if g.empty:
                        continue
                    m, band = pt.weighted(g, "r")
                    lines.append(f"| {bf} | {tg} | {y} | {len(g)} | {len(g) / n_sessions.get(y, 1):.2f} | "
                                 f"{(g.r > 0).mean():.0%} | {(g.reason == 'stop').mean():.0%} | {m:+.3f} ±{band:.3f} | "
                                 f"{g.r.sum():+.0f} |")
        lines.append("")
    base = t[(t.buffer == 5) & (t.target_type == "none")]
    lines += ["## Bontás (puffer 5 tick, cél nélkül, zárás 16:00-kor)", "", "Szint kora (seansz):", "",
              "| rész | kor | év | kötés | átlag R |", "|---|---|---|---|---|"]
    bins = [-1, 0, 1, 5, 10, 20, 60, 10_000]
    labels = ["0", "1", "2-5", "6-10", "11-20", "21-60", ">60"]
    base = base.assign(age_bin=pd.cut(base.age, bins, labels=labels))
    for (part, ab, y), g in base.groupby(["part", "age_bin", "year"], observed=True):
        m, band = pt.weighted(g, "r")
        lines.append(f"| {part} | {ab} | {y} | {len(g)} | {m:+.3f} ±{band:.3f} |")
    lines += ["", "Hányadik belépés a folyamatban:", "", "| rész | n | év | kötés | átlag R |", "|---|---|---|---|---|"]
    for (part, n, y), g in base.assign(n=base.n_in_process.clip(upper=4)).groupby(["part", "n", "year"]):
        m, band = pt.weighted(g, "r")
        lines.append(f"| {part} | {'4+' if n == 4 else n} | {y} | {len(g)} | {m:+.3f} ±{band:.3f} |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3], *(sys.argv[4:6]))
