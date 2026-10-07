"""Synthetic MBO file with a consistent order life cycle, a touch events file and the expected book features,
computed independently of the C# BookFeatureCollector.

    python make_book_test.py [seed]  ->  book_test.mbo.dbn.zst, book_test_events.csv, book_test_expected.csv

Life cycle as on GLBX.MDP3: a trade is one T per price level (aggressor side), then an F per filled resting order
(resting side) and a C (fully filled) or M (partially filled, new size) for that order. Plus adds, partial and full
cancels, size and price modifies, a snapshot (R + adds with the snapshot flag) at the start.
"""
import datetime
import random
import sys

import databento_dbn as d
import zstandard

SEED = int(sys.argv[1]) if len(sys.argv) > 1 else 1
random.seed(SEED)
TICK = 0.25
NS = 10**9
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

records = []          # (ts, instrument, action, side, price, size, order_id, flags)
orders = {1001: {}, 1002: {}}
next_id = [1]
ACTION = {"A": d.Action.ADD, "C": d.Action.CANCEL, "M": d.Action.MODIFY, "R": d.Action.CLEAR,
          "T": d.Action.TRADE, "F": d.Action.FILL}
SIDE = {"A": d.Side.ASK, "B": d.Side.BID, "N": d.Side.NONE}


def emit(ts, iid, action, side, price, size, oid=0, flags=0):
    records.append((ts, iid, action, side, price, size, oid, flags))


def new_id():
    next_id[0] += 1
    return next_id[0]


mids = {1001: 4750.0, 1002: 4800.0}
ts = start + NS
# snapshot
for iid in (1001, 1002):
    emit(ts, iid, "R", "N", 0.0, 0, 0, 32)
    for k in range(1, 6):
        for side, sign in (("B", -1), ("A", 1)):
            oid = new_id()
            price = mids[iid] + sign * k * TICK
            size = random.randint(5, 40)
            orders[iid][oid] = [side, price, size]
            emit(ts, iid, "A", side, price, size, oid, 32)

for step in range(60000):
    ts += random.randint(5, 120) * 10**6   # 5-120 ms, about 1 hour of data
    iid = 1001 if random.random() < 0.9 else 1002
    book = orders[iid]
    r = random.random()
    if r < 0.40 or len(book) < 6:
        side = random.choice("BA")
        price = mids[iid] + (-1 if side == "B" else 1) * random.randint(1, 6) * TICK
        oid = new_id()
        size = random.randint(1, 30)
        book[oid] = [side, price, size]
        emit(ts, iid, "A", side, price, size, oid)
    elif r < 0.65:
        oid = random.choice(list(book))
        side, price, size = book[oid]
        cut = size if random.random() < 0.6 else random.randint(1, size)
        emit(ts, iid, "C", side, price, cut, oid)
        book[oid][2] -= cut
        if book[oid][2] <= 0:
            del book[oid]
    elif r < 0.75:
        oid = random.choice(list(book))
        side, price, size = book[oid]
        if random.random() < 0.5:
            size = random.randint(1, 40)
        else:
            price = price + random.choice((-1, 1)) * TICK
            price = min(price, mids[iid] - TICK) if side == "B" else max(price, mids[iid] + TICK)
        book[oid] = [side, price, size]
        emit(ts, iid, "M", side, price, size, oid)
    else:
        aggressor = random.choice("BA")
        resting = "A" if aggressor == "B" else "B"
        want = random.randint(1, 60)
        levels = sorted({o[1] for o in book.values() if o[0] == resting}, reverse=(resting == "B"))
        for level in levels[:3]:
            if want <= 0:
                break
            queue = [oid for oid, o in book.items() if o[0] == resting and o[1] == level]
            taken = []
            for oid in queue:
                if want <= 0:
                    break
                q = min(want, book[oid][2])
                taken.append((oid, q))
                want -= q
            if not taken:
                continue
            emit(ts, iid, "T", aggressor, level, sum(q for _, q in taken))
            for oid, q in taken:
                emit(ts, iid, "F", resting, level, q, oid)
            for oid, q in taken:
                left = book[oid][2] - q
                if left <= 0:
                    emit(ts, iid, "C", resting, level, book[oid][2], oid)
                    del book[oid]
                else:
                    book[oid][2] = left
                    emit(ts, iid, "M", resting, level, left, oid)
            mids[iid] = level
        # occasional refill right after the trade at the same price (replenishment)
        if random.random() < 0.3:
            oid = new_id()
            size = random.randint(1, 20)
            book[oid] = [resting, mids[iid], size]
            emit(ts + random.randint(1, 900) * 10**6, iid, "A", resting, mids[iid], size, oid)

