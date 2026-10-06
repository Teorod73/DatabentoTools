"""Synthetic MBO files with the official databento_dbn encoder and the expected extract computed independently."""
import collections, datetime, gzip, io, random, sys
import databento_dbn as d
import zstandard

random.seed(int(sys.argv[2]) if len(sys.argv) > 2 else 1)
version = int(sys.argv[1]) if len(sys.argv) > 1 else 3
out = f"test_v{version}"

class I:
    def __init__(s, a, b, c): s.start_date = a; s.end_date = b; s.symbol = c
class M:
    def __init__(s, r, iv): s.raw_symbol = r; s.intervals = iv

day = datetime.date(2024, 3, 5)
nxt = day + datetime.timedelta(days=1)
instruments = {1001: ("ESH4", 4750.00, 0.25, 1.0), 1002: ("ESM4", 4800.00, 0.25, 0.08), 3001: ("ESH4-ESM4", 50.00, 0.05, 0.03), 1003: ("ESU4", 4850.00, 0.25, 0.002)}
maps = [M(sym, [I(day, nxt, str(i))]) for i, (sym, *_ ) in instruments.items()]
start = int(datetime.datetime(2024, 3, 5, tzinfo=datetime.timezone.utc).timestamp()) * 10**9
meta = d.Metadata(dataset="GLBX.MDP3", start=start, stype_in=d.SType.PARENT, stype_out=d.SType.INSTRUMENT_ID,
                  schema=d.Schema.MBO, symbols=["ES.FUT"], partial=[], not_found=[], mappings=maps,
                  end=start + 86400 * 10**9, version=version)

records = []
ts = start + 5 * 10**9
order_id = 1
expected_minute = collections.defaultdict(lambda: [0, 0, 0])   # (id, minute, price) -> buy, sell, unknown
expected_second = {}                                          # (id, second) -> [o, h, l, c, vol, buy, sell]
trade_volume = collections.Counter()

def rec(iid, action, side, price, size, flags=128, t=None):
    global order_id
    order_id += 1
    records.append(d.MBOMsg(publisher_id=1, instrument_id=iid, ts_event=t or ts, order_id=order_id,
                            price=round(price * 1e9), size=size, action=action, side=side,
                            ts_recv=(t or ts) + 1500, flags=flags, channel_id=0, ts_in_delta=100, sequence=order_id))

def expect_fill(iid, price, size, aggressor):
    sec = ts // 10**9
    key = (iid, sec - sec % 60, round(price * 1e9))
    expected_minute[key][aggressor] += size
    bar = expected_second.get((iid, sec))
    p = round(price * 1e9)
    if bar is None:
        bar = expected_second[(iid, sec)] = [p, p, p, p, 0, 0, 0]
    bar[1] = max(bar[1], p); bar[2] = min(bar[2], p); bar[3] = p; bar[4] += size
    if aggressor == 0: bar[5] += size
    if aggressor == 1: bar[6] += size

# snapshot at the start: must be ignored
for iid, (_, base, tick, _) in instruments.items():
    rec(iid, d.Action.CLEAR, d.Side.NONE, base, 0, flags=32)
    for k in range(3):
        rec(iid, d.Action.ADD, d.Side.BID, base - tick * (k + 1), 10, flags=32)
        rec(iid, d.Action.ADD, d.Side.ASK, base + tick * (k + 1), 10, flags=32)

prices = {iid: base for iid, (_, base, _, _) in instruments.items()}
for n in range(30000):
    ts += random.randint(1, 400) * 10**6   # 1-400 ms
    iid = random.choices(list(instruments), weights=[w for *_, w in instruments.values()])[0]
    tick = instruments[iid][2]
    prices[iid] += random.choice((-tick, 0, tick))
    r = random.random()
    if r < 0.45:
        rec(iid, d.Action.ADD, random.choice((d.Side.BID, d.Side.ASK)), prices[iid], random.randint(1, 20))
    elif r < 0.75:
        rec(iid, d.Action.CANCEL, random.choice((d.Side.BID, d.Side.ASK)), prices[iid], random.randint(1, 20))
    elif r < 0.80:
        rec(iid, d.Action.MODIFY, random.choice((d.Side.BID, d.Side.ASK)), prices[iid], random.randint(1, 20))
    else:
        # trade: aggressor buy/sell/unknown, sweeping 1-3 price levels, several resting orders per level
        aggressor = random.choices((0, 1, 2), weights=(45, 45, 10))[0]
        levels = random.choices((1, 2, 3), weights=(70, 20, 10))[0]
        fills = []
        for lv in range(levels):
            p = prices[iid] + (tick * lv if aggressor != 1 else -tick * lv)
            for _ in range(random.randint(1, 3)):
                fills.append((p, random.randint(1, 15)))
        total = sum(s for _, s in fills)
        trade_side = {0: d.Side.BID, 1: d.Side.ASK, 2: d.Side.NONE}[aggressor]
        # the trade record: aggregated at the first price (the worst case for trades as volume source)
        rec(iid, d.Action.TRADE, trade_side, fills[0][0], total, flags=0)
        trade_volume[iid] += total
        resting = {0: d.Side.ASK, 1: d.Side.BID, 2: d.Side.NONE}[aggressor]
        for k, (p, s) in enumerate(fills):
            rec(iid, d.Action.FILL, resting, p, s, flags=0)
            expect_fill(iid, p, s, aggressor)
        rec(iid, d.Action.CANCEL, resting, fills[-1][0], 0)
        prices[iid] = fills[-1][0]

buf = io.BytesIO()
buf.write(bytes(meta))
for r in records:
    b = bytes(r)
    if version < 3:
        b = b[:56]
    buf.write(b[:56])
raw = buf.getvalue()

# two zstd frames to test multi frame files
c = zstandard.ZstdCompressor()
half = len(raw) // 2
with open(f"{out}.mbo.dbn.zst", "wb") as f:
    f.write(c.compress(raw[:half]))
    f.write(c.compress(raw[half:]))
with open(f"{out}_plain.mbo.dbn", "wb") as f:
    f.write(raw)

def price(p):
    s = f"{p / 1e9:.9f}".rstrip("0").rstrip(".")
    return s

total = collections.Counter()
for (iid, _, _), v in expected_minute.items():
    total[iid] += sum(v)
mx = max(total.values())
selected = {i for i, v in total.items() if v >= 0.01 * mx}

with open(f"{out}_expected_minute.csv", "w") as f:
    f.write("utc_minute,instrument_id,price,buy,sell,unknown\n")
    for (iid, m, p), v in sorted(expected_minute.items()):
        if iid in selected:
            f.write(f"{m},{iid},{price(p)},{v[0]},{v[1]},{v[2]}\n")
with open(f"{out}_expected_seconds.csv", "w") as f:
    f.write("utc_second,instrument_id,open,high,low,close,volume,buy,sell\n")
    for (iid, s), b in sorted(expected_second.items()):
        if iid in selected:
            f.write(f"{s},{iid},{price(b[0])},{price(b[1])},{price(b[2])},{price(b[3])},{b[4]},{b[5]},{b[6]}\n")
print(version, "records", len(records), "selected", sorted(selected), "volumes", dict(total), "trade volume", dict(trade_volume))
