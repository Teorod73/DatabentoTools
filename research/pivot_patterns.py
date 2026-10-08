"""Ordered event patterns of the swing windows against random order (step 5).

    python pivot_patterns.py <sequencesDir> <outDir>

Input: events.csv.gz of pivot_sequences.py (first second of every event of every window). Per part of day, the high
and the low together. A pattern A -> B (-> C -> D) holds in a window when all its events happen there in this order
(other events may come in between; events in the same second are not ordered).

The random level comes from permutations (N_PERM, seeded).
    simple   the events of a window in a random order (every window keeps which events it has)
    strict   random order only among the events before the swing and among the events after it
    timing   every event keeps its own timing and frequency, only the windows are mixed: the occurrences (normalized
             time, or none) of each event are shuffled among the windows with a similar number of events (quintiles)
The simple and the strict level are fooled by the typical timing of the events: an event that usually comes early
precedes most others in any order of the rest, so almost every pair passed them (first run). The timing level keeps
that timing and only breaks the dependence between the events, so a pattern passes it only when its events go
together in this order more often than their own timings explain. A pattern passes when its count is above the 99%
quantile of the timing permutations; the other two levels are reported.

Left out: events that happen in at least TRIVIAL% of the windows in both years (a starting state, e.g. the large
series share is 0 at some point in almost every window), and patterns with two events of the same curve (their order is mechanical: a rise before its fall, a peak
after the rise; a crossing or a divergence belongs to the curves it is made of).

Search in 2024: the pairs, then the triples whose three pairs pass, then the quadruples whose two triples (A B C and
B C D) pass; at most MAX_CANDIDATES of the most frequent candidates per length. The check is 2025: the same counts
and permutations on the 2025 windows. Ranking: frequency in 2024, then the earlier completion (the median normalized
time of the last event, T0 = -1, swing 0, end +1).

Outputs: patterns.csv.gz (every candidate with its counts and quantiles), patterns.md (the patterns that pass in 2024
and in 2025).
"""
from __future__ import annotations

import itertools
import os
import sys

import numpy as np
import pandas as pd

N_PERM = 1000
SEED = 20261008
QUANTILE = 0.99
MAX_CANDIDATES = 2000
YEARS = (2024, 2025)
KINDS = ("simple", "strict", "timing")
PASS = "timing"
CURVES = ["delta", "def_cancel", "att_cancel", "def_refill", "att_refill", "def_hidden", "att_eff", "def_eff",
          "att_large", "def_large", "balance", "volume"]
CROSSES = {"cross_eff": ("def_eff", "att_eff"), "cross_large": ("def_large", "att_large"),
           "cross_refill": ("def_refill", "att_refill"), "cross_cancel": ("att_cancel", "def_cancel")}


TRIVIAL = 90
# the curves that follow the price move itself: after the swing they turn because the price turns
PRICE_CURVES = {"delta", "att_eff", "def_eff"}


def curves_of(event: str) -> set[str]:
    if event in CROSSES:
        return set(CROSSES[event])
    if event.startswith("div_"):
        return {event[4:]}
    return {c for c in CURVES if event.startswith(c + "_")}


def independent(chain, events: list[str]) -> bool:
    """No two events of the chain share a curve."""
    seen = set()
    for i in chain:
        c = curves_of(events[i])
        if seen & c:
            return False
        seen |= c
    return True


class Data:
    """Windows x events: the second (NaN when the event does not happen), whether it is after the swing, its u."""

    def __init__(self, ev: pd.DataFrame, events: list[str]):
        self.windows = sorted(ev.window_id.unique())
        self.events = events
        ev = ev[ev.event.isin(events)]
        col = {e: i for i, e in enumerate(events)}
        row = {w: i for i, w in enumerate(self.windows)}
        shape = (len(self.windows), len(events))
        self.second = np.full(shape, np.nan)
        self.u = np.full(shape, np.nan)
        r, c = ev.window_id.map(row).to_numpy(), ev.event.map(col).to_numpy()
        self.second[r, c] = ev.second.to_numpy(float)
        self.u[r, c] = ev.u.to_numpy(float)
        self.present = ~np.isnan(self.second)
        self.after = np.nan_to_num(self.u) >= 0
        count = self.present.sum(axis=1)
        self.stratum = np.searchsorted(np.quantile(count, [0.2, 0.4, 0.6, 0.8]), count, side="right")

    def keys(self, kind: str, k: int) -> np.ndarray:
        """The order keys (inf: the event does not happen): the real normalized times (in a window they are in the
        order of the seconds), or the keys of permutation k."""
        real = np.where(self.present, self.u, np.inf)
        if kind == "real":
            return real
        rng = np.random.default_rng([SEED, k, ["simple", "strict", "timing"].index(kind)])
        if kind == "timing":
            key = np.empty_like(real)
            for s in np.unique(self.stratum):
                rows = np.flatnonzero(self.stratum == s)
                for e in range(real.shape[1]):
                    key[rows, e] = real[rng.permutation(rows), e]
            return key
        key = rng.random(self.present.shape)
        if kind == "strict":
            key = key + self.after
        return np.where(self.present, key, np.inf)


