"""Summary tables of the reverse study (swing_hits.csv.gz of reverse_study.py) into summary.md.

Main set: complete sessions, no swing between 16:00 and 18:00 ET, extreme bar volume >= 0.2 x the usual volume.
hit%: swings at the zone, base%: the same with the zones shifted randomly, lift = hit / base, ± is the 95% interval.
"""
import sys

import numpy as np
import pandas as pd


def lift(x: pd.DataFrame, zone: str) -> str:
    h, b = x[f"hit:{zone}"].mean(), x[f"base:{zone}"].mean()
    if b <= 0:
        return "-"
    se = np.sqrt(h * (1 - h) / len(x))
    return f"{h * 100:.1f} / {b * 100:.1f} = **{h / b:.2f}** (±{1.96 * se / b:.2f})"


def main(path: str, out: str) -> None:
    df = pd.read_csv(path, parse_dates=["session"])
    main_set = df[df.complete & ~df.bucket.str.startswith("7") & (df.relative_volume >= 0.2)]
    d = main_set[main_set.tolerance == 0.03]
    zones = sorted(c[4:] for c in df.columns if c.startswith("hit:"))
    lines = ["# Reverse study (development period)", "",
             f"Swings: {d.groupby('multiplier').size().to_dict()}, sessions {d.session.min().date()} - {d.session.max().date()}, tolerance 0.03 ATR", ""]

    lines += ["## All swings by zone", "", "| zone | 0.5 | 0.3 |", "|---|---|---|"]
    for z in zones:
        lines.append(f"| {z} | {lift(d[d.multiplier == 0.5], z)} | {lift(d[d.multiplier == 0.3], z)} |")

    lines += ["", "## Swing highs and lows", "", "| zone | 0.5 high | 0.5 low | 0.3 high | 0.3 low |", "|---|---|---|---|---|"]
    for z in zones:
        cells = [lift(d[(d.multiplier == m) & (d.type == t)], z) for m in (0.5, 0.3) for t in ("high", "low")]
        lines.append(f"| {z} | " + " | ".join(cells) + " |")

    for title, column in (("Half years", "half"), ("Time of day", "bucket"), ("Open location", "open_location")):
        lines += ["", f"## any_prev_profile by {title.lower()}", "", f"| {column} | 0.5 | 0.3 |", "|---|---|---|"]
        for key, g in d.groupby(column):
            lines.append(f"| {key} | n={len(g[g.multiplier == 0.5])} {lift(g[g.multiplier == 0.5], 'any_prev_profile')} | "
                         f"n={len(g[g.multiplier == 0.3])} {lift(g[g.multiplier == 0.3], 'any_prev_profile')} |")

    lines += ["", "## any_prev_profile by tolerance", "", "| tolerance | 0.5 | 0.3 |", "|---|---|---|"]
    for tol, g in main_set.groupby("tolerance"):
        lines.append(f"| {tol} | {lift(g[g.multiplier == 0.5], 'any_prev_profile')} | {lift(g[g.multiplier == 0.3], 'any_prev_profile')} |")

    open(out, "w", encoding="utf-8").write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main(*sys.argv[1:3])
