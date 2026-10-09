"""Order flow curves of the ZigZag swing windows and their average shape in normalized time (step 3).

    python pivot_curves.py <pivotsDir> <flowDir> <outDir> [year] [seriesDir]

pivotsDir: windows.csv of pivot_windows.py, flowDir: the pivots/<name>.csv.gz files of DatabentoExtract pivots.
Only the valid windows of the year (default 2024, the search year) are used; 2025 stays for the check.

Every curve is mirrored to the side of the swing: at a high the attackers are the buyers and the defenders the
sellers (the ask side), at a low the other way round. The ratio curves are sums over the last N aggressor contracts
(buy + sell, N = the median aggressor volume of 30 s in the part of day of the base year), so the noise of a ratio
does not depend on how busy the market is; the volume curve is the volume of the last 30 seconds. The seconds with a
crossed book have no curve values.

    delta          (attacker - defender aggressor volume) / (sum)                                         -1..1
    def_cancel     defending side: cancelled / (cancelled + added) inside the band, weighted 1 / (1 + d)    0..1
    att_cancel     the same on the attacking side (the bid at a high)                                       0..1
    def_refill     refill on the defending best price / attacker aggressor volume                           0..
    att_refill     refill on the attacking best price / defender aggressor volume                           0..
    def_hidden     hidden (native iceberg) volume on the defending side / attacker aggressor volume         0..
    att_eff        attacker efficiency: mid move in the attack direction over the last N attacker contracts,
                   in ATR1 (N = median attacker volume of 30 s in the part of day, from the year's windows)
    def_eff        the same for the defenders (mid move against the attack over the last N defender contracts)
    att_large      large attacker series / attacker aggressor volume (threshold per session relative to the
                   previous 60 days, large_limits(); without the series data the fixed 60 RTH / 20 night)   0..1
    def_large      the same for the defenders                                                                0..1
    balance        (defending - attacking) / (sum) of the resting sizes on the same number of price levels  -1..1
                   (at a high the ask from the best ask up to the fixed band top, high + 20 ticks, and the bid on
                   as many levels from the best bid down; a low mirrored; NaN with a crossed book)
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
    "balance": "Könyv balansz szintenként (védő - támadó) / összes",
    "volume": "Agresszív volumen / szokásos",
}


TICK = 0.25


LARGE_LIMITS = [5, 6, 8, 10, 12, 15, 20, 25, 30, 40, 50, 60, 80, 100, 125, 150, 200, 250, 300, 400]
CALIBRATION_FROM = "2025-11-01"     # the last 60 days of the development period
TRAILING_DAYS, MIN_SESSIONS = 60, 20


def part_of_half_hour(et: pd.Series) -> pd.Series:
    minutes = et.dt.hour * 60 + et.dt.minute
    return pd.Series(np.where((minutes >= 9 * 60 + 30) & (minutes < 16 * 60), "rth",
                              np.where((minutes >= 20 * 60) | (minutes < 9 * 60 + 30), "night", "none")), index=et.index)


def large_limits(results: str, series_dir: str) -> pd.DataFrame:
    """The large series threshold of every session and part of day, relative to the previous days.

    The aggressor series of the front contract (DatabentoExtract pivots, series/) give per session and part of day
    the volume by series size. p = the share of the aggressor volume in series of at least LARGE (60 RTH, 20 night)
    over the sessions from CALIBRATION_FROM to the end of 2025 (the user set 60 / 20 for the present market). The
    threshold of a session is the smallest size whose share over the previous TRAILING_DAYS days is at most p,
    rounded (in log) to the nearest of LARGE_LIMITS. Fewer than MIN_SESSIONS earlier sessions: no threshold."""
    sessions = pd.read_csv(os.path.join(results, "sessions.csv"), parse_dates=["session"])
    front = dict(zip(sessions.session.dt.date, sessions.instrument_id))
    parts = []
    for path in sorted(glob.glob(os.path.join(series_dir, "*.csv.gz"))):
        df = pd.read_csv(path)
        et = pd.to_datetime(df.utc_half_hour, unit="s", utc=True).dt.tz_convert("America/New_York")
        df["session"] = (et + pd.Timedelta(hours=6)).dt.date
        df["part"] = part_of_half_hour(et)
        df = df[(df.part != "none") & (df.instrument_id == df.session.map(front))]
        parts.append(df.groupby(["session", "part", "size"]).volume.sum())
    volume = pd.concat(parts).groupby(level=[0, 1, 2]).sum().unstack(fill_value=0)
    volume = volume.reindex(columns=range(1, 401), fill_value=0)

    def share_at_least(v: np.ndarray) -> np.ndarray:
        tail = np.cumsum(v[::-1])[::-1]          # volume of the series of at least each size
        return tail / tail[0]

    limits = {"rth": 60, "night": 20}
    rows = []
    for part in ("night", "rth"):
        v = volume.xs(part, level=1)
        dates = pd.to_datetime(pd.Series(v.index))
        calibration = v[(dates >= CALIBRATION_FROM).to_numpy() & (dates.dt.year == 2025).to_numpy()].sum().to_numpy()
        p = share_at_least(calibration)[limits[part] - 1]
        for i, d in enumerate(dates):
            prior = ((dates >= d - pd.Timedelta(days=TRAILING_DAYS)) & (dates < d)).to_numpy()
            if prior.sum() < MIN_SESSIONS:
                continue
            share = share_at_least(v[prior].sum().to_numpy())
            size = int(np.argmax(share <= p)) + 1
            limit = min(LARGE_LIMITS, key=lambda x: abs(np.log(x) - np.log(size)))
            rows.append({"session": d.date(), "part": part, "share_p": p, "size": size, "large_limit": limit})
    return pd.DataFrame(rows)


def load(pivots: str, flow: str, years, limits: pd.DataFrame | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """limits: large_limits(); then large_buy / large_sell are the volumes of the series above the threshold of the
    window's session and part of day (NaN without a threshold), otherwise the fixed 60 (RTH) / 20 (night)."""
    years = [years] if isinstance(years, int) else list(years)
    windows = pd.read_csv(os.path.join(pivots, "windows.csv"))
    windows = windows[windows.valid & pd.to_datetime(windows.session).dt.year.isin(years)].set_index("window_id")
    if limits is not None:
        key = limits.set_index([limits.session.astype(str), "part"]).large_limit
        windows["large_limit"] = [key.get((s, p), np.nan) for s, p in zip(windows.session.astype(str), windows.part)]
    else:
        windows["large_limit"] = np.where(windows.part == "rth", 60, 20)
    rows = []
    for path in sorted(glob.glob(os.path.join(flow, "*.csv.gz"))):
        df = pd.read_csv(path)
        df = df[df.window_id.isin(windows.index)]
        if not len(df):
            continue
        limit = df.window_id.map(windows.large_limit).to_numpy()
        for side in ("buy", "sell"):
            picked = np.full(len(df), np.nan)
            for value in LARGE_LIMITS:
                column = f"large_{side}_{value}"
                if column in df:
                    picked = np.where(limit == value, df[column].to_numpy(float), picked)
            df[f"large_{side}"] = picked
        rows.append(df.drop(columns=[c for c in df.columns if c.startswith("large_") and c[-1].isdigit()]))
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
    out["att_large"] = pick("large_buy", "large_sell")
    out["def_large"] = pick("large_sell", "large_buy")
    bid = pd.to_numeric(df.bid, errors="coerce").to_numpy(float)
    ask = pd.to_numeric(df.ask, errors="coerce").to_numpy(float)
    mid = np.where(df.crossed.to_numpy() == 1, np.nan, (bid + ask) / 2)
    # the attacking side on as many levels as the defending side has up to the fixed band edge
    out["att_rest_eq"] = np.where(high, pd.to_numeric(df.bid_rest_top, errors="coerce").to_numpy(float),
                                  pd.to_numeric(df.ask_rest_bottom, errors="coerce").to_numpy(float))
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