def pair_counts(d: Data, key: np.ndarray) -> np.ndarray:
    present = np.isfinite(key)
    both = present[:, :, None] & present[:, None, :]
    return ((key[:, :, None] < key[:, None, :]) & both).sum(axis=0)


def chain_counts(d: Data, key: np.ndarray, chains: np.ndarray) -> np.ndarray:
    """chains: patterns x length event indices; how many windows have them in this order."""
    present = np.isfinite(key)
    ok = np.ones((len(d.windows), len(chains)), bool)
    for j in range(chains.shape[1]):
        ok &= present[:, chains[:, j]]
        if j:
            ok &= key[:, chains[:, j - 1]] < key[:, chains[:, j]]
    return ok.sum(axis=0)


def completion(d: Data, chains: np.ndarray) -> np.ndarray:
    """Median u of the last event over the windows where the chain holds."""
    key = d.keys("real", 0)
    ok = np.ones((len(d.windows), len(chains)), bool)
    for j in range(chains.shape[1]):
        ok &= d.present[:, chains[:, j]]
        if j:
            ok &= key[:, chains[:, j - 1]] < key[:, chains[:, j]]
    last = d.u[:, chains[:, -1]]
    return np.array([np.median(last[ok[:, i], i]) if ok[:, i].any() else np.nan for i in range(len(chains))])


def null_quantiles(d: Data, chains: np.ndarray | None) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """Mean and QUANTILE of the permutation counts, for the pairs (chains None: E x E) or the given chains."""
    out = {}
    for kind in KINDS:
        counts = []
        for k in range(N_PERM):
            key = d.keys(kind, k)
            counts.append(pair_counts(d, key) if chains is None else chain_counts(d, key, chains))
        counts = np.array(counts)
        out[kind] = (counts.mean(axis=0), np.quantile(counts, QUANTILE, axis=0))
    return out


def evaluate(d: Data, chains: np.ndarray, prefix: str) -> pd.DataFrame:
    real = chain_counts(d, d.keys("real", 0), chains)
    null = null_quantiles(d, chains)
    n = len(d.windows)
    out = pd.DataFrame({f"{prefix}freq": real / n * 100})
    for kind, (mean, q) in null.items():
        out[f"{prefix}{kind}_mean"] = mean / n * 100
        out[f"{prefix}{kind}_q99"] = q / n * 100
        out[f"{prefix}{kind}_pass"] = real > q
    out[f"{prefix}u_done"] = completion(d, chains)
    return out


def search(d: Data) -> list[np.ndarray]:
    """Candidates of every length from the 2024 data: pairs, then extensions of the passing ones."""
    e = len(d.events)
    real = pair_counts(d, d.keys("real", 0))
    null = null_quantiles(d, None)
    passed = real > null[PASS][1]
    for a in range(e):
        for b in range(e):
            if a == b or not independent((a, b), d.events):
                passed[a, b] = False
    pairs = np.array([(a, b) for a in range(e) for b in range(e) if a != b and independent((a, b), d.events)])
    found = [pairs]

    def top(chains: list[tuple]) -> np.ndarray:
        if not chains:
            return np.empty((0, 0), int)
        arr = np.array(chains)
        counts = chain_counts(d, d.keys("real", 0), arr)
        return arr[np.argsort(-counts, kind="stable")[:MAX_CANDIDATES]]

    triples = [(a, b, c) for a, b in np.argwhere(passed) for c in range(e)
               if c not in (a, b) and passed[b, c] and passed[a, c] and independent((a, b, c), d.events)]
    triples = top(triples)
    found.append(triples)
    if len(triples):
        ok = evaluate(d, triples, "")[f"{PASS}_pass"].to_numpy()
        good = {tuple(t) for t in triples[ok]}
        quads = [(a, b, c, x) for a, b, c in good for x in range(e)
                 if x not in (a, b, c) and (b, c, x) in good and independent((a, b, c, x), d.events)]
        found.append(top(quads))
    return found


