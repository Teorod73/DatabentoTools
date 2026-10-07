"""Order book and trade flow features of the zone touches against the trade results.

    python book_analysis.py <resultsDir> <outDir>

Joins results/book/*.csv (DatabentoExtract book) to the event study rows and writes the feature table
(book_events_features.csv.gz) and summary.md: per feature quintile the confirmation entry result (conf_r2) on the
real and on the shifted zones, separately for 2024 (where a rule may be chosen) and 2025 (where it is checked).

Feature timing: pre_* and *_t0 are known before the touch minute, *_1 (the touch minute) are known at the
confirmation entry (the earliest entry is the close of the touch minute), *_3 only if the entry is 3 minutes later.
"""
import glob
import os
import sys

import numpy as np
import pandas as pd

from export_book_events import event_ids

TICK = 0.25


def load(results: str) -> pd.DataFrame:
    events = pd.read_csv(os.path.join(results, "events", "events.csv.gz"), parse_dates=["session"])
    events = events[events.complete & ~events.bucket.str.startswith("7") & (events.weighting != "w")].copy()
    events["event_id"] = event_ids(events)
    frames = [pd.read_csv(f) for f in sorted(glob.glob(os.path.join(results, "book", "*.csv")))]
    book = pd.concat([f for f in frames if len(f)], ignore_index=True)
    return events.merge(book, on="event_id", how="inner")


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    long = df.trade_side == "long"
    sign = np.where(long, 1.0, -1.0)
    out = df.copy()
    # penetration through the near edge in ticks (from the trades of the touch minute)
    out["penetration_1"] = np.where(long, (df.zone_high - df.min_price_1), (df.max_price_1 - df.zone_low)) / TICK
    out["penetration_3"] = np.where(long, (df.zone_high - df.min_price_3), (df.max_price_3 - df.zone_low)) / TICK
    volume_1 = df.buy_1 + df.sell_1
    out["volume_1"] = volume_1
    # delta in the trade direction, pre delta in the attack direction
    out["delta_1"] = sign * (df.buy_1 - df.sell_1) / volume_1.replace(0, np.nan)
    pre = df.pre_buy + df.pre_sell
    out["pre_attack_delta"] = -sign * (df.pre_buy - df.pre_sell) / pre.replace(0, np.nan)
    out["imbalance_t0"] = sign * (df.bids_t0 - df.asks_t0) / (df.bids_t0 + df.asks_t0).replace(0, np.nan)
    out["attack_share_1"] = df.att_vol_1 / volume_1.replace(0, np.nan)
    out["attack_vs_resting_1"] = df.att_vol_1 / (df.resting_def_t0 + 1)
    out["absorption_1"] = df.att_vol_1 / (out.penetration_1.clip(lower=0) + 1)
    out["defense_kept_1"] = df.resting_def_1 / (df.resting_def_t0 + 1)
    out["replenish_ratio_1"] = df.replenished_1 / (df.att_vol_1 + 1)
    out["pulled_ratio_1"] = df.cancelled_def_1 / (df.resting_def_t0 + df.added_def_1 + 1)
    out["added_vs_filled_1"] = df.added_def_1 / (df.filled_def_1 + 1)
    out["large_att_1"] = df.large_att_1
    out["resting_def_t0_n"] = df.resting_def_t0
    out["pre_trades_n"] = df.pre_trades
    out["year"] = df.session.dt.year
    return out


FEATURES = ["pre_attack_delta", "pre_trades_n", "imbalance_t0", "resting_def_t0_n",
            "touch_delay", "volume_1", "delta_1", "attack_share_1", "attack_vs_resting_1", "absorption_1",
            "penetration_1", "defense_kept_1", "replenish_ratio_1", "pulled_ratio_1", "added_vs_filled_1", "large_att_1"]


def result(g: pd.DataFrame, key: str = "conf") -> tuple[int, float, float, float]:
    f = g[g[f"{key}_filled"]]
    if len(f) == 0:
        return 0, np.nan, np.nan, np.nan
    r = f[f"{key}_r2"]
    return len(f), (r >= 1.9).mean() * 100, r.mean(), 1.96 * r.std() / np.sqrt(len(f))