def volume_window_sums(m: pd.DataFrame, columns: list[str], n_by_part: pd.Series, part: pd.Series) -> pd.DataFrame:
    """Sums of the columns over the last seconds that hold at least N aggressor contracts (buy + sell): for every
    second the shortest run of seconds ending there with N contracts, N per part of day. Whole seconds, so the run
    can hold up to one second of volume more than N. NaN while the window has not traded N contracts yet."""
    sums = {c: np.full(len(m), np.nan) for c in columns}
    volume = (m.att_vol + m.def_vol).to_numpy(float)
    values = {c: m[c].to_numpy(float) for c in columns}
    for wid, idx in m.groupby("window_id", sort=False).indices.items():
        n = n_by_part[part.iat[idx[0]]]
        cum = np.concatenate([[0.0], np.cumsum(volume[idx])])
        end = np.arange(1, len(idx) + 1)
        start = np.searchsorted(cum, cum[end] - n, side="right") - 1      # cum[start] <= cum[end] - n
        ok = cum[end] >= n
        for c in columns:
            cs = np.concatenate([[0.0], np.cumsum(values[c][idx])])
            out = np.full(len(idx), np.nan)
            out[ok] = cs[end[ok]] - cs[start[ok]]
            sums[c][idx] = out
    return pd.DataFrame(sums, index=m.index)


