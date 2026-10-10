"""Template diagnostics for the curve shape matching (no trades).

    python template_diagnostics.py <resultsDir> <researchDataDir> <outDir>

The candidates with order flow (pattern_trades.candidate_windows: every ZigZag swing candidate and the 10% sample of
the other candidates, weight 10) in three classes per part of day, the high and the mirrored low together:
    pivot        the candidate is a ZigZag swing
    resolved     not a swing, but it came back 2 x ATR1 without a new extreme
    invalidated  a new extreme came first (a short at the high would be stopped)
and all candidates together (weighted) as the reference. Curves as in pivot_curves.py (volume and efficiency N of
the 2024 swing windows), normalized time per window with its real end (T0 = -1, extreme = 0, end = +1, bins of 0.05).

Per year, part, class, curve and bin the template is the median and the robust spread s = IQR / 1.349 (floor: 10% of
the median s of the curve). The difference of two templates in a bin is d = (median A - median B) / mean(s A, s B).
Reported per curve: the mean |d| before and after the extreme of each class against all candidates, the largest
|d| between the two good classes (pivot, resolved; below 0.25 in every bin = they can be merged), the spread of the
invalidated template over the pivot template after the extreme, and the correlation of the good - all profile between
2024 and 2025 (the stability). A curve is kept (decision fixed before the results) when in 2024 a good class differs
from all candidates by at least 0.1 in some bin and the 2024 / 2025 profile correlation is at least 0.5.
Outputs: templates.csv, separation.csv, summary.md, figures/<curve>.png (2024).
"""
from __future__ import annotations

import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import pattern_trades as pt
import pivot_curves as pc
import pivot_windows as pw

YEARS = (2024, 2025)
CLASSES = ("pivot", "resolved", "invalidated")
NAMES = {"pivot": "forduló", "resolved": "lezárult nem forduló", "invalidated": "érvénytelenült", "all": "összes jelölt"}
COLORS = {"pivot": "#2a78d6", "invalidated": "#eb6834", "resolved": "#1baf7a", "all": "#6f6e69"}
# att_large / def_large are replaced by the large series delta curves (2026-10-10)
CURVES = [c for c in pc.CURVES if c not in ("def_hidden", "att_large", "def_large")] + pc.LARGE_CURVES
MERGE_D = 0.25
KEEP_D = 0.1
KEEP_CORR = 0.5
SPREAD_FLOOR = 0.1


def weighted_quantiles(x: np.ndarray, w: np.ndarray, qs) -> list[float]:
    ok = ~np.isnan(x)
    x, w = x[ok], w[ok]
    if len(x) == 0:
        return [np.nan] * len(qs)
    order = np.argsort(x)
    x, w = x[order], w[order]
    cum = (np.cumsum(w) - 0.5 * w) / w.sum()
    return [float(np.interp(q, cum, x)) for q in qs]


def templates(per: pd.DataFrame) -> pd.DataFrame:
    rows = []
    groups = [(c, per[per.cls == c]) for c in CLASSES] + [("all", per)]
    for cls, g in groups:
        for (year, part, b), h in g.groupby(["year", "part", "bin"]):
            w = h.weight.to_numpy(float)
            for c in CURVES:
                q25, q50, q75 = weighted_quantiles(h[c].to_numpy(float), w, (0.25, 0.5, 0.75))
                rows.append({"year": year, "part": part, "cls": cls, "bin": b, "curve": c, "median": q50,
                             "q25": q25, "q75": q75, "s": (q75 - q25) / 1.349, "n": int(h[c].notna().sum())})
    t = pd.DataFrame(rows)
    floor = t.groupby(["year", "part", "curve"]).s.transform("median") * SPREAD_FLOOR
    t["s"] = np.maximum(t.s, floor)
    return t


