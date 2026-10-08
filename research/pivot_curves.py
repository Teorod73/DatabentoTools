"""Order flow curves of the ZigZag swing windows and their average shape in normalized time (step 3).

    python pivot_curves.py <pivotsDir> <flowDir> <outDir> [year]

pivotsDir: windows.csv of pivot_windows.py, flowDir: the pivots/<name>.csv.gz files of DatabentoExtract pivots.
Only the valid windows of the year (default 2024, the search year) are used; 2025 stays for the check.

Every curve is mirrored to the side of the swing: at a high the attackers are the buyers and the defenders the
sellers (the ask side), at a low the other way round. Rolling sums over the last 30 seconds (the rows start 30 s
before T0, so the first value at T0 is complete). The seconds with a crossed book have no curve values.

    delta          (attacker - defender aggressor volume) / (sum)                                         -1..1
    def_cancel     defending side: cancelled / (cancelled + added) inside the band, weighted 1 / (1 + d)    0..1
    att_cancel     the same on the attacking side (the bid at a high)                                       0..1
    def_refill     refill on the defending best price / attacker aggressor volume                           0..
    att_refill     refill on the attacking best price / defender aggressor volume                           0..
    def_hidden     hidden (native iceberg) volume on the defending side / attacker aggressor volume         0..
    att_eff        attacker efficiency: mid move in the attack direction over the last N attacker contracts,
                   in ATR1 (N = median attacker volume of 30 s in the part of day, from the year's windows)
    def_eff        the same for the defenders (mid move against the attack over the last N defender contracts)
    att_large      large attacker series (60 RTH / 20 night) / attacker aggressor volume                    0..1
    def_large      the same for the defenders                                                                0..1
    balance        (defending resting - attacking resting) / (sum) inside the band                          -1..1
    volume         aggressor volume of 30 s / its median in the part of day                                 0..

Normalized time: T0 = -1, the swing second Tx = 0, Tend = +1, linear in between on both sides. Each window is
averaged into 40 bins of 0.05, then median and quartiles over the windows per part of day and swing type.
Outputs: curves.csv.gz (window x bin), shape.csv (median, q25, q75), shape.md (consistency table), and one figure
per curve in figures/.
"""
from __future__ import annotations

import glob
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROLL = 30
BINS = np.round(np.arange(-1.0, 1.0001, 0.05), 2)
LARGE = {"rth": 60, "night": 20}
HIGH_COLOR, LOW_COLOR = "#2a78d6", "#eb6834"
CURVES = ["delta", "def_cancel", "att_cancel", "def_refill", "att_refill", "def_hidden",
          "att_eff", "def_eff", "att_large", "def_large", "balance", "volume"]
TITLES = {
    "delta": "Delta% (támadó - védő) / összes",
    "def_cancel": "Védő oldal: kivett / (kivett + betett), 1/(1+d)",
    "att_cancel": "Támadó oldal: kivett / (kivett + betett), 1/(1+d)",
    "def_refill": "Védő refill / támadó volumen",
    "att_refill": "Támadó refill / védő volumen",
    "def_hidden": "Védő natív iceberg / támadó volumen",
    "att_eff": "Támadó hatékonyság (ATR1 / N kontraktus)",
    "def_eff": "Védő hatékonyság (ATR1 / N kontraktus)",
    "att_large": "Nagy támadók aránya",
    "def_large": "Nagy védők aránya",
    "balance": "Könyv balansz (védő - támadó) / összes",
    "volume": "Agresszív volumen / szokásos",
}


