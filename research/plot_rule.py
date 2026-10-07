"""Schematic of the locked rule on a simulated Upper HVN and a VAH touch (short from below).

    python plot_rule.py <outDir>

ATR 84 points (2026 median): touch band +-0.03 ATR, confirmation close 0.05 ATR beyond the near edge, stop the
extreme + 0.05 ATR + 1 tick, no trade if the price goes 0.30 ATR beyond the far edge before the confirmation.
"""
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ATR, TICK = 84.0, 0.25
INK, MUTED, GRID = "#1f2328", "#6e7781", "#d0d7de"
ZONE, BAND, STOP, CANCEL, ENTRY, TARGET = "#8250df", "#c8b6f2", "#cf222e", "#24292f", "#0969da", "#1a7f37"


def candles(ax, bars):
    for x, (o, h, l, c) in enumerate(bars):
        color = "#57606a" if c >= o else "#afb8c1"
        ax.plot([x, x], [l, h], color="#57606a", lw=1, zorder=3)
        ax.add_patch(plt.Rectangle((x - 0.32, min(o, c)), 0.64, max(abs(c - o), 0.3), color=color, ec="#57606a",
                                   lw=0.8, zorder=4))


def bars_for(near, band, extreme, entry):
    """Approach from below, the touch of the band, a wick to the extreme, the confirmation close, the move down."""
    p = near - 24
    out = []
    for step in (4, 3, 5, 2, 4, 3):
        out.append((p, p + step + 1.5, p - 1.5, p + step)); p += step
    out.append((p, near - band + 0.75, p - 1, near - band - 0.5))            # touch: the high reaches the band
    out.append((near - band - 0.5, extreme, near - band - 1.5, near - 1.0))  # wick to the extreme
    out.append((near - 1.0, near + 0.5, entry - 0.5, entry))                 # confirmation close = entry
    p = entry
    for step in (-4, 2, -6, -3, 1.5, -7, -4, -3, -2):
        out.append((p, max(p, p + step) + 1.2, min(p, p + step) - 1.2, p + step)); p += step
    return out


def spread(items, gap):
    """Label y positions without overlap (items sorted by y, at least gap apart)."""
    items = sorted(items, key=lambda i: i[0])
    ys = []
    for y, *_ in items:
        ys.append(max(y, ys[-1] + gap) if ys else y)
    shift = (sum(i[0] for i in items) - sum(ys)) / len(ys)
    ys = [y + shift for y in ys]
    for k in range(1, len(ys)):
        ys[k] = max(ys[k], ys[k - 1] + gap)
    return [(y_label, *item) for y_label, item in zip(ys, items)]


