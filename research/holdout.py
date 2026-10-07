"""The locked rule on the 2026 holdout, run once.

    python holdout.py export <holdoutDir>      (after event_study.py ... <holdoutDir> 2026-12-31)
    python holdout.py evaluate <holdoutDir>    (after DatabentoExtract book on the 2026 files -> <holdoutDir>/book)

The rule, fixed on 2026-10-07 before any 2026 result was seen (thresholds = the 2024 real touch quantiles of
book_analysis.candidate_rule, frozen as numbers):
  - a zone of the event study on its good side (EXPECTED), complete session, not after 17:00, HVNs with the
    unweighted classification (weighting != "w");
  - confluence >= 3 (at least 3 other distinct zones within 0.1 ATR);
  - not avoided: attack_vs_resting_1 <= 0.8242, large_att_1 < 13, pre_attack_delta <= 0.1361,
    added_vs_filled_1 >= 4.835, defense_kept_1 >= 0.7007 (missing values do not avoid);
  - confirmation entry on the 1 minute close (simulate_confirmed), stop beyond the extreme, fixed 2R target,
    net R (conf_r2).
Criteria: pass = mean conf_r2 > 0 on the real zones and above the shifted zones; strong pass = the lower 95%
bound of the real mean > 0. Everything else in the summary is descriptive.
"""
import glob
import os
import sys

import numpy as np
import pandas as pd

from book_analysis import add_features, result
from export_book_events import event_ids
import export_book_events

FIRST = "2026-01-01"
THRESHOLDS = {"attack_vs_resting_1": 0.8241788570963473, "large_att_1": 13, "pre_attack_delta": 0.13614666262423925,
              "added_vs_filled_1": 4.835250704780288, "defense_kept_1": 0.7006544022373603}


def locked_rule(df: pd.DataFrame) -> pd.Series:
    t = THRESHOLDS
    avoid = ((df.attack_vs_resting_1 > t["attack_vs_resting_1"]) | (df.large_att_1 >= t["large_att_1"]) |
             (df.pre_attack_delta > t["pre_attack_delta"]) | (df.added_vs_filled_1 < t["added_vs_filled_1"]) |
             (df.defense_kept_1 < t["defense_kept_1"]))
    return ~avoid.fillna(False) & (df.confluence >= 3)


def holdout_events(folder: str) -> pd.DataFrame:
    ev = pd.read_csv(os.path.join(folder, "events.csv.gz"), parse_dates=["session"])
    ev = ev[(ev.session >= FIRST) & ev.complete & ~ev.bucket.str.startswith("7") & (ev.weighting != "w")].copy()
    ev["event_id"] = event_ids(ev)
    return ev


def export(folder: str) -> None:
    ev = holdout_events(folder)
    path = os.path.join(folder, "holdout_events.csv.gz")
    ev.to_csv(path, index=False)
    export_book_events.main(os.path.dirname(os.path.normpath(folder)), path, os.path.join(folder, "book_events_2026.csv"))


def evaluate(folder: str) -> None:
    ev = holdout_events(folder)
    frames = [pd.read_csv(f) for f in sorted(glob.glob(os.path.join(folder, "book", "*.csv")))]
    book = pd.concat([f for f in frames if len(f)], ignore_index=True)
    df = add_features(ev.merge(book, on="event_id", how="inner"))
    df = df[df.good_side]
    keep = locked_rule(df)
    lines = ["# 2026 holdout of the locked rule (confirmation entry, 2R, net R)", "",
             f"Sessions {df.session.min():%Y-%m-%d} - {df.session.max():%Y-%m-%d}; good side touches with book "
             f"features: {(~df.baseline).sum()} real, {df.baseline.sum()} shifted "
             f"(of {(ev.good_side & ~ev.baseline).sum()} real in the event study).", "",
             "| set | real | shifted |", "|---|---|---|"]
    stats = {}
    for label, mask in (("all good side", np.ones(len(df), bool)), ("confluence >= 3", df.confluence >= 3),
                        ("locked rule", keep)):
        cells = []
        for baseline in (False, True):
            n, win, e, ci = result(df[mask & (df.baseline == baseline)])
            stats[(label, baseline)] = (n, e, ci)
            cells.append(f"n={n} {win:.0f}% **{e:+.2f}**±{ci:.2f}")
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    n, e, ci = stats[("locked rule", False)]
    shifted = stats[("locked rule", True)][1]
    verdict = ("strong pass" if e - ci > 0 else "pass" if e > 0 and e > shifted else "fail")
    lines += ["", f"Verdict: **{verdict}** (real {e:+.3f} ± {ci:.3f}, shifted {shifted:+.3f}).", ""]
    f = df[keep & ~df.baseline & df.conf_filled]
    if len(f):
        lines += ["Locked rule, real, per month:", "", "| month | n | mean R | sum R |", "|---|---|---|---|"]
        for month, g in f.groupby(f.session.dt.to_period("M")):
            lines.append(f"| {month} | {len(g)} | {g.conf_r2.mean():+.2f} | {g.conf_r2.sum():+.1f} |")
        lines.append("")
    open(os.path.join(folder, "summary.md"), "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    {"export": export, "evaluate": evaluate}[sys.argv[1]](sys.argv[2])
