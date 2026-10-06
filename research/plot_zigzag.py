"""5 minute candles of the front contract with the ZigZag legs, for the comparison with the ZigZagAtr indicator."""
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

import zigzag as zz

COLORS = {0.15: "#9a9a9a", 0.3: "#FFD700", 0.5: "#00BFFF"}
WIDTHS = {0.15: 0.8, 0.3: 1.5, 0.5: 2.5}


def plot(bars: pd.DataFrame, swings: pd.DataFrame, first: str, last: str, path: str) -> None:
    b = zz.prepare_bars(bars)
    b = b[(b.session >= first) & (b.session <= last)].copy()
    b["t"] = b.et.dt.tz_localize(None).dt.floor("5min")
    five = b.groupby("t").agg(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"))
    fig, ax = plt.subplots(figsize=(18, 7))
    fig.patch.set_facecolor("#111111"); ax.set_facecolor("#111111"); ax.tick_params(colors="#cccccc")
    for t, r in five.iterrows():
        c = "#00d000" if r.close >= r.open else "#a020f0"
        ax.vlines(t, r.low, r.high, color=c, lw=0.8)
        ax.vlines(t, min(r.open, r.close), max(r.open, r.close), color=c, lw=2.5)
    sw = swings[(swings.session >= first) & (swings.session <= last)]
    for m, g in sw.groupby("multiplier"):
        g = g.sort_values("time")
        ax.plot(g.time.dt.tz_localize(None), g.price, color=COLORS.get(m, "white"), lw=WIDTHS.get(m, 1), label=f"{m}")
    ax.legend(facecolor="#222222", labelcolor="white")
    ax.set_title(f"ZigZag TimeOfDay {first} - {last}", color="white")
    ax.yaxis.tick_right()
    fig.tight_layout(); fig.savefig(path, dpi=100, facecolor=fig.get_facecolor()); plt.close(fig)


if __name__ == "__main__":
    bars_path, swings_path, out, *pairs = sys.argv[1:]
    bars = pd.read_csv(bars_path, parse_dates=["session"])
    swings = pd.read_csv(swings_path, parse_dates=["session"])
    swings["time"] = pd.to_datetime(swings.time, utc=True).dt.tz_convert(zz.ET)
    for pair in pairs:
        first, last = pair.split(":")
        plot(bars, swings, first, last, f"{out}/zigzag_{first}_{last}.png")
        print("written", pair)
