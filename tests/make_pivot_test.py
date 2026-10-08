"""Synthetic MBO file, a pivot windows file and the expected order flow per second, computed independently of the
C# PivotFlowCollector.

    python make_pivot_test.py [seed]  ->  pivot_test.mbo.dbn.zst, pivot_test_windows.csv, pivot_test_expected.csv

Life cycle as on GLBX.MDP3 (see make_book_test.py), events of several records share a timestamp and the last record
of an event has the last flag. Trade bursts of one side within a few ms make the large aggressor series, some trades
have no side. Each window is replayed separately with a plain book (best price by min / max of the levels).
"""
import datetime
import decimal
import random
import sys

import databento_dbn as d
import zstandard

SEED = int(sys.argv[1]) if len(sys.argv) > 1 else 1
random.seed(SEED)
TICK = 0.25
NS = 10**9
LAST = 0x80
SERIES_NS = 10 * 10**6
LIMITS = (20, 60)
day = datetime.date(2024, 3, 5)
start = int(datetime.datetime(2024, 3, 5, tzinfo=datetime.timezone.utc).timestamp()) * NS


class I:
    def __init__(s, a, b, c): s.start_date = a; s.end_date = b; s.symbol = c


class M:
    def __init__(s, r, iv): s.raw_symbol = r; s.intervals = iv


meta = d.Metadata(dataset="GLBX.MDP3", start=start, stype_in=d.SType.PARENT, stype_out=d.SType.INSTRUMENT_ID,
                  schema=d.Schema.MBO, symbols=["ES.FUT"], partial=[], not_found=[],
                  mappings=[M("ESH4", [I(day, day + datetime.timedelta(days=1), "1001")]),
                            M("ESM4", [I(day, day + datetime.timedelta(days=1), "1002")])],
                  end=start + 86400 * NS, version=2)

events = []           # list of (ts, instrument, [records]); record = (action, side, price, size, order_id, flags)
orders = {1001: {}, 1002: {}}
next_id = [1]
ACTION = {"A": d.Action.ADD, "C": d.Action.CANCEL, "M": d.Action.MODIFY, "R": d.Action.CLEAR,
          "T": d.Action.TRADE, "F": d.Action.FILL}
SIDE = {"A": d.Side.ASK, "B": d.Side.BID, "N": d.Side.NONE}


def new_id():
    next_id[0] += 1
    return next_id[0]


mids = {1001: 4750.0, 1002: 4800.0}
ts = start + NS
for iid in (1001, 1002):
    recs = [("R", "N", 0.0, 0, 0, 32)]
    for k in range(1, 8):
        for side, sign in (("B", -1), ("A", 1)):
            oid = new_id()
            price = mids[iid] + sign * k * TICK
            size = random.randint(5, 40)
            orders[iid][oid] = [side, price, size]
            recs.append(("A", side, price, size, oid, 32))
    events.append((ts, iid, recs))


def trade(iid, aggressor, want, recs):
    """One aggressor order: T per level, F per resting order, then C or M per order."""
    book = orders[iid]
    resting = "A" if aggressor == "B" else "B"
    levels = sorted({o[1] for o in book.values() if o[0] == resting}, reverse=(resting == "B"))
    for level in levels[:3]:
        if want <= 0:
            break
        taken = []
        for oid in [oid for oid, o in book.items() if o[0] == resting and o[1] == level]:
            if want <= 0:
                break
            q = min(want, book[oid][2])
            taken.append((oid, q))
            want -= q
        if not taken:
            continue
        recs.append(("T", aggressor, level, sum(q for _, q in taken), 0, 0))
        for oid, q in taken:
            recs.append(("F", resting, level, q, oid, 0))
        for oid, q in taken:
            left = book[oid][2] - q
            if left <= 0:
                recs.append(("C", resting, level, book[oid][2], oid, 0))
                del book[oid]
            else:
                book[oid][2] = left
                recs.append(("M", resting, level, left, oid, 0))
        mids[iid] = level


