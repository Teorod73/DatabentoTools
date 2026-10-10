"""Separating power of the live template score (AUC), no trades.

    python template_auc.py <resultsDir> <researchDataDir> <outDir>

The candidates and curves of template_diagnostics.py. The templates (median and robust spread per bin, pivot,
resolved and all candidates, per part of day) come from the other year: the 2024 templates score the 2025 candidates
and the reverse (the live version will use rolling 120 day templates).

The live score of a candidate at second t (after the swing, before the window end, so the candidate is still open):
    before the extreme   the bins of -1..0 (T0 to the swing second), known at the confirmation
    after the extreme    the end is unknown: L = r x (length before), r in R_GRID, L > t - swing; the seconds after
                         the swing in bins of 0.05 x L, only the complete ones
    distance of a curve  mean z^2 before and mean z^2 after, equal weight (z = (x - median) / spread)
    distance of a set    mean over its curves; for every template the r with the smallest distance of the set
    score                distance(all) - min(distance(pivot), distance(resolved))
Sets: A order flow, B price (efficiency), C = (A + B) / 2. Evaluated at the confirmation and 30, 60, 120 s later.
AUC weighted by the sample weight: good (pivot + resolved) against invalidated, and pivot against the others; also
per curve (its score with the r of the full set). Decision (fixed before the results, CLAUDE.md): below 0.6 for the
best set at the confirmation and at +60 s in both years: no trading test.
Outputs: scores.csv.gz, auc.csv, summary.md.
"""
from __future__ import annotations

import os
import sys

import warnings

import numpy as np
import pandas as pd

import template_diagnostics as td

R_GRID = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0, 4.0)
DELAYS = (0, 30, 60, 120)
FLOW = ["delta", "def_cancel", "att_cancel", "def_refill", "att_refill", "balance", "volume", "large_delta_post"]
FLOW_RTH = FLOW + ["large_delta"]
PRICE = ["att_eff", "def_eff"]
TEMPLATES = ("pivot", "resolved", "all")
BIN = 0.05
NBIN = 20


def template_arrays(t: pd.DataFrame, curves: list[str]) -> dict:
    """(year, part, class) -> (median, spread), arrays of 40 bins x curves (bins -1..-0.05, then 0..0.95)."""
    out = {}
    for (year, part, cls), g in t.groupby(["year", "part", "cls"]):
        med = g.pivot(index="bin", columns="curve", values="median").reindex(columns=curves)
        s = g.pivot(index="bin", columns="curve", values="s").reindex(columns=curves)
        bins = np.round(np.arange(-1, 1, BIN), 2)
        out[(year, part, cls)] = (med.reindex(bins).to_numpy(float), s.reindex(bins).to_numpy(float))
    return out


def z2(x: np.ndarray, med: np.ndarray, s: np.ndarray) -> np.ndarray:
    """Mean z^2 per curve over the bins with a value (rows: bins)."""
    with np.errstate(invalid="ignore", divide="ignore"):
        z = ((x - med) / s) ** 2
    ok = ~np.isnan(z)
    n = ok.sum(axis=0)
    return np.where(n > 0, np.where(ok, z, 0).sum(axis=0) / np.maximum(n, 1), np.nan)


