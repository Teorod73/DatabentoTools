"""Summary of refine_study.py: second bar entry, structural targets, news windows.

    python analyze_refine.py <refineDir>

Writes summary.md: n / win% / mean net R ± 95% CI per set and target, 2024 and 2025, real and shifted zones.
"""
import os
import sys

import numpy as np
import pandas as pd

from book_analysis import candidate_rule

TARGETS = ("sec_r1", "sec_r2", "sec_r3", "sec_r_zone1", "sec_r_zone2")


def cell(r: pd.Series) -> str:
    r = r.dropna()
    if len(r) < 2:
        return "-"
    win = (r > 0).mean() * 100
    return f"n={len(r)} {win:.0f}% **{r.mean():+.2f}**±{1.96 * r.std() / np.sqrt(len(r)):.2f}"


def table(df: pd.DataFrame, sets: list[tuple[str, pd.Series]], column: str) -> list[str]:
    lines = ["| set | 2024 real | 2025 real | 2024 shifted | 2025 shifted |", "|---|---|---|---|---|"]
    for label, mask in sets:
        cells = [cell(df.loc[mask & (df.year == y) & (df.baseline == b), column])
                 for b in (False, True) for y in (2024, 2025)]
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    return lines + [""]


def main(folder: str) -> None:
    df = pd.read_csv(os.path.join(folder, "refined_events.csv.gz"), parse_dates=["session"])
    df["year"] = df.session.dt.year
    df["sec_filled"] = df.sec_filled.fillna(False).astype(bool)
    keep = candidate_rule(df)
    f = df.sec_filled
    room2 = df.sec_room >= 2
    calm = ~df.news
    sets = [("all", f), ("no news", f & calm), ("news", f & df.news), ("room >= 2R", f & room2),
            ("room < 2R", f & ~room2),
            ("rule", f & keep), ("rule, confluence >= 3", f & keep & (df.confluence >= 3)),
            ("rule, confluence >= 3, no news", f & keep & (df.confluence >= 3) & calm),
            ("rule, confluence >= 3, no news, room >= 2R", f & keep & (df.confluence >= 3) & calm & room2)]

    lines = ["# Refined simulation (second bars, structural targets, news windows; net R)", "",
             "Win% = share of the trades with a positive net result. The rule (book_analysis.candidate_rule) and the",
             "confluence filter were chosen looking at both years: 2025 is not an independent check, 2026 is.", ""]
    real = df[~df.baseline]
    lines += [f"Touches: {len(real)} real, {df.baseline.sum()} shifted; entries {f[~df.baseline].sum()} real.",
              f"Entry delay (s) median {real.sec_entry_delay.median():.0f}, risk (pt) median {real.sec_risk.median():.2f}, "
              f"news touches {real.news.mean() * 100:.1f}%.", "",
              "Minute simulation of the same touches (event study conf_r2), for comparison:", ""]
    lines += table(df.assign(conf_r2=df.conf_r2.where(df.conf_filled)), sets[:1] + sets[5:7], "conf_r2")
    for column in TARGETS:
        lines += [f"## {column}", ""] + table(df, sets, column)
    zrr = real.loc[real.sec_filled, ["sec_zone1_rr", "sec_zone2_rr", "sec_room"]].replace(np.inf, np.nan)
    lines += ["## Structural target distances (R, real entries)", "", "```",
              zrr.describe(percentiles=[0.25, 0.5, 0.75]).round(2).to_string(), "```", ""]
    open(os.path.join(folder, "summary.md"), "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main(sys.argv[1])