records.sort(key=lambda x: x[0])

with open("book_test.mbo.dbn.zst", "wb") as f:
    raw = bytearray(bytes(meta))
    for seq, (t, iid, action, side, price, size, oid, flags) in enumerate(records):
        msg = d.MBOMsg(publisher_id=1, instrument_id=iid, ts_event=t, order_id=oid, price=round(price * 1e9),
                       size=size, action=ACTION[action], side=SIDE[side], ts_recv=t + 1000, flags=flags,
                       channel_id=0, ts_in_delta=0, sequence=seq)
        raw += bytes(msg)[:56]
    f.write(zstandard.ZstdCompressor().compress(bytes(raw)))

# ------------------------------------------------------------------------------------------ events
trades = [r for r in records if r[1] == 1001 and r[2] == "T"]
events = []
for k in range(25):
    t, _, _, _, price, _, _, _ = random.choice(trades[200:-200])
    t0 = (t // NS) // 60 * 60
    side = random.choice((1, -1))
    width = random.choice((0, 1, 2)) * TICK
    low = price - random.randint(0, 2) * TICK
    events.append({"id": f"e{k}", "iid": 1001, "t0": t0, "low": low, "high": low + width, "band": 0.5, "side": side})
events.append({"id": "eSpread", "iid": 1002, "t0": (records[-1][0] // NS) // 60 * 60 - 300, "low": mids[1002],
               "high": mids[1002], "band": 0.25, "side": 1})
with open("book_test_events.csv", "w") as f:
    f.write("event_id,instrument_id,t0_utc_seconds,zone_low,zone_high,band,side\n")
    for e in events:
        f.write(f"{e['id']},{e['iid']},{e['t0']},{e['low']},{e['high']},{e['band']},{e['side']}\n")


# ------------------------------------------------------------------------------------------ expected
def size_between(levels, low, high):
    return sum(s for p, s in levels.items() if low - 1e-9 <= p <= high + 1e-9)


def price_text(p):
    return f"{p:.9f}".rstrip("0").rstrip(".")


rows = []
for e in events:
    t0, b_lo, b_hi = e["t0"] * NS, e["low"] - e["band"], e["high"] + e["band"]
    defending = "B" if e["side"] == 1 else "A"
    attacking = "A" if e["side"] == 1 else "B"
    book, levels = {}, {"B": {}, "A": {}}
    pending = {}
    last_trade = None
    f = {"pre_buy": 0, "pre_sell": 0, "pre_trades": 0, "touch_delay": -1, "rt0": -1, "r1": -1, "r3": -1,
         "bids": -1, "asks": -1}
    w = [{k: 0 for k in ("att", "counter", "buy", "sell", "trades", "added", "cancelled", "filled", "repl", "large")} for _ in range(2)]
    hi = [None, None]
    lo = [None, None]
    last_attack = {}
    done = False

    def in_band(p):
        return b_lo - 1e-9 <= p <= b_hi + 1e-9

    def win(t, i):
        return t0 <= t < t0 + (60 if i == 0 else 180) * NS

    def change(side, price, delta):
        lv = levels[side]
        lv[price] = lv.get(price, 0) + delta
        if lv[price] <= 0:
            del lv[price]

    def defending_change(t, side, price, delta, filled):
        if side != defending or not in_band(price):
            return
        for i in range(2):
            if not win(t, i):
                continue
            if delta > 0:
                w[i]["added"] += delta
                if price in last_attack and t - last_attack[price] <= NS:
                    w[i]["repl"] += delta
            elif delta < 0:
                w[i]["cancelled"] += -delta
            w[i]["filled"] += filled

    for (t, iid, action, side, price, size, oid, flags) in records:
        if iid != e["iid"]:
            continue
        if not done:
            if f["rt0"] < 0 and t >= t0:
                f["rt0"] = size_between(levels[defending], b_lo, b_hi)
                ref = last_trade if last_trade is not None else (e["low"] + e["high"]) / 2
                f["bids"] = size_between(levels["B"], ref - 10 * TICK, ref)
                f["asks"] = size_between(levels["A"], ref, ref + 10 * TICK)
            if f["r1"] < 0 and t >= t0 + 60 * NS:
                f["r1"] = size_between(levels[defending], b_lo, b_hi)
            if f["r3"] < 0 and t >= t0 + 180 * NS:
                f["r3"] = size_between(levels[defending], b_lo, b_hi)
                done = True
        if action == "T":
            last_trade = price
            if not done and t >= t0 - 300 * NS:
                if t < t0:
                    f["pre_trades"] += 1
                    if side == "B": f["pre_buy"] += size
                    if side == "A": f["pre_sell"] += size
                else:
                    if f["touch_delay"] < 0 and t < t0 + 60 * NS and in_band(price):
                        f["touch_delay"] = (t - t0) / 1e9
                    for i in range(2):
                        if not win(t, i):
                            continue
                        w[i]["trades"] += 1
                        if side == "B": w[i]["buy"] += size
                        if side == "A": w[i]["sell"] += size
                        hi[i] = price if hi[i] is None else max(hi[i], price)
                        lo[i] = price if lo[i] is None else min(lo[i], price)
                        if side == attacking:
                            if in_band(price):
                                w[i]["att"] += size
                                if size >= 20: w[i]["large"] += 1
                        else:
                            w[i]["counter"] += size
                    if side == attacking and in_band(price):
                        last_attack[price] = t
        elif action == "F":
            if oid in book:
                pending[oid] = pending.get(oid, 0) + size
        elif action == "A":
            book[oid] = [side, price, size]
            change(side, price, size)
            defending_change(t, side, price, size, 0)
        elif action == "C":
            if oid in book:
                s_, p_, z_ = book[oid]
                red = min(z_, size)
                fill = min(pending.get(oid, 0), red)
                pending[oid] = pending.get(oid, 0) - fill
                book[oid][2] -= red
                change(s_, p_, -red)
                if book[oid][2] <= 0:
                    del book[oid]
                defending_change(t, s_, p_, -(red - fill), fill)
        elif action == "M":
            if oid not in book:
                book[oid] = [side, price, size]
                change(side, price, size)
                defending_change(t, side, price, size, 0)
            else:
                s_, p_, z_ = book[oid]
                if p_ == price and s_ == side:
                    delta = size - z_
                    if delta < 0:
                        fill = min(pending.get(oid, 0), -delta)
                        pending[oid] = pending.get(oid, 0) - fill
                        defending_change(t, s_, p_, delta + fill, fill)
                    elif delta > 0:
                        defending_change(t, s_, p_, delta, 0)
                    book[oid][2] = size
                    change(s_, p_, delta)
                    if size <= 0:
                        del book[oid]
                else:
                    change(s_, p_, -z_)
                    defending_change(t, s_, p_, -z_, 0)
                    book[oid] = [side, price, size]
                    pending[oid] = 0
                    change(side, price, size)
                    defending_change(t, side, price, size, 0)
        elif action == "R":
            book.clear(); levels["B"].clear(); levels["A"].clear()

    pre_partial = 1 if t0 < start + 300 * NS else 0
    td = f["touch_delay"]
    row = [e["id"], pre_partial, f["pre_buy"], f["pre_sell"], f["pre_trades"],
           (f"{td:.3f}".rstrip("0").rstrip(".") if td >= 0 else "-1"), f["rt0"], f["r1"], f["r3"], f["bids"], f["asks"]]
    for i in range(2):
        x = w[i]
        row += [x["att"], x["counter"], x["buy"], x["sell"], x["trades"], x["added"], x["cancelled"], x["filled"],
                x["repl"], x["large"], price_text(hi[i]) if hi[i] is not None else "", price_text(lo[i]) if lo[i] is not None else ""]
    rows.append(row)

header = ["event_id", "pre_partial", "pre_buy", "pre_sell", "pre_trades", "touch_delay", "resting_def_t0",
          "resting_def_1", "resting_def_3", "bids_t0", "asks_t0"]
for wn in ("1", "3"):
    header += [f"{c}_{wn}" for c in ("att_vol", "counter_vol", "buy", "sell", "trades", "added_def", "cancelled_def",
                                      "filled_def", "replenished", "large_att", "max_price", "min_price")]
order = {e["id"]: e["t0"] for e in events}
rows.sort(key=lambda r: order[r[0]])
with open("book_test_expected.csv", "w") as f:
    f.write(",".join(header) + "\n")
    for r in rows:
        f.write(",".join(map(str, r)) + "\n")
print(len(records), "records", len(events), "events")
