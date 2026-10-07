"""The locked rule (confirmation entry) on a simulated Upper HVN and a VAH touch, short from below.

    python plot_rule.py <outDir>

The touch, the entry, the stop and the result come from the event study functions themselves (find_events,
simulate_confirmed) run on the drawn 1 minute bars, so the picture is the tested version. ATR 84 points (2026
median).
"""
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

import event_study as es

ATR = 84.0
INK, MUTED, GRID = "#1f2328", "#6e7781", "#d0d7de"
ZONE, BAND, STOP, CANCEL, ENTRY, TARGET, WINDOW = "#8250df", "#c8b6f2", "#cf222e", "#24292f", "#0969da", "#1a7f37", "#f6f8fa"

# o, h, l, c relative to the zone low: armed far below, rising, touch, wick to the extreme, confirmation close, target
BASE = [(-30, -26, -32, -27), (-27, -22, -29, -23), (-23, -17, -24, -18), (-18, -10, -19, -11), (-11, -5, -12, -6),
        (-6, 1, -7, -1), (-1, None, -3, 1), (1, 3, -2, 0), (0, 1, -6, -5),
        (-5, -3, -10, -9), (-9, -6, -12, -7), (-7, -6, -15, -14), (-14, -12, -19, -18), (-18, -14, -20, -15),
        (-15, -14, -24, -23), (-23, -21, -29, -28), (-28, -26, -32, -31), (-31, -30, -37, -35), (-35, -33, -38, -36)]


def make_bars(zlow, extreme):
    bars = [(o, extreme - zlow if h is None else h, l, c) for o, h, l, c in BASE]
    a = np.array(bars, float) + zlow
    return a[:, 0], a[:, 1], a[:, 2], a[:, 3]


def candles(ax, o, h, l, c, highlight):
    for x in range(len(o)):
        edge = highlight.get(x, "#57606a")
        ax.plot([x, x], [l[x], h[x]], color=edge, lw=1.4 if x in highlight else 1, zorder=4)
        ax.add_patch(plt.Rectangle((x - 0.32, min(o[x], c[x])), 0.64, max(abs(c[x] - o[x]), 0.3),
                                   facecolor="#57606a" if c[x] >= o[x] else "#d0d7de", edgecolor=edge,
                                   lw=2 if x in highlight else 0.8, zorder=5))


def spread(items, gap):
    """Label y positions without overlap."""
    items = sorted(items, key=lambda i: i[0])
    ys = []
    for y, *_ in items:
        ys.append(max(y, ys[-1] + gap) if ys else y)
    shift = (sum(i[0] for i in items) - sum(ys)) / len(ys)
    ys = [y + shift for y in ys]
    for k in range(1, len(ys)):
        ys[k] = max(ys[k], ys[k - 1] + gap)
    return [(y_label, *item) for y_label, item in zip(ys, items)]


