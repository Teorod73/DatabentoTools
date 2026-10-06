"""Session profiles, levels and 1 minute bars of the front contract for the whole extract.

    python build_sessions.py <extractDir> <outDir>

Output:
    sessions.csv          one row per session: front contract, completeness, volume, high, low, POC, VAH, VAL
    levels.csv            HVNs and LVNs of every session, the HVNs with both classifications (weighted and unweighted)
    bars_1min.csv.gz      1 minute bars of the front contract (UTC minute start), for the ZigZag and the event study
"""
import os
import sys
import time

import pandas as pd

import profiles as pr


def main(directory: str, out: str) -> None:
    os.makedirs(out, exist_ok=True)
    settings = pr.Settings()
    symbols = pr.read_symbols(directory)
    sessions, levels, bars = [], [], []
    started = time.time()

    for session, day, seconds in pr.iter_sessions(directory):
        profile, minute_bars = pr.session_profile(session, day, seconds, symbols, settings)
        if profile is None:
            continue

        sessions.append({
            "session": profile.session.date(), "instrument_id": profile.instrument_id, "symbol": profile.symbol,
            "complete": profile.complete, "start": profile.start, "end": profile.end, "volume": profile.volume,
            "rows": len(profile.prices), "high": profile.high, "low": profile.low,
            "poc": profile.poc, "vah": profile.vah, "val": profile.val,
        })
        for h in profile.hvns:
            levels.append({"session": profile.session.date(), "type": "hvn", "low": h["low"], "high": h["high"],
                           "peak": h["peak"],
                           "kind_w": h["kind_w"], "asymmetry_w": round(h["asymmetry_w"], 4), "overshoot_w": round(h["overshoot_w"], 4),
                           "kind_u": h["kind_u"], "asymmetry_u": round(h["asymmetry_u"], 4), "overshoot_u": round(h["overshoot_u"], 4)})
        for l in profile.lvns:
            levels.append({"session": profile.session.date(), "type": "lvn", "low": l["low"], "high": l["high"]})
        bars.append(minute_bars)

        if len(sessions) % 50 == 0:
            print(f"{len(sessions)} sessions, {profile.session.date()}, {time.time() - started:.0f} s", flush=True)

    pd.DataFrame(sessions).to_csv(os.path.join(out, "sessions.csv"), index=False)
    pd.DataFrame(levels).to_csv(os.path.join(out, "levels.csv"), index=False)
    pd.concat(bars, ignore_index=True).to_csv(os.path.join(out, "bars_1min.csv.gz"), index=False)
    print(f"done: {len(sessions)} sessions, {len(levels)} levels, {time.time() - started:.0f} s")


if __name__ == "__main__":
    main(*sys.argv[1:3])