for step in range(40000):
    ts += random.randint(1, 120) * 10**6
    iid = 1001 if random.random() < 0.9 else 1002
    book = orders[iid]
    recs = []
    r = random.random()
    if r < 0.40 or len(book) < 8:
        for _ in range(random.randint(1, 3)):
            side = random.choice("BA")
            price = mids[iid] + (-1 if side == "B" else 1) * random.randint(0, 8) * TICK
            if side == "B" and any(o[0] == "A" and o[1] <= price for o in book.values()):
                price = mids[iid] - TICK
            if side == "A" and any(o[0] == "B" and o[1] >= price for o in book.values()):
                price = mids[iid] + TICK
            if (side == "B" and any(o[0] == "A" and o[1] <= price for o in book.values())) or \
               (side == "A" and any(o[0] == "B" and o[1] >= price for o in book.values())):
                continue
            oid = new_id()
            size = random.randint(1, 30)
            book[oid] = [side, price, size]
            recs.append(("A", side, price, size, oid, 0))
    elif r < 0.62:
        oid = random.choice(list(book))
        side, price, size = book[oid]
        cut = size if random.random() < 0.6 else random.randint(1, size)
        recs.append(("C", side, price, cut, oid, 0))
        book[oid][2] -= cut
        if book[oid][2] <= 0:
            del book[oid]
    elif r < 0.72:
        oid = random.choice(list(book))
        side, price, size = book[oid]
        if random.random() < 0.5:
            size = random.randint(1, 40)
        else:
            new_price = price + random.choice((-1, 1)) * TICK
            if not ((side == "B" and any(o[0] == "A" and o[1] <= new_price for o in book.values())) or
                    (side == "A" and any(o[0] == "B" and o[1] >= new_price for o in book.values()))):
                price = new_price
        book[oid] = [side, price, size]
        recs.append(("M", side, price, size, oid, 0))
    elif r < 0.74:
        # a trade without aggressor side (e.g. implied)
        recs.append(("T", "N", mids[iid], random.randint(1, 5), 0, 0))
    else:
        aggressor = random.choice("BA")
        trade(iid, aggressor, random.randint(1, 40), recs)
        # a burst of the same side within a few ms (a large series) or a later one
        if random.random() < 0.3:
            for _ in range(random.randint(1, 4)):
                events.append((ts, iid, recs))
                ts += random.randint(1, 6) * 10**6
                recs = []
                trade(iid, aggressor, random.randint(5, 40), recs)
        if random.random() < 0.3 and recs:
            # refill at the traded price
            resting = "A" if aggressor == "B" else "B"
            if not any(o[0] == aggressor and ((o[1] >= mids[iid]) if resting == "A" else (o[1] <= mids[iid]))
                       for o in book.values()):
                oid = new_id()
                size = random.randint(1, 20)
                book[oid] = [resting, mids[iid], size]
                recs.append(("A", resting, mids[iid], size, oid, 0))
    if recs:
        events.append((ts, iid, recs))

records = []          # (ts, iid, action, side, price, size, oid, flags)
for t, iid, recs in events:
    for k, (action, side, price, size, oid, flags) in enumerate(recs):
        records.append((t, iid, action, side, price, size, oid, flags | (LAST if k == len(recs) - 1 else 0)))
records.sort(key=lambda x: x[0])

with open("pivot_test.mbo.dbn.zst", "wb") as f:
    raw = bytearray(bytes(meta))
    for seq, (t, iid, action, side, price, size, oid, flags) in enumerate(records):
        msg = d.MBOMsg(publisher_id=1, instrument_id=iid, ts_event=t, order_id=oid, price=round(price * 1e9),
                       size=size, action=ACTION[action], side=SIDE[side], ts_recv=t + 1000, flags=flags,
                       channel_id=0, ts_in_delta=0, sequence=seq)
        raw += bytes(msg)[:56]
    f.write(zstandard.ZstdCompressor().compress(bytes(raw)))

