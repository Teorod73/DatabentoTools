using System.Globalization;
using System.Text;

namespace DatabentoExtract;

// One zone touch of the event study (events file of research/export_book_events.py)
public sealed class TouchEvent
{
    public string Id = "";
    public uint InstrumentId;
    public ulong T0;                 // start of the touch minute, UTC ns
    public long ZoneLow, ZoneHigh;   // 1e-9 price units
    public long BandLow, BandHigh;   // zone +- touch tolerance
    public int Side;                 // +1 long (support, the price came from above), -1 short (resistance, from below)

    // the defending resting orders are on the bid for a long, on the ask for a short
    public char DefendingSide => Side == 1 ? 'B' : 'A';
    // the attacking trades: sellers for a long (trade side A), buyers for a short (trade side B)
    public char AttackingTradeSide => Side == 1 ? 'A' : 'B';
    public bool InBand(long price) => price >= BandLow && price <= BandHigh;
}

/// <summary>
/// Order book and trade flow features around the zone touches, replaying the MBO records of one file.
///
/// Book: A adds, C reduces (removes at zero), M sets price and size (an unknown order is added), R clears the
/// instrument. T and F do not change the book (the resting order changes come as C or M), but the filled size
/// of an order is remembered so the following C or M reduction is counted as fill and not as cancel.
///
/// Windows from T0 (the start of the touch minute): pre = [T0 - 5 min, T0), W1 = [T0, T0 + 60 s),
/// W3 = [T0, T0 + 180 s). The band is the zone +- touch tolerance.
/// </summary>
public sealed class BookFeatureCollector
{
    private const long NanosPerSecond = 1_000_000_000;
    private const long PreWindow = 300 * NanosPerSecond;
    private const long Window1 = 60 * NanosPerSecond;
    private const long Window3 = 180 * NanosPerSecond;
    private const long ReplenishDelay = 1 * NanosPerSecond;
    private const int LargeTrade = 20;
    private const int ImbalanceTicks = 10;

    private sealed class Order
    {
        public char Side;
        public long Price;
        public long Size;
        public long PendingFill;
    }

    private sealed class Book
    {
        public readonly Dictionary<ulong, Order> Orders = [];
        public readonly Dictionary<long, long> Bids = [];
        public readonly Dictionary<long, long> Asks = [];
        public long LastTrade = MboRecord.UndefinedPrice;

        public Dictionary<long, long> Levels(char side) => side == 'B' ? Bids : Asks;

        public void Change(char side, long price, long delta)
        {
            var levels = Levels(side);
            var size = levels.GetValueOrDefault(price) + delta;
            if (size > 0) levels[price] = size;
            else levels.Remove(price);
        }

        public long SizeBetween(char side, long low, long high, long tick)
        {
            var levels = Levels(side);
            long sum = 0;
            for (var p = low - low % tick; p <= high; p += tick)
                if (p >= low)
                    sum += levels.GetValueOrDefault(p);
            return sum;
        }
    }

    private sealed class State(TouchEvent e)
    {
        public readonly TouchEvent Event = e;
        public bool PrePartial;
        public long PreBuy, PreSell, PreTrades;
        public double TouchDelay = -1;           // seconds from T0 to the first trade in the band
        public long RestingDefT0 = -1, RestingDef1 = -1, RestingDef3 = -1;
        public long BidsT0 = -1, AsksT0 = -1;
        public readonly long[] AttVol = new long[2], CounterVol = new long[2], Buy = new long[2], Sell = new long[2];
        public readonly long[] AddedDef = new long[2], CancelledDef = new long[2], FilledDef = new long[2];
        public readonly long[] Replenished = new long[2], LargeAtt = new long[2], Trades = new long[2];
        public readonly long[] MaxPrice = [long.MinValue, long.MinValue], MinPrice = [long.MaxValue, long.MaxValue];
        public readonly Dictionary<long, ulong> LastAttackAt = [];   // price -> time of the last attacking trade
        public bool Done;
    }

    private readonly long tick;
    private readonly Dictionary<uint, Book> books = [];
    private readonly List<State> states;
    private readonly HashSet<uint> instruments;
    private readonly ulong fileStart;
    private int next;                          // first state not active yet (states sorted by T0)
    private readonly List<State> active = [];