def draw(path, name, zlow, zhigh, extreme):
    o, h, l, c = make_bars(zlow, extreme)
    n = len(o)
    idx, sides = es.find_events(h, l, c, np.ones(n, bool), zlow, zhigh, ATR)
    assert len(idx) == 1 and sides[0] == -1, (idx, sides)
    i = int(idx[0])
    res = es.simulate_confirmed(h, l, c, i, -1, zlow, zhigh, ATR)
    assert res is not None
    k = i + res["fill_delay"]
    entry, stop, risk = res["entry"], res["stop"], res["risk"]
    high_since = h[i:k + 1].max()
    target = entry - 2 * risk
    band, rearm = es.TOLERANCE * ATR, es.REARM * ATR
    confirm = zlow - es.CONFIRM_DISTANCE * ATR
    cancel = zhigh + es.ACCEPTANCE * ATR
    exit_bar = k + 1 + int(np.argmax(l[k + 1:] <= target))

    fig, ax = plt.subplots(figsize=(13.5, 8.5), dpi=130)
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")
    x_end = n + 0.5
    ax.axvspan(i - 0.5, k + 0.5, color=WINDOW, zorder=0)
    ax.fill_between([-1, x_end], zlow - band, zhigh + band, color=BAND, alpha=0.5, lw=0, zorder=1)
    if zhigh > zlow:
        ax.fill_between([-1, x_end], zlow, zhigh, color=ZONE, alpha=0.35, lw=0, zorder=2)
    else:
        ax.plot([-1, x_end], [zlow, zlow], color=ZONE, lw=2.5, zorder=2)
    candles(ax, o, h, l, c, {i: ZONE, k: ENTRY, exit_bar: TARGET})

    lines = [(cancel, CANCEL, (0, (6, 3)), 1.6), (stop, STOP, "-", 1.8), (high_since, MUTED, ":", 1.2),
             (confirm, ENTRY, (0, (2, 2)), 1.2), (entry, ENTRY, "-", 1.8), (zlow - rearm, MUTED, (0, (4, 3)), 1.0),
             (target, TARGET, "-", 1.8)]
    for y, color, style, lw in lines:
        ax.plot([-1, x_end], [y, y], color=color, ls=style, lw=lw, zorder=3)
    zone_text = f"{name} zóna  {zlow:.2f} – {zhigh:.2f}" if zhigh > zlow else f"{name} szint  {zlow:.2f}"
    labels = [
        (cancel, CANCEL, f"Elmaradó kötés határa: túlsó szél + 0,30 ATR (+{0.30 * ATR:.1f} pt)  {cancel:.2f}\n"
                         "ha a megerősítés előtt ide ér az ár, nincs kötés"),
        (stop, STOP, f"Stop: legmagasabb ár az érintés óta + 0,05 ATR + 1 tick  {stop:.2f}"),
        (high_since, MUTED, f"Legmagasabb ár az érintéstől a megerősítésig  {high_since:.2f}"),
        (zhigh + band, INK, f"Érintési sáv teteje: túlsó szél + 0,03 ATR (+{band:.1f} pt)  {zhigh + band:.2f}"),
        ((zlow + zhigh) / 2, ZONE, zone_text),
        (zlow - band, INK, f"Érintési sáv alja: közeli szél − 0,03 ATR (−{band:.1f} pt)  {zlow - band:.2f}"),
        (confirm, ENTRY, f"Megerősítés: záróár legfeljebb közeli szél − 0,05 ATR (−{0.05 * ATR:.1f} pt)  {confirm:.2f}"),
        (entry, ENTRY, f"Belépés: a megerősítő perc záróára  {entry:.2f}"),
        (zlow - rearm, MUTED, f"Élesítés: előtte egy gyertya teljesen közeli szél − 0,15 ATR alatt  {zlow - rearm:.2f}"),
        (target, TARGET, f"Cél: belépés − 2R  (1R = {risk:.2f} pt)  {target:.2f}"),
    ]
    span = (cancel + 6) - (target - 8)
    for y_label, y, color, text in spread(labels, span * 0.043):
        ax.annotate(text, (x_end, y), (x_end + 1.0, y_label), va="center", ha="left", fontsize=9.3, color=color,
                    fontweight="bold" if color == ZONE else "normal",
                    arrowprops=dict(arrowstyle="-", color=GRID, lw=0.8, shrinkA=0, shrinkB=0))
    ax.annotate("1. érintés: a gyertya\nbelelóg a sávba", (i, h[i]), (i - 4.5, zhigh + 12), fontsize=9.3, color=ZONE,
                arrowprops=dict(arrowstyle="->", color=ZONE))
    ax.annotate("2. megerősítés: a záróár\na küszöb alatt → belépés", (k + 0.3, entry), (k + 2.2, zhigh + 6),
                fontsize=9.3, color=ENTRY, arrowprops=dict(arrowstyle="->", color=ENTRY))
    ax.annotate("3. cél (2R)", (exit_bar, l[exit_bar]), (exit_bar - 4, target - 6), fontsize=9.3, color=TARGET,
                arrowprops=dict(arrowstyle="->", color=TARGET))
    ax.text(i - 0.4, cancel + 2, f"megerősítésre várás: legfeljebb 15 perc,\nitt a {k - i}. percben jött", fontsize=8.8, color=MUTED)
    ax.annotate("", (n - 0.2, stop), (n - 0.2, entry), arrowprops=dict(arrowstyle="<->", color=STOP, lw=1.2))
    ax.text(n, (stop + entry) / 2, "1R", color=STOP, fontsize=9.3, va="center")
    ax.set_xlim(-1, n + 23)
    ax.set_ylim(target - 8, cancel + 6)
    ax.set_xticks(range(n))
    ax.set_xticklabels([str(x - i) if x >= i else "" for x in range(n)], fontsize=8, color=MUTED)
    ax.set_xlabel("perc az érintéstől", color=MUTED, fontsize=9, loc="left")
    ax.grid(axis="y", color=GRID, lw=0.5)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    ax.tick_params(colors=MUTED)
    ax.set_title(f"{name} (szimulált), megerősítéses belépés: short alulról, 1 perces gyertyák, ATR = {ATR:.0f} pont "
                 f"(2026-os medián)\nAz érintés, a belépés, a stop és az eredmény a teszt függvényeiből "
                 f"(find_events, simulate_confirmed): +{res['r2']:.2f}R nettó", loc="left", color=INK, fontsize=11.5)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


if __name__ == "__main__":
    out = sys.argv[1]
    os.makedirs(out, exist_ok=True)
    draw(os.path.join(out, "rule_upper_hvn.png"), "Upper HVN", 6800.0, 6806.0, 6805.0)
    draw(os.path.join(out, "rule_vah.png"), "VAH", 6850.0, 6850.0, 6853.0)