def main(sequences: str, out_dir: str) -> None:
    ev = pd.read_csv(os.path.join(sequences, "events.csv.gz"))
    rows = []
    for part in ("night", "rth"):
        e = ev[ev.part == part]
        share = e.groupby(["year", "event"]).window_id.nunique().unstack(0).div(e.groupby("year").window_id.nunique())
        trivial = share.index[(share * 100 >= TRIVIAL).all(axis=1)]
        events = sorted(set(e.event) - set(trivial))
        print(part, "trivial events left out:", ", ".join(trivial))
        data = {y: Data(ev[(ev.part == part) & (ev.year == y)], events) for y in YEARS}
        for chains in search(data[2024]):
            if not len(chains):
                continue
            table = pd.concat([evaluate(data[y], chains, f"y{y}_") for y in YEARS], axis=1)
            table.insert(0, "pattern", [" > ".join(events[i] for i in c) for c in chains])
            table.insert(0, "length", chains.shape[1])
            table.insert(0, "part", part)
            table["price_events"] = [sum(bool(curves_of(events[i]) & PRICE_CURVES) for i in c) for c in chains]
            # pairs: the candidates are all pairs, keep the passing ones and the frequent ones for the record
            rows.append(table)
            print(part, chains.shape[1], len(chains), "candidates,", int(table[f"y2024_{PASS}_pass"].sum()), "pass 2024")
    result = pd.concat(rows, ignore_index=True)
    os.makedirs(out_dir, exist_ok=True)
    result.to_csv(os.path.join(out_dir, "patterns.csv.gz"), index=False)
    with open(os.path.join(out_dir, "patterns.md"), "w", encoding="utf-8") as f:
        f.write(report(result, ev))


def report(r: pd.DataFrame, ev: pd.DataFrame) -> str:
    n = ev.groupby(["year", "part"]).window_id.nunique()
    lines = ["# Esemény-sorrend minták (5. lépés)", "",
             "Ablakok: " + ", ".join(f"{y} {p}: {c}" for (y, p), c in n.items()), "",
             f"Átmegy: a valódi darabszám nagyobb a {N_PERM} „időzítés” keverés {QUANTILE:.0%}-os kvantilisénél (minden "
             "esemény megtartja a saját időzítését és gyakoriságát, csak az ablakok keverednek, hasonló eseményszámú "
             "ablakok között). Azonos görbéből két esemény nem lehet egy mintában. gyak. = az ablakok %-a, várt = az "
             "időzítés-keverés átlaga, sima / szigorú = a két régi keverés 99%-os szintje, u = a minta utolsó "
             "eseményének medián normalizált ideje (csúcs = 0).", ""]
    for part in ("night", "rth"):
        p = r[r.part == part]
        lines += [f"## {'Éjszaka' if part == 'night' else 'RTH'}", "",
                  "| hossz | jelölt | átment 2024 | átment 2024 és 2025 |", "|---|---|---|---|"]
        for length, g in p.groupby("length"):
            both = (g[f"y2024_{PASS}_pass"] & g[f"y2025_{PASS}_pass"]).sum()
            lines.append(f"| {length} | {len(g)} | {int(g[f'y2024_{PASS}_pass'].sum())} | {int(both)} |")
        lines.append("")
        ok = p[p[f"y2024_{PASS}_pass"] & p[f"y2025_{PASS}_pass"]]
        for title, sel in (("minden esemény", ok), ("csak könyv- és orderflow-események (delta és hatékonyság nélkül)",
                                                    ok[ok.price_events == 0])):
            lines += [f"### A leggyakoribb minták, amelyek 2024-ben és 2025-ben is átmennek: {title}", "",
                      "| minta | gyak. 2024 | várt 2024 | sima q99 | szigorú q99 | u 2024 | gyak. 2025 | várt 2025 |",
                      "|---|---|---|---|---|---|---|---|"]
            for length in sorted(sel.length.unique(), reverse=True):
                g = sel[sel.length == length].sort_values(["y2024_freq", "y2024_u_done"], ascending=[False, True])
                for _, x in g.head(12).iterrows():
                    lines.append(f"| {x.pattern} | {x.y2024_freq:.1f} | {x[f'y2024_{PASS}_mean']:.1f} | "
                                 f"{x.y2024_simple_q99:.1f} | {x.y2024_strict_q99:.1f} | {x.y2024_u_done:.2f} | "
                                 f"{x.y2025_freq:.1f} | {x[f'y2025_{PASS}_mean']:.1f} |")
            lines.append("")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
