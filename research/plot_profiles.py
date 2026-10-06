"""Charts of session profiles for the comparison with the NinjaTrader VolumeProfileTool."""
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

import profiles as pr

COLORS = {"upper": "#FF4500", "lower": "#2E8B57", "neutral": "#8E8E8E"}


def plot(profile: pr.Profile, seconds: pd.DataFrame, path: str) -> None:
    bars = seconds[(seconds.session == profile.session) & (seconds.instrument_id == profile.instrument_id)].copy()
    bars["t"] = bars.et.dt.tz_localize(None).dt.floor("5min")
    five = bars.groupby("t").agg(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"))

    fig, (ax_p, ax_c) = plt.subplots(1, 2, figsize=(16, 8), sharey=True, gridspec_kw={"width_ratios": [1, 4]})
    fig.patch.set_facecolor("#1e1e1e")
    for ax in (ax_p, ax_c):
        ax.set_facecolor("#1e1e1e")
        ax.tick_params(colors="#cccccc")
        for h in profile.hvns:
            ax.axhspan(h["low"] - pr.TICK / 2, h["high"] + pr.TICK / 2, color=COLORS[h["kind"]], alpha=0.25, lw=0)
        ax.axhline(profile.poc, color="magenta", ls="--", lw=1.5)
        ax.axhline(profile.vah, color="#66ff66", ls="--", lw=1.2)
        ax.axhline(profile.val, color="#66ff66", ls="--", lw=1.2)

    ax_p.barh(profile.prices, profile.volumes, height=pr.TICK * 0.9, color="#d0d000", alpha=0.8)
    ax_p.plot(profile.smoothed, profile.prices, color="white", lw=1)
    ax_p.invert_xaxis()
    ax_p.set_title(f"{profile.symbol} {profile.session.date()}  vol {profile.volume:,}  rows {len(profile.prices)}", color="white")

    width = pd.Timedelta(minutes=3.5)
    for t, r in five.iterrows():
        color = "#00e000" if r.close >= r.open else "#a020f0"
        ax_c.vlines(t, r.low, r.high, color=color, lw=1)
        ax_c.vlines(t, min(r.open, r.close), max(r.open, r.close), color=color, lw=4)
    text = (f"POC {profile.poc}  VAH {profile.vah}  VAL {profile.val}  H {profile.high}  L {profile.low}\n" +
            "\n".join(f"HVN {h['low']}-{h['high']} {h['kind']}" +
                      (f" (asym {h['asymmetry']:.2f}, overshoot {h['overshoot']:.2f})" if "asymmetry" in h else "")
                      for h in reversed(profile.hvns)))
    ax_c.text(0.01, 0.99, text, transform=ax_c.transAxes, va="top", color="white", fontsize=8, family="monospace")
    ax_c.yaxis.tick_right()
    fig.tight_layout()
    fig.savefig(path, dpi=110, facecolor=fig.get_facecolor())
    plt.close(fig)


if __name__ == "__main__":
    directory, out, *days = sys.argv[1:]
    profiles = {str(p.session.date()): p for p in pr.session_profiles(directory)}
    seconds = pr.add_session(pr.read_extract(directory, "seconds"), "utc_second")
    for day in days:
        plot(profiles[day], seconds, f"{out}/profile_{day}.png")
        print("written", day)