# ------------------------------------------------------------------------------------------ windows
last_second = records[-1][0] // NS
windows = []
for k in range(12):
    s0 = random.randint(start // NS + 5, last_second - 400)
    length = random.randint(20, 300)
    center = 4750.0 + random.randint(-20, 20) * TICK
    half = random.randint(20, 60) * TICK
    windows.append({"id": f"w{k}", "iid": 1001, "start": s0, "end": s0 + length, "low": center - half, "high": center + half})
windows.append({"id": "wSpread", "iid": 1002, "start": last_second - 100, "end": last_second + 5,
                "low": 4790.0, "high": 4810.0})
with open("pivot_test_windows.csv", "w") as f:
    f.write("window_id,instrument_id,start_second,end_second,band_low,band_high\n")
    for w in windows:
        f.write(f"{w['id']},{w['iid']},{w['start']},{w['end']},{w['low']},{w['high']}\n")


# ------------------------------------------------------------------------------------------ expected
def price_text(p):
    return "" if p is None else f"{p:.9f}".rstrip("0").rstrip(".")


def number(x):
    """As .NET "0.####": 15 significant digits first, then 4 decimals rounded half away from zero."""
    if x == 0:
        return "0"
    exact = decimal.Decimal(x)
    shown = decimal.Context(prec=15, rounding=decimal.ROUND_HALF_EVEN).plus(exact)
    text = f"{shown.quantize(decimal.Decimal('0.0001'), rounding=decimal.ROUND_HALF_UP):f}"
    text = text.rstrip("0").rstrip(".") if "." in text else text
    return "0" if text in ("", "-0") else text


def replay(w):
    book, levels, pending_fill = {}, {"B": {}, "A": {}}, {}
    lo_band, hi_band = w["low"], w["high"]
    half = (hi_band - lo_band) / 2 / TICK
    rows = []
    started = False
    cur = None
    series = None
    pending = False
    last_t = None
    ep = {"A": {"price": None, "q0": 0, "agg": 0, "closed": 0}, "B": {"price": None, "q0": 0, "agg": 0, "closed": 0}}
    written = {"A": 0, "B": 0}
    acc = {}

    def best(side):
        lv = levels[side]
        if not lv:
            return None
        return max(lv) if side == "B" else min(lv)

    def reset_acc():
        acc.clear()
        acc.update(buy=0, sell=0, trades=0, last=None, high=None, low=None,
                   large_buy=[0, 0], large_sell=[0, 0])
        for s in "AB":
            acc[s] = dict(add=0, add1=0.0, addl=0.0, cancel=0, cancel1=0.0, cancell=0.0, fill=0, up=0, down=0)

    def active():
        return started and cur < w["end"]

    def total(side):
        e = ep[side]
        return e["closed"] + max(0, e["agg"] - e["q0"])

    def start_episode(side):
        p = best(side)
        ep[side].update(price=p, q0=levels[side].get(p, 0) if p is not None else 0, agg=0)

    def evaluate():
        if not active():
            return
        for side in "AB":
            p = best(side)
            e = ep[side]
            if p == e["price"]:
                continue
            if e["price"] is not None:
                e["closed"] += max(0, e["agg"] - e["q0"])
                if p is not None:
                    acc[side]["up" if p > e["price"] else "down"] += 1
            e.update(price=p, q0=levels[side].get(p, 0) if p is not None else 0, agg=0)

    def write_row():
        nonlocal cur
        b, a = best("B"), best("A")
        row = [w["id"], cur, acc["buy"], acc["sell"], acc["trades"], price_text(acc["last"]),
               price_text(acc["high"]), price_text(acc["low"]), price_text(b), price_text(a)]
        for side in "AB":
            x = acc[side]
            rest = sum(sz for p, sz in levels[side].items() if lo_band - 1e-9 <= p <= hi_band + 1e-9)
            row += [x["add"], number(x["add1"]), number(x["addl"]), x["cancel"], number(x["cancel1"]),
                    number(x["cancell"]), x["fill"], rest]
        ta, tb = total("A"), total("B")
        row += [ta - written["A"], acc["A"]["up"], acc["A"]["down"], tb - written["B"], acc["B"]["up"], acc["B"]["down"]]
        for k in range(2):
            row += [acc["large_buy"][k], acc["large_sell"][k]]
        row.append(1 if b is not None and a is not None and b >= a else 0)
        rows.append(",".join(map(str, row)))
        written["A"], written["B"] = ta, tb
        reset_acc()
        cur += 1

    def close_series():
        nonlocal series
        if series is None:
            return
        side, first, vol = series
        series = None
        if active():
            for k, limit in enumerate(LIMITS):
                if vol >= limit:
                    acc["large_buy" if side == "B" else "large_sell"][k] += vol

    def resting_change(side, price, change, filled):
        if not active() or (change == 0 and filled == 0):
            return
        if not (lo_band - 1e-9 <= price <= hi_band + 1e-9):
            return
        b = best(side)
        dist = 0 if b is None else max(0, round(((b - price) if side == "B" else (price - b)) / TICK))
        w1 = 1.0 / (1 + dist)
        wl = max(0.0, 1.0 - dist / half)
        x = acc[side]
        if change > 0:
            x["add"] += change
            x["add1"] += change * w1
            x["addl"] += change * wl
        elif change < 0:
            x["cancel"] += -change
            x["cancel1"] += -change * w1
            x["cancell"] += -change * wl
        x["fill"] += filled

    def lv_change(side, price, delta):
        lv = levels[side]
        lv[price] = lv.get(price, 0) + delta
        if lv[price] <= 0:
            del lv[price]

    def add(side, price, size, oid):
        if side not in "AB":
            return
        resting_change(side, price, size, 0)
        book[oid] = [side, price, size]
        pending_fill[oid] = 0
        lv_change(side, price, size)

    reset_acc()
    for (t, iid, action, side, price, size, oid, flags) in records:
        if iid != w["iid"]:
            continue
        if pending and t != last_t:
            evaluate()
            pending = False
        last_t = t
        sec = t // NS
        if not started and w["start"] <= sec:
            started = True
            cur = w["start"]
            start_episode("A")
            start_episode("B")
        if started:
            while cur < sec and cur < w["end"]:
                write_row()
        if series is not None and t - series[1] > SERIES_NS:
            close_series()

        if action == "T":
            if series is not None and series[0] != side:
                close_series()
            if side in "AB":
                if series is not None:
                    series = (series[0], series[1], series[2] + size)
                else:
                    series = (side, t, size)
            if active():
                acc["trades"] += 1
                if side == "B": acc["buy"] += size
                if side == "A": acc["sell"] += size
                acc["last"] = price
                acc["high"] = price if acc["high"] is None else max(acc["high"], price)
                acc["low"] = price if acc["low"] is None else min(acc["low"], price)
                if side in "AB":
                    e = ep["A" if side == "B" else "B"]
                    if e["price"] == price:
                        e["agg"] += size
        elif action == "F":
            if oid in book:
                pending_fill[oid] += size
        elif action == "A":
            add(side, price, size, oid)
        elif action == "C":
            if oid in book:
                s_, p_, z_ = book[oid]
                red = min(z_, size)
                fill = min(pending_fill[oid], red)
                pending_fill[oid] -= fill
                resting_change(s_, p_, -(red - fill), fill)
                book[oid][2] -= red
                lv_change(s_, p_, -red)
                if book[oid][2] <= 0:
                    del book[oid]
        elif action == "M":
            if oid not in book:
                add(side, price, size, oid)
            else:
                s_, p_, z_ = book[oid]
                if p_ == price and s_ == side:
                    delta = size - z_
                    if delta < 0:
                        fill = min(pending_fill[oid], -delta)
                        pending_fill[oid] -= fill
                        resting_change(s_, p_, delta + fill, fill)
                    elif delta > 0:
                        resting_change(s_, p_, delta, 0)
                    book[oid][2] = size
                    lv_change(s_, p_, delta)
                    if size <= 0:
                        del book[oid]
                else:
                    resting_change(s_, p_, -z_, 0)
                    lv_change(s_, p_, -z_)
                    book[oid] = [side, price, size]
                    pending_fill[oid] = 0
                    resting_change(side, price, size, 0)
                    lv_change(side, price, size)
        elif action == "R":
            book.clear(); levels["B"].clear(); levels["A"].clear()

        pending = True
        if flags & LAST:
            evaluate()
            pending = False

    if pending:
        evaluate()
    if started:
        while cur < w["end"]:
            write_row()
    return rows


header = ["window_id", "second", "buy", "sell", "trades", "last", "high", "low", "bid", "ask"]
for side in ("ask", "bid"):
    header += [f"{side}_{c}" for c in ("add", "add_w1", "add_wl", "cancel", "cancel_w1", "cancel_wl", "fill", "rest")]
header += ["ask_refill", "ask_ep_up", "ask_ep_down", "bid_refill", "bid_ep_up", "bid_ep_down"]
for limit in LIMITS:
    header += [f"large_buy_{limit}", f"large_sell_{limit}"]
header.append("crossed")

with open("pivot_test_expected.csv", "w") as f:
    f.write(",".join(header) + "\n")
    for w in sorted(windows, key=lambda w: w["start"]):
        for row in replay(w):
            f.write(row + "\n")
print(len(records), "records", len(windows), "windows")