def window_scores(w, sec: np.ndarray, vals: np.ndarray, tpl: dict, sets: dict, t: int) -> dict:
    """The scores of one window at second t. vals: seconds x curves (all curves of the sets, in order)."""
    t0, tx = float(w.entry_minute), float(w.swing_second)
    pre_len = max(tx - t0, 1.0)
    # bins before the extreme
    pre_mask = (sec >= t0) & (sec <= tx)
    u = (sec[pre_mask] - tx) / pre_len
    b = np.clip(np.floor((u + 1) / BIN).astype(int), 0, NBIN - 1)
    pre = np.full((NBIN, vals.shape[1]), np.nan)
    for k in np.unique(b):
        with np.errstate(invalid="ignore"):
            pre[k] = np.nanmean(vals[pre_mask][b == k], axis=0) if np.any(b == k) else np.nan
    post_mask = (sec > tx) & (sec <= t)
    elapsed = t - tx
    dist = {}           # (template, r) -> distance per curve
    for name in TEMPLATES:
        med, s = tpl[name]
        d_pre = z2(pre, med[:NBIN], s[:NBIN])
        for r in R_GRID:
            length = r * pre_len
            if length <= elapsed:
                continue
            complete = int(np.floor(elapsed / (BIN * length)))
            if complete == 0:
                dist[(name, r)] = d_pre
                continue
            up = (sec[post_mask] - tx) / length
            pb = np.floor(up / BIN).astype(int)
            post = np.full((complete, vals.shape[1]), np.nan)
            for k in range(complete):
                sel = pb == k
                if sel.any():
                    with np.errstate(invalid="ignore"):
                        post[k] = np.nanmean(vals[post_mask][sel], axis=0)
            d_post = z2(post, med[NBIN:NBIN + complete], s[NBIN:NBIN + complete])
            dist[(name, r)] = np.where(np.isnan(d_post), d_pre, np.where(np.isnan(d_pre), d_post, (d_pre + d_post) / 2))
    out = {}
    for set_name, idx in sets.items():
        best = {}
        for name in TEMPLATES:
            cands = [(np.nanmean(d[idx]), r, d) for (n, r), d in dist.items() if n == name and not np.all(np.isnan(d[idx]))]
            if not cands:
                break
            best[name] = min(cands, key=lambda c: c[0])
        if len(best) < len(TEMPLATES):
            out[set_name] = np.nan
            continue
        out[set_name] = best["all"][0] - min(best["pivot"][0], best["resolved"][0])
        if set_name == "all_curves":
            per_curve = best["all"][2] - np.fmin(best["pivot"][2], best["resolved"][2])
            for j, c in zip(idx, per_curve[idx]):
                out[f"curve:{j}"] = c
    return out


def weighted_auc(score: np.ndarray, positive: np.ndarray, weight: np.ndarray) -> float:
    ok = ~np.isnan(score)
    score, positive, weight = score[ok], positive[ok], weight[ok]
    wp, wn = weight[positive].sum(), weight[~positive].sum()
    if wp == 0 or wn == 0:
        return np.nan
    df = pd.DataFrame({"s": score, "wp": np.where(positive, weight, 0.0), "wn": np.where(positive, 0.0, weight)})
    g = df.groupby("s")[["wp", "wn"]].sum().sort_index()
    below_neg = g.wn.cumsum() - g.wn
    return float(((g.wp * (below_neg + 0.5 * g.wn)).sum()) / (wp * wn))


def main(results: str, data: str, out_dir: str) -> None:
    warnings.filterwarnings("ignore", category=RuntimeWarning)
    windows, cur = td.load_candidate_curves(results, data, out_dir)
    curves = list(dict.fromkeys(FLOW_RTH + PRICE))
    per = td.pc.binned(cur, windows, curves)
    per["cls"] = per.window_id.map(windows.cls)
    per["weight"] = per.window_id.map(windows.weight)
    per["year"] = pd.to_datetime(per.window_id.map(windows.session)).dt.year
    per = per[per.part != "none"]
    t = td.templates(per, curves)
    tpl_all = template_arrays(t, curves)

    cur = cur[["window_id", "second"] + curves].sort_values(["window_id", "second"])
    blocks = cur.groupby("window_id", sort=False).indices
    sec_all = cur.second.to_numpy(float)
    vals_all = cur[curves].to_numpy(float)
    rows = []
    for wid, idx in blocks.items():
        w = windows.loc[wid]
        if w.part not in ("night", "rth") or np.isnan(w.swing_second):
            continue
        year = pd.Timestamp(w.session).year
        other = 2025 if year == 2024 else 2024
        tpl = {name: tpl_all.get((other, w.part, name)) for name in TEMPLATES}
        if any(v is None for v in tpl.values()):
            continue
        flow = FLOW_RTH if w.part == "rth" else FLOW
        sets = {"A": [curves.index(c) for c in flow], "B": [curves.index(c) for c in PRICE],
                "all_curves": [curves.index(c) for c in flow + PRICE]}
        for delay in DELAYS:
            t_eval = int(w.confirm_minute) + delay
            if t_eval >= int(w.end_minute):
                continue
            sc = window_scores(w, sec_all[idx], vals_all[idx], tpl, sets, t_eval)
            row = {"window_id": wid, "year": year, "part": w.part, "cls": w.cls, "weight": w.weight, "delay": delay,
                   "A": sc.get("A"), "B": sc.get("B")}
            row["C"] = np.nanmean([row["A"], row["B"]]) if not (pd.isna(row["A"]) and pd.isna(row["B"])) else np.nan
            for k, v in sc.items():
                if k.startswith("curve:"):
                    row[curves[int(k[6:])]] = v
            rows.append(row)
    scores = pd.DataFrame(rows)
    scores.to_csv(os.path.join(out_dir, "scores.csv.gz"), index=False)

    auc_rows = []
    for (year, part, delay), g in scores.groupby(["year", "part", "delay"]):
        good = g.cls.isin(["pivot", "resolved"]).to_numpy()
        piv = (g.cls == "pivot").to_numpy()
        inv = (g.cls == "invalidated").to_numpy()
        w = g.weight.to_numpy(float)
        base = {"year": year, "part": part, "delay": delay, "n": len(g),
                "open_pivot": int(piv.sum()), "open_resolved": int((g.cls == "resolved").sum()),
                "open_invalidated": int(inv.sum())}
        for col in ["A", "B", "C"] + curves:
            if col not in g:
                continue
            s = g[col].to_numpy(float)
            auc_rows.append({**base, "score": col,
                             "auc_good_vs_invalidated": weighted_auc(s, good, w),
                             "auc_pivot_vs_rest": weighted_auc(s, piv, w)})
    auc = pd.DataFrame(auc_rows)
    auc.to_csv(os.path.join(out_dir, "auc.csv"), index=False)
    with open(os.path.join(out_dir, "summary.md"), "w", encoding="utf-8") as f:
        f.write(summary(auc, curves))