    public BookFeatureCollector(IEnumerable<TouchEvent> events, ulong fileStart, double tickSize = 0.25)
    {
        tick = (long)Math.Round(tickSize * 1e9);
        this.fileStart = fileStart;
        states = events.OrderBy(e => e.T0).Select(e => new State(e)).ToList();
        instruments = states.Select(s => s.Event.InstrumentId).ToHashSet();
        foreach (var id in instruments)
            books[id] = new Book();
        foreach (var s in states)
            s.PrePartial = s.Event.T0 < fileStart + (ulong)PreWindow;
    }

    public int EventCount => states.Count;

    public void Process(byte rtype, ReadOnlySpan<byte> record)
    {
        if (rtype != MboRecord.RType || record.Length < MboRecord.Size)
            return;

        var mbo = new MboRecord(record);
        if (!instruments.Contains(mbo.InstrumentId))
            return;

        var ts = mbo.Timestamp;
        Activate(ts);

        var book = books[mbo.InstrumentId];

        // book snapshots of the events at their window borders, before this record changes the book
        foreach (var s in active)
        {
            if (s.Event.InstrumentId != mbo.InstrumentId)
                continue;
            var e = s.Event;
            if (s.RestingDefT0 < 0 && ts >= e.T0)
            {
                s.RestingDefT0 = book.SizeBetween(e.DefendingSide, e.BandLow, e.BandHigh, tick);
                var reference = book.LastTrade != MboRecord.UndefinedPrice ? book.LastTrade : (e.ZoneLow + e.ZoneHigh) / 2;
                s.BidsT0 = book.SizeBetween('B', reference - ImbalanceTicks * tick, reference, tick);
                s.AsksT0 = book.SizeBetween('A', reference, reference + ImbalanceTicks * tick, tick);
            }
            if (s.RestingDef1 < 0 && ts >= e.T0 + (ulong)Window1)
                s.RestingDef1 = book.SizeBetween(e.DefendingSide, e.BandLow, e.BandHigh, tick);
            if (s.RestingDef3 < 0 && ts >= e.T0 + (ulong)Window3)
            {
                s.RestingDef3 = book.SizeBetween(e.DefendingSide, e.BandLow, e.BandHigh, tick);
                s.Done = true;
            }
        }

        switch (mbo.Action)
        {
            case 'T':
                if (mbo.Price != MboRecord.UndefinedPrice)
                {
                    book.LastTrade = mbo.Price;
                    OnTrade(mbo, ts);
                }
                break;
            case 'F':
                if (book.Orders.TryGetValue(mbo.OrderId, out var filled))
                    filled.PendingFill += mbo.OrderSize;
                break;
            case 'A':
                Add(book, mbo, ts);
                break;
            case 'C':
                Reduce(book, mbo, ts, mbo.OrderSize, remove: false);
                break;
            case 'M':
                Modify(book, mbo, ts);
                break;
            case 'R':
                book.Orders.Clear();
                book.Bids.Clear();
                book.Asks.Clear();
                break;
        }

        if (active.Count > 0)
            active.RemoveAll(s => s.Done);
    }

    private void Activate(ulong ts)
    {
        while (next < states.Count && ts + (ulong)PreWindow >= states[next].Event.T0)
            active.Add(states[next++]);
    }

    private void OnTrade(MboRecord mbo, ulong ts)
    {
        foreach (var s in active)
        {
            var e = s.Event;
            if (e.InstrumentId != mbo.InstrumentId)
                continue;
            long size = mbo.OrderSize;

            if (ts < e.T0)
            {
                s.PreTrades++;
                if (mbo.Side == 'B') s.PreBuy += size;
                else if (mbo.Side == 'A') s.PreSell += size;
                continue;
            }

            if (s.TouchDelay < 0 && ts < e.T0 + (ulong)Window1 && e.InBand(mbo.Price))
                s.TouchDelay = (ts - e.T0) / 1e9;

            for (var w = 0; w < 2; w++)
            {
                if (!InWindow(e, ts, w))
                    continue;
                s.Trades[w]++;
                if (mbo.Side == 'B') s.Buy[w] += size;
                else if (mbo.Side == 'A') s.Sell[w] += size;
                s.MaxPrice[w] = Math.Max(s.MaxPrice[w], mbo.Price);
                s.MinPrice[w] = Math.Min(s.MinPrice[w], mbo.Price);

                if (mbo.Side == e.AttackingTradeSide)
                {
                    if (e.InBand(mbo.Price))
                    {
                        s.AttVol[w] += size;
                        if (size >= LargeTrade) s.LargeAtt[w]++;
                    }
                }
                else if (mbo.Side is 'A' or 'B')
                    s.CounterVol[w] += size;
            }

            if (mbo.Side == e.AttackingTradeSide && e.InBand(mbo.Price))
                s.LastAttackAt[mbo.Price] = ts;
        }
    }

