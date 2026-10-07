"""Summary tables of the event study (events.csv.gz of event_study.py) into summary.md.

Main set: complete sessions, no touch between 16:00 and 18:00 ET, the HVNs with the unweighted classification.
E2R: average net R with a 2R target (± 95% interval), *_base: the same on the randomly shifted zones.
"""
import sys

import numpy as np
import pandas as pd

VARIANTS = {"b05": "limit, stop far edge + 0.05 ATR", "b10": "limit, stop far edge + 0.10 ATR",
            "b20": "limit, stop far edge + 0.20 ATR", "b30": "limit, stop far edge + 0.30 ATR",
            "conf": "confirmation close, stop extreme + 0.05 ATR"}


def summary(g: pd.DataFrame, key: str) -> str:
    f = g[g[f"{key}_filled"]]
    if len(f) == 0:
        return "-"
    r = f[f"{key}_r2"]
    return f"n={len(f)} win {(r >= 1.9).mean() * 100:.1f}% E2R **{r.mean():+.3f}** ±{1.96 * r.std() / np.sqrt(len(f)):.3f}"


def table(lines, data, by, key, title):
    lines += ["", f"## {title}", "", f"| {' / '.join(by)} | real | shifted |", "|---|---|---|"]
    for k, g in data.groupby(by, observed=True):
        real, base = g[~g.baseline], g[g.baseline]
        if real[f"{key}_filled"].sum() >= 80:
            label = " / ".join(map(str, k)) if isinstance(k, tuple) else str(k)
            lines.append(f"| {label} | {summary(real, key)} | {summary(base, key)} |")


def main(path: str, out: str) -> None:
    df = pd.read_csv(path, parse_dates=["session"])
    df = df[df.complete & ~df.bucket.str.startswith("7") & (df.weighting != "w")].copy()
    df["test_b"] = df.test.clip(upper=3)
    df["confluence_b"] = df.confluence.clip(upper=3)
    good = df[df.good_side]
    lines = ["# Event study (development period)", "",
             f"Sessions {df.session.min().date()} - {df.session.max().date()}, touches: {(~df.baseline).sum()} real, "
             f"{df.baseline.sum()} on shifted zones. Good side only below unless noted.", "",
             "## Entry variants (good side)", "", "| variant | real | shifted |", "|---|---|---|"]
    for key, name in VARIANTS.items():
        lines.append(f"| {name} | {summary(good[~good.baseline], key)} | {summary(good[good.baseline], key)} |")
    for by, title in ((["type", "good_side"], "Zone type and side"), (["test_b"], "Test number"),
                      (["confluence_b"], "Other zones within 0.1 ATR"), (["open_location"], "Open location"),
                      (["bucket"], "Time of day"), (["half"], "Half years"),
                      (["type", "test_b"], "Zone type and test number"), (["type", "open_location"], "Zone type and open location")):
        data = df if "good_side" in by else good
        for key in ("conf", "b10"):
            table(lines, data, by, key, f"{title} ({VARIANTS[key]})")
    open(out, "w", encoding="utf-8").write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main(*sys.argv[1:3])