def summary(auc: pd.DataFrame, curves: list[str]) -> str:
    lines = ["# Az élő sablon-pontszám szétválasztó ereje (AUC, kereskedés nélkül)", "",
             "Sablonok a másik évből. A = orderflow, B = ár (hatékonyság), C = (A + B) / 2. AUC súlyozva (a nem "
             "ZigZag-minta súlya 10); 0,5 = véletlen. jó = forduló + lezárult nem forduló, rossz = érvénytelenült. "
             "Időpont: másodperc a megerősítés után; csak a még nyitott jelöltek. Döntés: ha a legjobb változat a "
             "megerősítéskor és +60 s-nál mindkét évben 0,6 alatt van, nincs kereskedési teszt.", ""]
    for part in ("night", "rth"):
        lines += [f"## {'Éjszaka' if part == 'night' else 'RTH'}", "",
                  "| év | idő (s) | nyitott: forduló / lezárult / érvénytelenült | A jó-rossz | B jó-rossz | C jó-rossz | "
                  "A forduló-többi | B forduló-többi | C forduló-többi |", "|---|---|---|---|---|---|---|---|---|"]
        g = auc[(auc.part == part) & auc.score.isin(["A", "B", "C"])]
        for (year, delay), h in g.groupby(["year", "delay"]):
            v = h.set_index("score")
            r = h.iloc[0]
            lines.append(f"| {year} | {delay} | {r.open_pivot} / {r.open_resolved} / {r.open_invalidated} | " +
                         " | ".join(f"{v.loc[s, 'auc_good_vs_invalidated']:.3f}" for s in "ABC") + " | " +
                         " | ".join(f"{v.loc[s, 'auc_pivot_vs_rest']:.3f}" for s in "ABC") + " |")
        lines += ["", "Görbénként (jó - rossz AUC, a megerősítéskor / +60 s, 2024 / 2025):", "",
                  "| görbe | 2024 0 s | 2024 60 s | 2025 0 s | 2025 60 s |", "|---|---|---|---|---|"]
        g = auc[(auc.part == part) & auc.score.isin(curves)]
        for c in curves:
            h = g[g.score == c].set_index(["year", "delay"]).auc_good_vs_invalidated
            if h.empty:
                continue
            cell = lambda y, d: f"{h.get((y, d), np.nan):.3f}"
            lines.append(f"| {c} | {cell(2024, 0)} | {cell(2024, 60)} | {cell(2025, 0)} | {cell(2025, 60)} |")
        lines.append("")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3])