    private static bool InWindow(TouchEvent e, ulong ts, int w) =>
        ts >= e.T0 && ts < e.T0 + (ulong)(w == 0 ? Window1 : Window3);

    private void Add(Book book, MboRecord mbo, ulong ts)
    {
        if (mbo.Side is not ('A' or 'B') || mbo.Price == MboRecord.UndefinedPrice)
            return;
        book.Orders[mbo.OrderId] = new Order { Side = mbo.Side, Price = mbo.Price, Size = mbo.OrderSize };
        book.Change(mbo.Side, mbo.Price, mbo.OrderSize);
        OnDefendingChange(mbo.InstrumentId, mbo.Side, mbo.Price, mbo.OrderSize, 0, ts);
    }

    private void Reduce(Book book, MboRecord mbo, ulong ts, long size, bool remove)
    {
        if (!book.Orders.TryGetValue(mbo.OrderId, out var order))
            return;
        var reduce = Math.Min(order.Size, size);
        var fill = Math.Min(order.PendingFill, reduce);
        order.PendingFill -= fill;
        order.Size -= reduce;
        book.Change(order.Side, order.Price, -reduce);
        if (order.Size <= 0 || remove)
        {
            if (order.Size > 0)
                book.Change(order.Side, order.Price, -order.Size);
            book.Orders.Remove(mbo.OrderId);
        }
        OnDefendingChange(mbo.InstrumentId, order.Side, order.Price, -(reduce - fill), fill, ts);
    }

    private void Modify(Book book, MboRecord mbo, ulong ts)
    {
        if (!book.Orders.TryGetValue(mbo.OrderId, out var order))
        {
            Add(book, mbo, ts);
            return;
        }
        if (order.Price == mbo.Price && order.Side == mbo.Side)
        {
            var delta = (long)mbo.OrderSize - order.Size;
            if (delta < 0)
            {
                var fill = Math.Min(order.PendingFill, -delta);
                order.PendingFill -= fill;
                OnDefendingChange(mbo.InstrumentId, order.Side, order.Price, delta + fill, fill, ts);
            }
            else if (delta > 0)
                OnDefendingChange(mbo.InstrumentId, order.Side, order.Price, delta, 0, ts);
            order.Size = mbo.OrderSize;
            book.Change(order.Side, order.Price, delta);
            if (order.Size <= 0)
                book.Orders.Remove(mbo.OrderId);
            return;
        }

        // price (or side) change: removed from the old price, added at the new one
        book.Change(order.Side, order.Price, -order.Size);
        OnDefendingChange(mbo.InstrumentId, order.Side, order.Price, -order.Size, 0, ts);
        order.Side = mbo.Side;
        order.Price = mbo.Price;
        order.Size = mbo.OrderSize;
        order.PendingFill = 0;
        book.Change(order.Side, order.Price, order.Size);
        OnDefendingChange(mbo.InstrumentId, order.Side, order.Price, order.Size, 0, ts);
    }

    // resting size change on a price: added (> 0) or cancelled (< 0), filled separately
    private void OnDefendingChange(uint instrumentId, char side, long price, long change, long filled, ulong ts)
    {
        foreach (var s in active)
        {
            var e = s.Event;
            if (e.InstrumentId != instrumentId || side != e.DefendingSide || !e.InBand(price))
                continue;
            for (var w = 0; w < 2; w++)
            {
                if (!InWindow(e, ts, w))
                    continue;
                if (change > 0)
                {
                    s.AddedDef[w] += change;
                    if (s.LastAttackAt.TryGetValue(price, out var at) && ts - at <= (ulong)ReplenishDelay)
                        s.Replenished[w] += change;
                }
                else if (change < 0)
                    s.CancelledDef[w] += -change;
                s.FilledDef[w] += filled;
            }
        }
    }

    public static IEnumerable<string> Header()
    {
        var columns = new List<string> { "event_id", "pre_partial", "pre_buy", "pre_sell", "pre_trades", "touch_delay",
            "resting_def_t0", "resting_def_1", "resting_def_3", "bids_t0", "asks_t0" };
        foreach (var w in new[] { "1", "3" })
            columns.AddRange(new[] { "att_vol", "counter_vol", "buy", "sell", "trades", "added_def", "cancelled_def",
                "filled_def", "replenished", "large_att", "max_price", "min_price" }.Select(c => $"{c}_{w}"));
        return columns;
    }