def load(pivots: str, flow: str, year: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    windows = pd.read_csv(os.path.join(pivots, "windows.csv"))
    windows = windows[windows.valid & (pd.to_datetime(windows.session).dt.year == year)].set_index("window_id")
    rows = []
    for path in sorted(glob.glob(os.path.join(flow, "*.csv.gz"))):
        df = pd.read_csv(path)
        df = df[df.window_id.isin(windows.index)]
        if len(df):
            rows.append(df)
    return windows, pd.concat(rows, ignore_index=True)


def mirrored(df: pd.DataFrame, windows: pd.DataFrame) -> pd.DataFrame:
    """Attacker / defender columns: at a high the attackers buy and the defenders rest on the ask."""
    high = df.window_id.map(windows.type).eq("high").to_numpy()

    def pick(a: str, b: str) -> np.ndarray:
        return np.where(high, df[a].to_numpy(float), df[b].to_numpy(float))

    out = pd.DataFrame({"window_id": df.window_id, "second": df.second})
    out["att_vol"], out["def_vol"] = pick("buy", "sell"), pick("sell", "buy")
    for side, (ask_side, bid_side) in {"def": ("ask", "bid"), "att": ("bid", "ask")}.items():
        for c in ("add_w1", "cancel_w1", "rest", "refill", "hidden"):
            out[f"{side}_{c}"] = pick(f"{ask_side}_{c}", f"{bid_side}_{c}")
    for limit in (20, 60):
        out[f"att_large_{limit}"] = pick(f"large_buy_{limit}", f"large_sell_{limit}")
        out[f"def_large_{limit}"] = pick(f"large_sell_{limit}", f"large_buy_{limit}")
    bid = pd.to_numeric(df.bid, errors="coerce").to_numpy(float)
    ask = pd.to_numeric(df.ask, errors="coerce").to_numpy(float)
    mid = np.where(df.crossed.to_numpy() == 1, np.nan, (bid + ask) / 2)
    out["mid_att"] = np.where(high, mid, -mid)          # rises when the attack succeeds
    out["crossed"] = df.crossed.to_numpy()
    return out


def efficiency(mid: np.ndarray, volume: np.ndarray, n: float) -> np.ndarray:
    """Mid move over the last n contracts of the volume: for every second the latest earlier second where the
    cumulative volume is at least n lower (fixed volume, not fixed time)."""
    cum = np.cumsum(volume)
    out = np.full(len(mid), np.nan)
    starts = np.searchsorted(cum, cum - n, side="right") - 1
    ok = (starts >= 0) & (cum >= n)
    out[ok] = mid[ok] - mid[starts[ok]]
    return out


def curves(m: pd.DataFrame, windows: pd.DataFrame) -> pd.DataFrame:
    m = m.sort_values(["window_id", "second"]).reset_index(drop=True)
    g = m.groupby("window_id", sort=False)
    roll = lambda c: g[c].transform(lambda x: x.rolling(ROLL, min_periods=ROLL).sum())
    w = m.window_id.map(windows.part)
    att, dfn = roll("att_vol"), roll("def_vol")

    def ratio(a, b):
        return (a / b).where(b > 0)

    out = m[["window_id", "second"]].copy()
    out["delta"] = ratio(att - dfn, att + dfn)
    for side in ("def", "att"):
        c, a = roll(f"{side}_cancel_w1"), roll(f"{side}_add_w1")
        out[f"{side}_cancel"] = ratio(c, c + a)
    out["def_refill"] = ratio(roll("def_refill"), att)
    out["att_refill"] = ratio(roll("att_refill"), dfn)
    out["def_hidden"] = ratio(roll("def_hidden"), att)
    large_att = np.where(w == "rth", roll("att_large_60"), roll("att_large_20"))
    large_def = np.where(w == "rth", roll("def_large_60"), roll("def_large_20"))
    out["att_large"] = ratio(pd.Series(large_att), att)
    out["def_large"] = ratio(pd.Series(large_def), dfn)
    rest_d, rest_a = m.def_rest, m.att_rest
    out["balance"] = ratio(rest_d - rest_a, rest_d + rest_a)
    vol = att + dfn
    typical = vol.groupby(w).transform("median")
    out["volume"] = vol / typical

    # efficiency over a fixed number of contracts (the median 30 s attacker volume of the part of day)
    n_att = att.groupby(w).median()
    atr = m.window_id.map(windows.atr1)
    out["att_eff"] = np.nan
    out["def_eff"] = np.nan
    for wid, idx in g.indices.items():
        part = windows.part[wid]
        mid = m.mid_att.to_numpy()[idx]
        out.loc[idx, "att_eff"] = efficiency(mid, m.att_vol.to_numpy()[idx], n_att[part]) / windows.atr1[wid]
        out.loc[idx, "def_eff"] = -efficiency(mid, m.def_vol.to_numpy()[idx], n_att[part]) / windows.atr1[wid]

    out.loc[m.crossed.to_numpy() == 1, CURVES] = np.nan
    return out


def normalized_time(out: pd.DataFrame, windows: pd.DataFrame) -> pd.Series:
    t0 = out.window_id.map(windows.entry_minute)
    tx = out.window_id.map(windows.swing_second)
    tend = out.window_id.map(windows.end_minute)
    s = out.second
    u = np.where(s <= tx, (s - tx) / (tx - t0).clip(lower=1), (s - tx) / (tend - tx).clip(lower=1))
    return pd.Series(u, index=out.index)


def binned(out: pd.DataFrame, windows: pd.DataFrame) -> pd.DataFrame:
    u = normalized_time(out, windows)
    keep = (u >= -1) & (u <= 1)
    b = np.clip(np.round(np.floor((u[keep] + 1) / 0.05) * 0.05 - 1, 2), -1, 0.95)
    per = out[keep].assign(bin=b).groupby(["window_id", "bin"])[CURVES].mean().reset_index()
    per["type"] = per.window_id.map(windows.type)
    per["part"] = per.window_id.map(windows.part)
    return per


def shape(per: pd.DataFrame) -> pd.DataFrame:
    q = per.groupby(["part", "type", "bin"])[CURVES]
    return pd.concat({"median": q.median(), "q25": q.quantile(0.25), "q75": q.quantile(0.75)}, axis=1)


def consistency(per: pd.DataFrame) -> list[str]:
    """Per curve and part: the median before the high (bins -0.5..0), after (0..0.5), and the share of windows whose
    curve after the high is lower than before (how often the change across the swing has the same sign)."""
    lines = ["| görbe | rész | n | medián előtte (-0,5..0) | medián utána (0..0,5) | utána kisebb (%) |",
             "|---|---|---|---|---|---|"]
    before = per[(per.bin >= -0.5) & (per.bin < 0)].groupby("window_id")[CURVES].mean()
    after = per[(per.bin >= 0) & (per.bin < 0.5)].groupby("window_id")[CURVES].mean()
    part = per.groupby("window_id").part.first()
    for c in CURVES:
        for p in ("night", "rth"):
            ids = part.index[part == p]
            b, a = before.loc[ids, c], after.loc[ids, c]
            ok = b.notna() & a.notna()
            lower = (a[ok] < b[ok]).mean() * 100
            lines.append(f"| {c} | {p} | {ok.sum()} | {b.median():.3f} | {a.median():.3f} | {lower:.0f} |")
    return lines


def plot(sh: pd.DataFrame, curve: str, path: str) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(11, 3.8), sharey=True)
    for ax, part in zip(axes, ("night", "rth")):
        for kind, color, label in (("high", HIGH_COLOR, "High"), ("low", LOW_COLOR, "Low (tükrözve)")):
            if (part, kind) not in sh.index.droplevel(2):
                continue
            s = sh.loc[(part, kind)]
            x = s.index.to_numpy() + 0.025
            ax.fill_between(x, s[("q25", curve)], s[("q75", curve)], color=color, alpha=0.15, linewidth=0)
            ax.plot(x, s[("median", curve)], color=color, linewidth=2, label=label)
        ax.axvline(0, color="#8a8a85", linewidth=1, linestyle="--")
        ax.set_title("éjszaka" if part == "night" else "RTH", fontsize=10, loc="left")
        ax.set_xlabel("normalizált idő (T0 = -1, csúcs = 0, vég = +1)", fontsize=9)
        ax.grid(axis="y", color="#e4e3dc", linewidth=0.6)
        for spine in ("top", "right"):
            ax.spines[spine].set_visible(False)
        ax.tick_params(labelsize=8)
    axes[0].legend(frameon=False, fontsize=8, loc="best")
    fig.suptitle(TITLES[curve] + " — medián és q25-q75", fontsize=11, x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def main(pivots: str, flow: str, out_dir: str, year: int = 2024) -> None:
    windows, df = load(pivots, flow, year)
    c = curves(mirrored(df, windows), windows)
    per = binned(c, windows)
    sh = shape(per)
    os.makedirs(os.path.join(out_dir, "figures"), exist_ok=True)
    per.to_csv(os.path.join(out_dir, "curves.csv.gz"), index=False)
    sh.to_csv(os.path.join(out_dir, "shape.csv"))
    for curve in CURVES:
        plot(sh, curve, os.path.join(out_dir, "figures", f"{curve}.png"))

    counts = per.groupby(["part", "type"]).window_id.nunique()
    lines = [f"# Görbék a fordulók körül ({year}, normalizált idő)", "",
             "Ablakok: " + ", ".join(f"{p} {t}: {n}" for (p, t), n in counts.items()), "",
             "A görbék a csúcs oldalára tükrözve: High-nál a támadók a vevők, a védők az eladók (Ask oldal).", "",
             "## Változás a csúcson át", ""] + consistency(per)
    with open(os.path.join(out_dir, "shape.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]) if len(sys.argv) > 4 else 2024)