def draw(path, title, zlow, zhigh, extreme):
    band = 0.03 * ATR
    near, far = zlow, zhigh                      # short from below: the near edge is the zone low
    confirm = near - 0.05 * ATR
    entry = confirm - 0.5                        # the confirmation close
    stop = extreme + 0.05 * ATR + TICK
    cancel = far + 0.30 * ATR
    risk = stop - entry
    target = entry - 2 * risk
    bars = bars_for(near, band, extreme, entry)
    n = len(bars)

    fig, ax = plt.subplots(figsize=(13, 8), dpi=130)
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")
    ax.fill_between([-1, n + 1.5], zlow - band, zhigh + band, color=BAND, alpha=0.5, lw=0, zorder=1)
    if zhigh > zlow:
        ax.fill_between([-1, n + 1.5], zlow, zhigh, color=ZONE, alpha=0.35, lw=0, zorder=2)
    else:
        ax.plot([-1, n + 1.5], [zlow, zlow], color=ZONE, lw=2.5, zorder=2)
    candles(ax, bars)

    x_end = n + 1.5
    lines = [(cancel, CANCEL, (0, (6, 3)), 1.6),
             (stop, STOP, "-", 1.8), (extreme, MUTED, ":", 1.2),
             (confirm, ENTRY, (0, (2, 2)), 1.2), (entry, ENTRY, "-", 1.8), (target, TARGET, "-", 1.8)]
    for y, color, style, lw in lines:
        ax.plot([-1, x_end], [y, y], color=color, ls=style, lw=lw, zorder=5)
    zone_name = title.split(" (")[0]
    labels = [
        (cancel, CANCEL, f"Elmaradó kötés határa: túlsó szél + 0,30 ATR (+{0.30 * ATR:.1f} pt)  {cancel:.2f}\n"
                         "ha a megerősítés előtt ide ér, nincs kötés"),
        (stop, STOP, f"Stop: szélsőérték + 0,05 ATR + 1 tick (+{0.05 * ATR + TICK:.2f} pt)  {stop:.2f}"),
        (extreme, MUTED, f"Szélsőérték az érintés óta  {extreme:.2f}"),
        (zhigh + band, INK, f"Érintési sáv teteje: +0,03 ATR (+{band:.1f} pt)  {zhigh + band:.2f}"),
        ((zlow + zhigh) / 2, ZONE, f"{zone_name} " + (f"zóna  {zlow:.2f} – {zhigh:.2f}" if zhigh > zlow
                                                      else f"szint  {zlow:.2f}")),
        (zlow - band, INK, f"Érintési sáv alja: −0,03 ATR (−{band:.1f} pt)  {zlow - band:.2f}"),
        (confirm, ENTRY, f"Megerősítési küszöb: közeli szél − 0,05 ATR (−{0.05 * ATR:.1f} pt)  {confirm:.2f}"),
        (entry, ENTRY, f"Belépés: a megerősítő perc záróára  {entry:.2f}"),
        (target, TARGET, f"Cél: belépés − 2R (R = {risk:.2f} pt)  {target:.2f}"),
    ]
    span = (cancel + 6) - (target - 6)
    for y_label, y, color, text in spread(labels, span * 0.042):
        ax.annotate(text, (x_end, y), (x_end + 1.2, y_label), va="center", ha="left", fontsize=9.5, color=color,
                    fontweight="bold" if color == ZONE else "normal",
                    arrowprops=dict(arrowstyle="-", color=GRID, lw=0.8, shrinkA=0, shrinkB=0))
    ax.annotate("érintés", (6, near - band + 0.75), (2.0, near + 8), fontsize=9.5, color=INK,
                arrowprops=dict(arrowstyle="->", color=MUTED))
    ax.annotate("megerősítés\n= belépés", (8.3, entry), (11.5, entry + 9), fontsize=9.5, color=INK,
                arrowprops=dict(arrowstyle="->", color=MUTED))
    ax.annotate("", (n + 0.6, stop), (n + 0.6, entry), arrowprops=dict(arrowstyle="<->", color=STOP, lw=1.2))
    ax.text(n + 0.8, (stop + entry) / 2, "1R", color=STOP, fontsize=9.5, va="center")
    ax.set_xlim(-1, n + 22)
    ax.set_ylim(target - 6, cancel + 6)
    ax.set_xticks([])
    ax.grid(axis="y", color=GRID, lw=0.5)
    for side in ("top", "right", "bottom"):
        ax.spines[side].set_visible(False)
    ax.spines["left"].set_color(GRID)
    ax.tick_params(colors=MUTED)
    ax.set_title(f"{title}: short alulról, 1 perces gyertyák, ATR = {ATR:.0f} pont (2026-os medián)", loc="left",
                 color=INK, fontsize=12)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


if __name__ == "__main__":
    out = sys.argv[1]
    os.makedirs(out, exist_ok=True)
    draw(os.path.join(out, "rule_upper_hvn.png"), "Upper HVN (szimulált)", 6800.0, 6806.0, 6805.0)
    draw(os.path.join(out, "rule_vah.png"), "VAH (szimulált)", 6850.0, 6850.0, 6852.0)