    public IEnumerable<string> Rows()
    {
        foreach (var s in states)
        {
            var values = new List<string>
            {
                s.Event.Id, s.PrePartial ? "1" : "0", s.PreBuy.ToString(), s.PreSell.ToString(), s.PreTrades.ToString(),
                s.TouchDelay.ToString("0.###", CultureInfo.InvariantCulture),
                s.RestingDefT0.ToString(), s.RestingDef1.ToString(), s.RestingDef3.ToString(),
                s.BidsT0.ToString(), s.AsksT0.ToString(),
            };
            for (var w = 0; w < 2; w++)
                values.AddRange(new[]
                {
                    s.AttVol[w].ToString(), s.CounterVol[w].ToString(), s.Buy[w].ToString(), s.Sell[w].ToString(),
                    s.Trades[w].ToString(), s.AddedDef[w].ToString(), s.CancelledDef[w].ToString(), s.FilledDef[w].ToString(),
                    s.Replenished[w].ToString(), s.LargeAtt[w].ToString(),
                    s.MaxPrice[w] == long.MinValue ? "" : Extractor.Price(s.MaxPrice[w]),
                    s.MinPrice[w] == long.MaxValue ? "" : Extractor.Price(s.MinPrice[w]),
                });
            yield return string.Join(",", values);
        }
    }

    public static List<TouchEvent> ReadEvents(string path)
    {
        var events = new List<TouchEvent>();
        foreach (var line in File.ReadLines(path).Skip(1))
        {
            var p = line.Split(',');
            // event_id,instrument_id,t0_utc_seconds,zone_low,zone_high,band,side
            double Parse(int i) => double.Parse(p[i], CultureInfo.InvariantCulture);
            long Raw(double price) => (long)Math.Round(price * 1e9);
            var band = Parse(5);
            events.Add(new TouchEvent
            {
                Id = p[0],
                InstrumentId = uint.Parse(p[1], CultureInfo.InvariantCulture),
                T0 = ulong.Parse(p[2], CultureInfo.InvariantCulture) * NanosPerSecond,
                ZoneLow = Raw(Parse(3)), ZoneHigh = Raw(Parse(4)),
                BandLow = Raw(Parse(3) - band), BandHigh = Raw(Parse(4) + band),
                Side = int.Parse(p[6], CultureInfo.InvariantCulture),
            });
        }
        return events;
    }
}

public static class BookProcessor
{
    // the events whose touch minute is in the UTC day of the file
    public static void Process(string path, string outDirectory, List<TouchEvent> allEvents, bool force, object consoleSync)
    {
        var name = FileProcessor.BaseName(path);
        var output = Path.Combine(outDirectory, "book", name + ".csv");
        if (!force && File.Exists(output))
        {
            lock (consoleSync) Console.WriteLine($"{name}: already done, skipped");
            return;
        }

        var stopwatch = System.Diagnostics.Stopwatch.StartNew();
        using var stream = DbnReader.Open(path);
        var metadata = DbnReader.ReadMetadata(stream);
        var start = metadata.Start;
        var end = metadata.End == MboRecord.UndefinedTimestamp ? start + 86_400UL * 1_000_000_000 : metadata.End;
        var events = allEvents.Where(e => e.T0 >= start && e.T0 < end).ToList();

        Directory.CreateDirectory(Path.GetDirectoryName(output)!);
        if (events.Count == 0)
        {
            File.WriteAllText(output, string.Join(",", BookFeatureCollector.Header()) + "\n");
            lock (consoleSync) Console.WriteLine($"{name}: no events");
            return;
        }

        var collector = new BookFeatureCollector(events, start);
        DbnReader.ReadRecords(stream, collector.Process);

        var temp = output + ".tmp";
        using (var writer = new StreamWriter(temp, false, new UTF8Encoding(false)))
        {
            writer.WriteLine(string.Join(",", BookFeatureCollector.Header()));
            foreach (var row in collector.Rows())
                writer.WriteLine(row);
        }
        File.Move(temp, output, true);

        lock (consoleSync)
            Console.WriteLine($"{name}: {events.Count} events in {stopwatch.Elapsed.TotalSeconds:0.0} s");
    }
}
