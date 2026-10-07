"""The locked rule (holdout.py) with one position at a time.

    python position_study.py <resultsDir>

The trades of the locked rule (2024-2025 from book_analysis/book_events_features.csv.gz, 2026 from holdout/) are
simulated again on the 1 minute bars exactly as event_study.simulate_confirmed (2R target), now also with the exit
bar. Per session in the order of the entry (close of the confirmation bar) a trade is taken only if no position is
open: its entry bar must be after the exit bar of the previous trade taken. Real and shifted zones are separate
accounts. Writes holdout/positions.csv.gz and holdout/positions.md.
"""
import glob
import os
import sys

import numpy as np
import pandas as pd

import event_study as es
import reverse_study as rs
import zigzag as zz
from book_analysis import add_features
from holdout import holdout_events, locked_rule


def exit_of(high, low, close, i, side, zlow, zhigh, atr):
    """simulate_confirmed with the 2R target, returning (entry bar, exit bar, r2)."""
    n = len(high)
    near, far = (zhigh, zlow) if side == 1 else (zlow, zhigh)
    extreme = low[i] if side == 1 else high[i]
    for k in range(i, min(n, i + es.CONFIRM_WINDOW)):
        extreme = min(extreme, low[k]) if side == 1 else max(extreme, high[k])
        if (side == 1 and extreme <= far - es.ACCEPTANCE * atr) or (side == -1 and extreme >= far + es.ACCEPTANCE * atr):
            return None
        confirmed = close[k] >= near + es.CONFIRM_DISTANCE * atr if side == 1 else close[k] <= near - es.CONFIRM_DISTANCE * atr
        if confirmed and k + 1 < n:
            entry = close[k]
            stop = extreme - 0.05 * atr - es.TICK if side == 1 else extreme + 0.05 * atr + es.TICK
            risk = abs(entry - stop)
            end = min(n, k + 1 + es.MAX_BARS)
            h, l = high[k + 1:end], low[k + 1:end]
            hit_stop = (l <= stop) if side == 1 else (h >= stop)
            stop_at = int(np.argmax(hit_stop)) if hit_stop.any() else None
            target = entry + side * 2 * risk
            hit_target = (h >= target) if side == 1 else (l <= target)
            target_at = int(np.argmax(hit_target)) if hit_target.any() else None
            if stop_at is not None and (target_at is None or stop_at <= target_at):
                r, out = -1.0 - es.TICK / risk, k + 1 + stop_at
            elif target_at is not None:
                r, out = 2.0, k + 1 + target_at
            else:
                r, out = side * (close[end - 1] - entry) / risk, end - 1
            return k, out, r - es.COMMISSION / risk
    return None


def trades(results: str) -> pd.DataFrame:
    dev = pd.read_csv(os.path.join(results, "book_analysis", "book_events_features.csv.gz"), parse_dates=["session"])
    folder = os.path.join(results, "holdout")
    ev = holdout_events(folder)
    book = pd.concat([f for f in (pd.read_csv(p) for p in sorted(glob.glob(os.path.join(folder, "book", "*.csv"))))
                      if len(f)], ignore_index=True)
    hold = add_features(ev.merge(book, on="event_id", how="inner"))
    df = pd.concat([dev, hold[hold.good_side]], ignore_index=True)
    df["year"] = df.session.dt.year
    return df[locked_rule(df) & df.conf_filled].copy()


def drawdown(r: pd.Series) -> float:
    equity = r.cumsum()
    return float((equity.cummax() - equity).max()) if len(r) else np.nan


def main(results: str) -> None:
    df = trades(results)
    _, _, bars = rs.load(results)
    b = zz.prepare_bars(bars).sort_values("minute").reset_index(drop=True)
    b = b[b.session.isin(set(df.session))]
    rows = []
    for session, day in b.groupby("session", sort=True):
        high, low, close = day.high.to_numpy(float), day.low.to_numpy(float), day.close.to_numpy(float)
        index = {m: j for j, m in enumerate(day.minute_of_day.to_numpy())}
        for _, t in df[df.session == session].iterrows():
            side = 1 if t.trade_side == "long" else -1
            res = exit_of(high, low, close, index[t.minute_of_day], side, t.zone_low, t.zone_high, t.atr)
            if res is None:
                continue
            k, out, r = res
            rows.append({"session": session, "year": t.year, "baseline": t.baseline, "event_id": t.event_id,
                         "type": t.type, "trade_side": t.trade_side, "entry_bar": k, "exit_bar": out,
                         "entry_minute": int(day.minute_of_day.iloc[k]), "r2": r, "conf_r2": t.conf_r2})
    p = pd.DataFrame(rows).sort_values(["baseline", "session", "entry_bar", "event_id"]).reset_index(drop=True)
    mismatch = int((~np.isclose(p.r2, p.conf_r2)).sum())

    taken = np.zeros(len(p), bool)
    for _, g in p.groupby(["baseline", "session"]):
        free_after = -1
        for j, row in g.iterrows():
            if row.entry_bar > free_after:
                taken[j] = True
                free_after = row.exit_bar
    p["taken"] = taken
    p.to_csv(os.path.join(results, "holdout", "positions.csv.gz"), index=False)

    lines = ["# Locked rule, one position at a time (confirmation entry, 2R, net R)", "",
             f"Re-simulated trades: {len(p)}, r2 differing from the event study: {mismatch}.",
             "Interval: 95%, clustered by session. 2026 is the holdout, 2024-2025 the development years.", "",
             "| year | zones | all signals | one position | skipped signals | trades / session | total R | max DD R |",
             "|---|---|---|---|---|---|---|---|"]
    for year in (2024, 2025, 2026):
        for baseline in (False, True):
            g = p[(p.year == year) & (p.baseline == baseline)]
            t = g[g.taken]
            s = t.groupby("session").r2.agg(["sum", "count"])
            ci = 1.96 * s["sum"].std() * np.sqrt(len(s)) / s["count"].sum()
            skipped = g[~g.taken].r2
            lines.append(f"| {year} | {'shifted' if baseline else 'real'} | n={len(g)} **{g.r2.mean():+.2f}** | "
                         f"n={len(t)} {(t.r2 >= 1.9).mean() * 100:.0f}% **{t.r2.mean():+.2f}**±{ci:.2f} | "
                         f"n={len(skipped)} {skipped.mean():+.2f} | {s['count'].mean():.1f} | {t.r2.sum():+.1f} | "
                         f"{drawdown(t.r2):.1f} |")
    t = p[p.taken & ~p.baseline]
    lines += ["", "Real zones, one position, by side:", "", "| year | long | short |", "|---|---|---|"]
    for year in (2024, 2025, 2026):
        cells = [f"n={len(g)} {g.r2.mean():+.2f}" for g in (t[(t.year == year) & (t.trade_side == s)] for s in ("long", "short"))]
        lines.append(f"| {year} | " + " | ".join(cells) + " |")
    lines.append("")
    open(os.path.join(results, "holdout", "positions.md"), "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main(sys.argv[1])