def quintile_table(df: pd.DataFrame, feature: str, key: str = "conf") -> list[str]:
    lines = [f"### {feature}", "", "| quintile (2024 edges) | 2024 real | 2025 real | 2024 shifted | 2025 shifted |",
             "|---|---|---|---|---|"]
    train = df[(df.year == 2024) & ~df.baseline][feature].dropna()
    if train.nunique() < 5:
        edges = np.unique(train.quantile([0, 0.5, 1]).to_numpy())
    else:
        edges = np.unique(train.quantile(np.linspace(0, 1, 6)).to_numpy())
    edges[0], edges[-1] = -np.inf, np.inf
    bins = pd.cut(df[feature], edges, include_lowest=True)
    for interval, g in df.groupby(bins, observed=True):
        cells = []
        for baseline in (False, True):
            for year in (2024, 2025):
                n, win, e, ci = result(g[(g.year == year) & (g.baseline == baseline)], key)
                cells.append(f"n={n} {win:.0f}% **{e:+.2f}**±{ci:.2f}" if n else "-")
        lines.append(f"| {interval} | {cells[0]} | {cells[1]} | {cells[2]} | {cells[3]} |")
    return lines + [""]


def candidate_rule(df: pd.DataFrame) -> pd.Series:
    """Avoid the touches with a strong attack in the touch minute, many large attacking trades, little added
    defending size compared to the fills, much pulled defense or a strong attack before the touch. The quantile
    thresholds come from the 2024 real touches. Known at the confirmation entry only (touch minute features)."""
    train = df[(df.year == 2024) & ~df.baseline]
    q = lambda column, p: train[column].quantile(p)
    avoid = ((df.attack_vs_resting_1 > q("attack_vs_resting_1", 0.8)) |
             (df.large_att_1 >= 13) |
             (df.pre_attack_delta > q("pre_attack_delta", 0.8)) |
             (df.added_vs_filled_1 < q("added_vs_filled_1", 0.2)) |
             (df.defense_kept_1 < q("defense_kept_1", 0.2)))
    return ~avoid.fillna(False)


def main(results: str, out: str) -> None:
    os.makedirs(out, exist_ok=True)
    df = add_features(load(results))
    df = df[df.good_side]
    df.to_csv(os.path.join(out, "book_events_features.csv.gz"), index=False)
    lines = ["# Book features and the confirmation entry (good side, 2R target, net R)", "",
             f"Touches: {(~df.baseline).sum()} real, {df.baseline.sum()} shifted. Quintile edges from the 2024 real touches.", ""]
    for name, g in (("all", df),):
        lines.append("| | 2024 real | 2025 real | 2024 shifted | 2025 shifted |")
        lines.append("|---|---|---|---|---|")
        cells = []
        for baseline in (False, True):
            for year in (2024, 2025):
                n, win, e, ci = result(g[(g.year == year) & (g.baseline == baseline)])
                cells.append(f"n={n} {win:.0f}% **{e:+.2f}**±{ci:.2f}")
        lines += [f"| {name} | " + " | ".join(cells) + " |", ""]
    keep = candidate_rule(df)
    lines += ["## Candidate rule (confirmation entry)", "",
              "Not avoided: no strong attack in the touch minute (attack_vs_resting_1 <= 2024 q80), fewer than 13",
              "large attacking trades, pre_attack_delta <= q80, added_vs_filled_1 >= q20, defense_kept_1 >= q20.",
              "The features were chosen looking at both years, so 2025 is not an independent check; 2026 is.", "",
              "| set | 2024 real | 2025 real | 2024 shifted | 2025 shifted |", "|---|---|---|---|---|"]
    sets = (("all", np.ones(len(df), bool)), ("not avoided", keep), ("avoided", ~keep),
            ("not avoided, confluence >= 3", keep & (df.confluence >= 3)),
            ("not avoided, confluence >= 3, inside value", keep & (df.confluence >= 3) & (df.open_location == "inside value")))
    for label, mask in sets:
        cells = []
        for baseline in (False, True):
            for year in (2024, 2025):
                n, win, e, ci = result(df[mask & (df.year == year) & (df.baseline == baseline)])
                cells.append(f"n={n} {win:.0f}% **{e:+.2f}**±{ci:.2f}")
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    lines.append("")
    for feature in FEATURES:
        lines += quintile_table(df, feature)
    open(os.path.join(out, "summary.md"), "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print(f"{len(df)} good side touches with book features")


if __name__ == "__main__":
    main(*sys.argv[1:3])