def curves(m: pd.DataFrame, windows: pd.DataFrame, base: np.ndarray | None = None,
           norms: tuple[pd.Series, pd.Series] | None = None) -> pd.DataFrame:
    """base: the rows whose medians give N of the volume windows, normalize the volume and give N of the efficiency
    (default all rows); with more years only the search year should be the base. norms: (N of the volume windows,
    N of the efficiency) per part of day instead of base (other windows, e.g. the candidates, with the N of the
    swing windows); curves.attrs["norms"] keeps the ones used.

    The ratio curves are sums over the last N aggressor contracts (N = the median aggressor volume of 30 s in the
    part of day), not over a fixed time: the noise of a ratio depends on how many contracts it is made of, and a
    fixed time window holds more of them when the market is busier (2025 had about 25% more trades than 2024)."""
    m = m.sort_values(["window_id", "second"]).reset_index(drop=True)
    g = m.groupby("window_id", sort=False)
    roll30 = lambda c: g[c].transform(lambda x: x.rolling(ROLL, min_periods=ROLL).sum())
    w = m.window_id.map(windows.part)
    base = np.ones(len(m), bool) if base is None else base
    vol30 = roll30("att_vol") + roll30("def_vol")
    n_total = vol30[base].groupby(w[base]).median() if norms is None else norms[0]
    columns = ["att_vol", "def_vol", "def_cancel_w1", "def_add_w1", "att_cancel_w1", "att_add_w1", "def_refill",
               "att_refill", "def_hidden", "att_large", "def_large"]
    sums = volume_window_sums(m, columns, n_total, w)
    roll = lambda c: sums[c]
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
    out["att_large"] = ratio(roll("att_large"), att)
    out["def_large"] = ratio(roll("def_large"), dfn)
    rest_d, rest_a = m.def_rest, m.att_rest_eq.where(m.crossed != 1)
    out["balance"] = ratio(rest_d - rest_a, rest_d + rest_a)
    typical = w.map(n_total)
    out["volume"] = vol30 / typical

    # efficiency over a fixed number of contracts (the median 30 s attacker volume of the part of day)
    n_att = roll30("att_vol")[base].groupby(w[base]).median() if norms is None else norms[1]
    atr = m.window_id.map(windows.atr1)
    out["att_eff"] = np.nan
    out["def_eff"] = np.nan
    for wid, idx in g.indices.items():
        part = windows.part[wid]
        mid = m.mid_att.to_numpy()[idx]
        out.loc[idx, "att_eff"] = efficiency(mid, m.att_vol.to_numpy()[idx], n_att[part]) / windows.atr1[wid]
        out.loc[idx, "def_eff"] = -efficiency(mid, m.def_vol.to_numpy()[idx], n_att[part]) / windows.atr1[wid]

    out.loc[m.crossed.to_numpy() == 1, CURVES] = np.nan
    out.attrs["norms"] = (n_total, n_att)
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


def main(pivots: str, flow: str, out_dir: str, year: int = 2024, series: str | None = None) -> None:
    limits = large_limits(os.path.dirname(os.path.abspath(pivots)), series) if series else None
    windows, df = load(pivots, flow, year, limits)
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
    main(sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]) if len(sys.argv) > 4 else 2024,
         sys.argv[5] if len(sys.argv) > 5 else None)