def separation(t: pd.DataFrame) -> pd.DataFrame:
    key = ["year", "part", "curve", "bin"]
    wide = t.pivot_table(index=key, columns="cls", values=["median", "s"])
    out = pd.DataFrame(index=wide.index)
    for a, b in (("pivot", "all"), ("resolved", "all"), ("invalidated", "all"), ("pivot", "resolved")):
        out[f"{a}-{b}"] = (wide[("median", a)] - wide[("median", b)]) / ((wide[("s", a)] + wide[("s", b)]) / 2)
    out["spread_inv_piv"] = wide[("s", "invalidated")] / wide[("s", "pivot")]
    return out.reset_index()


def summarize(sep: pd.DataFrame, counts: pd.DataFrame, corr: dict) -> str:
    lines = ["# Sablon-diagnosztika (kereskedés nélkül)", "",
             "Jelöltek C# adattal: minden ZigZag-jelölt és a többi jelölt 10%-os mintája (súly 10). Normalizált idő a "
             "valódi véggel (T0 = -1, csúcs = 0, vég = +1). d = a két sablon mediánjának különbsége a két robusztus "
             "szórás átlagában (binenként). Előtte = a -1..0 binek |d| átlaga, utána = a 0..1 binek |d| átlaga.", "",
             "## Ablakok", "", "| év | rész | forduló | lezárult nem forduló | érvénytelenült |", "|---|---|---|---|---|"]
    for (y, p), r in counts.iterrows():
        lines.append(f"| {y} | {p} | {r.get('pivot', 0)} | {r.get('resolved', 0)} | {r.get('invalidated', 0)} |")
    pre, post = sep.bin < 0, sep.bin >= 0
    for part in ("night", "rth"):
        lines += ["", f"## {'Éjszaka' if part == 'night' else 'RTH'}", "",
                  "| görbe | év | forduló - összes (előtte / utána) | lezárult - összes | érvénytelenült - összes | "
                  "forduló - lezárult max |d| | érvénytelenült / forduló szórás (utána) | profil r 2024-2025 | marad |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for c in CURVES:
            for y in YEARS:
                g = sep[(sep.part == part) & (sep.curve == c) & (sep.year == y)]
                if g.empty:
                    continue
                cell = lambda col: (f"{g.loc[pre[g.index], col].abs().mean():.2f} / "
                                    f"{g.loc[post[g.index], col].abs().mean():.2f}")
                r, keep = corr.get((part, c), (np.nan, False))
                lines.append(
                    f"| {c} | {y} | {cell('pivot-all')} | {cell('resolved-all')} | {cell('invalidated-all')} | "
                    f"{g['pivot-resolved'].abs().max():.2f} | {g.loc[post[g.index], 'spread_inv_piv'].median():.2f} | "
                    + (f"{r:.2f} | {'igen' if keep else 'nem'} |" if y == 2024 else " | |"))
    lines += ["", f"Marad: 2024-ben valamelyik jó sablon legalább egy binben ≥ {KEEP_D} eltérés az összes jelölttől, "
              f"és a jó - összes eltérés-profil (a két jó osztály átlaga, 40 bin) 2024 és 2025 között r ≥ {KEEP_CORR}. "
              f"Összevonható a két jó sablon, ha a forduló - lezárult |d| minden binben < {MERGE_D}."]
    return "\n".join(lines)


def curve_correlations(per: pd.DataFrame) -> list[str]:
    lines = ["", "## Görbék együttmozgása (2024, ablak x bin értékek, |r| ≥ 0,5)", "", "| rész | görbe | görbe | r |",
             "|---|---|---|---|"]
    for part in ("night", "rth"):
        g = per[(per.year == 2024) & (per.part == part)][CURVES]
        r = g.corr()
        for i, a in enumerate(CURVES):
            for b in CURVES[i + 1:]:
                if abs(r.loc[a, b]) >= 0.5:
                    lines.append(f"| {part} | {a} | {b} | {r.loc[a, b]:+.2f} |")
    return lines


def plot(t: pd.DataFrame, curve: str, path: str) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(11, 3.8), sharey=True)
    for ax, part in zip(axes, ("night", "rth")):
        for cls in ("pivot", "resolved", "invalidated", "all"):
            s = t[(t.year == 2024) & (t.part == part) & (t.cls == cls) & (t.curve == curve)].sort_values("bin")
            x = s.bin.to_numpy() + 0.025
            if cls == "all":
                ax.plot(x, s["median"], color=COLORS[cls], linewidth=1.5, linestyle="--", label=NAMES[cls])
                continue
            ax.fill_between(x, s.q25, s.q75, color=COLORS[cls], alpha=0.10, linewidth=0)
            ax.plot(x, s["median"], color=COLORS[cls], linewidth=2, label=NAMES[cls])
        ax.axvline(0, color="#8a8a85", linewidth=1, linestyle=":")
        ax.set_title("éjszaka" if part == "night" else "RTH", fontsize=10, loc="left")
        ax.set_xlabel("normalizált idő (T0 = -1, csúcs = 0, vég = +1)", fontsize=9)
        ax.grid(axis="y", color="#e4e3dc", linewidth=0.6)
        for spine in ("top", "right"):
            ax.spines[spine].set_visible(False)
        ax.tick_params(labelsize=8)
    axes[0].legend(frameon=False, fontsize=8, loc="best")
    fig.suptitle(pc.TITLES[curve] + " — sablonok 2024 (medián, q25-q75)", fontsize=11, x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def main(results: str, data: str, out_dir: str) -> None:
    seq = os.path.join(results, "pivots", "sequences")
    limits = pd.read_csv(os.path.join(seq, "large_limits.csv"))
    norms = pt.norms(results, os.path.join(data, "pivots"), limits)

    cands = pt.candidate_windows(results)
    cands = pw.add_swing_second(cands, data)
    cands["cls"] = np.where(cands["pivot"], "pivot", np.where(cands.resolved, "resolved", "invalidated"))
    os.makedirs(os.path.join(out_dir, "figures"), exist_ok=True)
    cands.assign(valid=cands.swing_second.notna()).to_csv(os.path.join(out_dir, "windows.csv"), index=False)
    frames = []
    for flow in (os.path.join(data, "pivots"), os.path.join(data, "candidates", "pivots")):
        windows, df = pc.load(out_dir, flow, list(YEARS), limits)
        frames.append(df)
    df = pd.concat(frames, ignore_index=True)
    m = pc.mirrored(df, windows).sort_values(["window_id", "second"]).reset_index(drop=True)
    cur = pc.curves(m, windows, norms=norms)
    per = pc.binned(cur, windows, CURVES)
    per["cls"] = per.window_id.map(windows.cls)
    per["weight"] = per.window_id.map(windows.weight)
    per["year"] = pd.to_datetime(per.window_id.map(windows.session)).dt.year
    per = per[per.part != "none"]

    t = templates(per)
    t.to_csv(os.path.join(out_dir, "templates.csv"), index=False)
    sep = separation(t)
    sep.to_csv(os.path.join(out_dir, "separation.csv"), index=False)

    corr = {}
    for part in ("night", "rth"):
        for c in CURVES:
            g = sep[(sep.part == part) & (sep.curve == c)]
            prof = g.assign(good=(g["pivot-all"] + g["resolved-all"]) / 2).pivot(index="bin", columns="year", values="good")
            r = prof[2024].corr(prof[2025])
            d24 = g[g.year == 2024]
            keep = bool(max(d24["pivot-all"].abs().max(), d24["resolved-all"].abs().max()) >= KEEP_D and r >= KEEP_CORR)
            corr[(part, c)] = (r, keep)

    w = windows[windows.index.isin(per.window_id)]
    counts = w.assign(year=pd.to_datetime(w.session).dt.year).groupby(["year", "part", "cls"]).size().unstack(fill_value=0)
    text = summarize(sep, counts, corr) + "\n" + "\n".join(curve_correlations(per)) + "\n"
    with open(os.path.join(out_dir, "summary.md"), "w", encoding="utf-8") as f:
        f.write(text)
    for c in CURVES:
        plot(t, c, os.path.join(out_dir, "figures", f"{c}.png"))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3])
