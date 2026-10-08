using System.Globalization;
using System.IO.Compression;
using System.Text;

namespace DatabentoExtract;

// One study window around a ZigZag swing (pivot events file of research/pivot_windows.py)
public sealed class PivotWindow
{
    public string Id = "";
    public uint InstrumentId;
    public long StartSecond, EndSecond;   // UTC seconds, rows for [start, end)
    public long BandLow, BandHigh;        // 1e-9 price units
}

/// <summary>
/// Order flow of the pivot windows per second, replaying the MBO records of one file.
///
/// Book as in BookFeatureCollector: A adds, C reduces, M sets price and size (an unknown order is added), R clears
/// the instrument; T and F do not change the book, the filled size of an order is remembered so the following C or M
/// reduction is counted as fill and not as cancel. The best bid and ask are evaluated at the end of an event (record
/// with the last flag, or the first record of a later timestamp), not after every record of the event.
///
/// Per second and window (every flow column is the change in that second):
///   buy, sell, trades        aggressor volume (trade side B / A) and trade count
///   last, high, low          trade prices of the second (empty without trade); bid, ask: best prices at its end
///   {ask,bid}_add / _cancel / _fill   resting size added, cancelled (not filled) and filled on that side inside the
///                            band; _w1 weighted by 1 / (1 + d), _wl by max(0, 1 - d / H), d = ticks from the best
///                            price of that side before the change, H = half band in ticks
///   {ask,bid}_rest           resting size of that side inside the band at the end of the second
///   {ask,bid}_refill         refill on the best price: an episode lasts while the best price of the side stays,
///                            Q0 = its resting size at the start, A = aggressor volume against it at that price,
///                            the counter is the sum of max(0, A - Q0) of the closed episodes plus the open one
///   {ask,bid}_ep_up / _down  closed episodes whose best price moved up / down
///   large_{buy,sell}_{20,60} volume of the aggressor series of at least 20 / 60 contracts (AgressiveDetector:
///                            same side trades within 10 ms of the first trade of the series), in the second the
///                            series is closed (by an opposite or unknown side trade or a record of the instrument
///                            later than 10 ms)
///   crossed                  1 when the best bid is not below the best ask at the end of the second
/// The episodes of a window start at its first second (Q0 = the resting size at the best price then).
/// </summary>
public sealed class PivotFlowCollector
{
    private const long NanosPerSecond = 1_000_000_000;
    private const long SeriesTime = 10_000_000;     // 10 ms
    private static readonly int[] LargeLimits = [20, 60];

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
        public readonly SortedSet<long> BidPrices = [];
        public readonly SortedSet<long> AskPrices = [];

        public void Change(char side, long price, long delta)
        {
            var levels = side == 'B' ? Bids : Asks;
            var prices = side == 'B' ? BidPrices : AskPrices;
            var size = levels.GetValueOrDefault(price) + delta;
            if (size > 0)
            {
                levels[price] = size;
                prices.Add(price);
            }
            else
            {
                levels.Remove(price);
                prices.Remove(price);
            }
        }

        public void Clear()
        {
            Orders.Clear();
            Bids.Clear();
            Asks.Clear();
            BidPrices.Clear();
            AskPrices.Clear();
        }

        public long BestBid => BidPrices.Count > 0 ? BidPrices.Max : MboRecord.UndefinedPrice;
        public long BestAsk => AskPrices.Count > 0 ? AskPrices.Min : MboRecord.UndefinedPrice;

        public long Size(char side, long price) => (side == 'B' ? Bids : Asks).GetValueOrDefault(price);

        public long SizeBetween(char side, long low, long high, long tick)
        {
            var levels = side == 'B' ? Bids : Asks;
            long sum = 0;
            for (var p = low - low % tick; p <= high; p += tick)
                if (p >= low)
                    sum += levels.GetValueOrDefault(p);
            return sum;
        }
    }

    // series of same side aggressor trades of an instrument
    private sealed class Series
    {
        public char Side;
        public ulong First;
        public long Volume;
    }

    private sealed class Episode
    {
        public long Price = MboRecord.UndefinedPrice;
        public long Initial, Aggressor, Closed;
        public long Total => Closed + Math.Max(0, Aggressor - Initial);
    }

    // flow of one side in the current second
    private sealed class SideFlow
    {
        public long Add, Cancel, Fill, EpisodesUp, EpisodesDown;
        public double AddW1, AddWl, CancelW1, CancelWl;
    }

    private sealed class State(PivotWindow w)
    {
        public readonly PivotWindow Window = w;
        public long Second;                     // the second being collected
        public bool Started;
        public readonly Episode AskEpisode = new(), BidEpisode = new();
        public long AskRefillWritten, BidRefillWritten;
        public long Buy, Sell, Trades;
        public long Last = MboRecord.UndefinedPrice, High = long.MinValue, Low = long.MaxValue;
        public readonly SideFlow Ask = new(), Bid = new();
        public readonly long[] LargeBuy = new long[LargeLimits.Length], LargeSell = new long[LargeLimits.Length];
        public readonly List<string> Rows = [];
        public bool Done;

        public Episode EpisodeOf(char side) => side == 'B' ? BidEpisode : AskEpisode;
        public SideFlow FlowOf(char side) => side == 'B' ? Bid : Ask;
    }

    private readonly long tick;
    private readonly Dictionary<uint, Book> books = [];
    private readonly Dictionary<uint, Series?> series = [];
    private readonly List<State> states;
    private readonly HashSet<uint> instruments;
    private readonly List<State> active = [];
    private int next;
    private ulong lastTs;
    private bool pending;

    public PivotFlowCollector(IEnumerable<PivotWindow> windows, double tickSize = 0.25)
    {
        tick = (long)Math.Round(tickSize * 1e9);
        states = windows.OrderBy(w => w.StartSecond).Select(w => new State(w)).ToList();
        instruments = states.Select(s => s.Window.InstrumentId).ToHashSet();
        foreach (var id in instruments)
        {
            books[id] = new Book();
            series[id] = null;
        }
    }

    public void Process(byte rtype, ReadOnlySpan<byte> record)
    {
        if (rtype != MboRecord.RType || record.Length < MboRecord.Size)
            return;

        var mbo = new MboRecord(record);
        if (!instruments.Contains(mbo.InstrumentId))
            return;

        var ts = mbo.Timestamp;
        if (pending && ts != lastTs)
            EvaluateBests();
        lastTs = ts;

        Advance(ts);
        if (series[mbo.InstrumentId] is { } open && ts - open.First > SeriesTime)
            CloseSeries(mbo.InstrumentId);

        var book = books[mbo.InstrumentId];
        switch (mbo.Action)
        {
            case 'T':
                if (mbo.Price != MboRecord.UndefinedPrice)
                    OnTrade(mbo, ts);
                break;
            case 'F':
                if (book.Orders.TryGetValue(mbo.OrderId, out var filled))
                    filled.PendingFill += mbo.OrderSize;
                break;
            case 'A':
                Add(book, mbo);
                break;
            case 'C':
                Reduce(book, mbo);
                break;
            case 'M':
                Modify(book, mbo);
                break;
            case 'R':
                book.Clear();
                break;
        }

        pending = true;
        if ((mbo.Flags & MboRecord.FlagLast) != 0)
            EvaluateBests();

        if (active.Count > 0)
            active.RemoveAll(s => s.Done);
    }

    // after the last record of the file
    public void Finish()
    {
        if (pending)
            EvaluateBests();
        foreach (var s in active)
            while (s.Second < s.Window.EndSecond)
                WriteRow(s);
        active.Clear();
    }

    // starts the windows and writes the rows of the seconds before ts
    private void Advance(ulong ts)
    {
        var second = (long)(ts / NanosPerSecond);
        while (next < states.Count && states[next].Window.StartSecond <= second)
        {
            var s = states[next++];
            s.Second = s.Window.StartSecond;
            StartEpisode(s, 'A');
            StartEpisode(s, 'B');
            s.Started = true;
            active.Add(s);
        }

        foreach (var s in active)
        {
            while (s.Second < second && s.Second < s.Window.EndSecond)
                WriteRow(s);
            if (s.Second >= s.Window.EndSecond)
                s.Done = true;
        }
        if (active.Count > 0)
            active.RemoveAll(s => s.Done);
    }

    private void StartEpisode(State s, char side)
    {
        var book = books[s.Window.InstrumentId];
        var episode = s.EpisodeOf(side);
        episode.Price = side == 'B' ? book.BestBid : book.BestAsk;
        episode.Initial = episode.Price == MboRecord.UndefinedPrice ? 0 : book.Size(side, episode.Price);
        episode.Aggressor = 0;
    }

    private void EvaluateBests()
    {
        pending = false;
        foreach (var s in active)
        {
            var book = books[s.Window.InstrumentId];
            foreach (var side in new[] { 'A', 'B' })
            {
                var episode = s.EpisodeOf(side);
                var price = side == 'B' ? book.BestBid : book.BestAsk;
                if (price == episode.Price)
                    continue;
                if (episode.Price != MboRecord.UndefinedPrice)
                {
                    episode.Closed += Math.Max(0, episode.Aggressor - episode.Initial);
                    if (price != MboRecord.UndefinedPrice)
                    {
                        if (price > episode.Price) s.FlowOf(side).EpisodesUp++;
                        else s.FlowOf(side).EpisodesDown++;
                    }
                }
                episode.Price = price;
                episode.Initial = price == MboRecord.UndefinedPrice ? 0 : book.Size(side, price);
                episode.Aggressor = 0;
            }
        }
    }

    private void CloseSeries(uint id)
    {
        if (series[id] is not { } open)
            return;
        series[id] = null;
        foreach (var s in active)
        {
            if (s.Window.InstrumentId != id)
                continue;
            for (var k = 0; k < LargeLimits.Length; k++)
                if (open.Volume >= LargeLimits[k])
                {
                    if (open.Side == 'B') s.LargeBuy[k] += open.Volume;
                    else s.LargeSell[k] += open.Volume;
                }
        }
    }

    private void OnTrade(MboRecord mbo, ulong ts)
    {
        var id = mbo.InstrumentId;
        long size = mbo.OrderSize;

        // aggressor series as the AgressiveDetector (FromFirstTrade): an unknown side trade closes it
        if (series[id] is { } open && open.Side != mbo.Side)
            CloseSeries(id);
        if (mbo.Side is 'A' or 'B')
        {
            if (series[id] is { } same)
                same.Volume += size;
            else
                series[id] = new Series { Side = mbo.Side, First = ts, Volume = size };
        }

        foreach (var s in active)
        {
            if (s.Window.InstrumentId != id)
                continue;
            s.Trades++;
            if (mbo.Side == 'B') s.Buy += size;
            else if (mbo.Side == 'A') s.Sell += size;
            s.Last = mbo.Price;
            s.High = Math.Max(s.High, mbo.Price);
            s.Low = Math.Min(s.Low, mbo.Price);

            // a buyer takes the ask, a seller the bid
            if (mbo.Side is 'A' or 'B')
            {
                var episode = s.EpisodeOf(mbo.Side == 'B' ? 'A' : 'B');
                if (episode.Price == mbo.Price)
                    episode.Aggressor += size;
            }
        }
    }

    private void Add(Book book, MboRecord mbo)
    {
        if (mbo.Side is not ('A' or 'B') || mbo.Price == MboRecord.UndefinedPrice)
            return;
        OnRestingChange(book, mbo.InstrumentId, mbo.Side, mbo.Price, mbo.OrderSize, 0);
        book.Orders[mbo.OrderId] = new Order { Side = mbo.Side, Price = mbo.Price, Size = mbo.OrderSize };
        book.Change(mbo.Side, mbo.Price, mbo.OrderSize);
    }

    private void Reduce(Book book, MboRecord mbo)
    {
        if (!book.Orders.TryGetValue(mbo.OrderId, out var order))
            return;
        var reduce = Math.Min(order.Size, mbo.OrderSize);
        var fill = Math.Min(order.PendingFill, reduce);
        order.PendingFill -= fill;
        OnRestingChange(book, mbo.InstrumentId, order.Side, order.Price, -(reduce - fill), fill);
        order.Size -= reduce;
        book.Change(order.Side, order.Price, -reduce);
        if (order.Size <= 0)
            book.Orders.Remove(mbo.OrderId);
    }

    private void Modify(Book book, MboRecord mbo)
    {
        if (!book.Orders.TryGetValue(mbo.OrderId, out var order))
        {
            Add(book, mbo);
            return;
        }
        if (order.Price == mbo.Price && order.Side == mbo.Side)
        {
            var delta = (long)mbo.OrderSize - order.Size;
            if (delta < 0)
            {
                var fill = Math.Min(order.PendingFill, -delta);
                order.PendingFill -= fill;
                OnRestingChange(book, mbo.InstrumentId, order.Side, order.Price, delta + fill, fill);
            }
            else if (delta > 0)
                OnRestingChange(book, mbo.InstrumentId, order.Side, order.Price, delta, 0);
            order.Size = mbo.OrderSize;
            book.Change(order.Side, order.Price, delta);
            if (order.Size <= 0)
                book.Orders.Remove(mbo.OrderId);
            return;
        }

        // price (or side) change: cancelled at the old price, added at the new one
        OnRestingChange(book, mbo.InstrumentId, order.Side, order.Price, -order.Size, 0);
        book.Change(order.Side, order.Price, -order.Size);
        if (mbo.Side is not ('A' or 'B') || mbo.Price == MboRecord.UndefinedPrice)
        {
            book.Orders.Remove(mbo.OrderId);
            return;
        }
        order.Side = mbo.Side;
        order.Price = mbo.Price;
        order.Size = mbo.OrderSize;
        order.PendingFill = 0;
        OnRestingChange(book, mbo.InstrumentId, order.Side, order.Price, order.Size, 0);
        book.Change(order.Side, order.Price, order.Size);
    }

    // resting size change inside the band of the windows: added (> 0) or cancelled (< 0), filled separately.
    // Called before the book changes, so the distance is measured from the best price before the change.
    private void OnRestingChange(Book book, uint instrumentId, char side, long price, long change, long filled)
    {
        if (active.Count == 0 || (change == 0 && filled == 0))
            return;
        var best = side == 'B' ? book.BestBid : book.BestAsk;
        var distance = best == MboRecord.UndefinedPrice ? 0 : Math.Max(0, side == 'B' ? (best - price) / tick : (price - best) / tick);
        var w1 = 1.0 / (1 + distance);

        foreach (var s in active)
        {
            var w = s.Window;
            if (w.InstrumentId != instrumentId || price < w.BandLow || price > w.BandHigh)
                continue;
            var half = (w.BandHigh - w.BandLow) / 2.0 / tick;
            var wl = Math.Max(0.0, 1.0 - distance / half);
            var flow = s.FlowOf(side);
            if (change > 0)
            {
                flow.Add += change;
                flow.AddW1 += change * w1;
                flow.AddWl += change * wl;
            }
            else if (change < 0)
            {
                flow.Cancel += -change;
                flow.CancelW1 += -change * w1;
                flow.CancelWl += -change * wl;
            }
            flow.Fill += filled;
        }
    }

    private void WriteRow(State s)
    {
        var w = s.Window;
        var book = books[w.InstrumentId];
        var (bid, ask) = (book.BestBid, book.BestAsk);
        var askRefill = s.AskEpisode.Total;
        var bidRefill = s.BidEpisode.Total;

        var values = new List<string>
        {
            w.Id, s.Second.ToString(CultureInfo.InvariantCulture),
            s.Buy.ToString(), s.Sell.ToString(), s.Trades.ToString(),
            PriceOrEmpty(s.Last), s.High == long.MinValue ? "" : Extractor.Price(s.High),
            s.Low == long.MaxValue ? "" : Extractor.Price(s.Low),
            PriceOrEmpty(bid), PriceOrEmpty(ask),
        };
        foreach (var side in new[] { 'A', 'B' })
        {
            var f = s.FlowOf(side);
            values.AddRange(new[]
            {
                f.Add.ToString(), Number(f.AddW1), Number(f.AddWl),
                f.Cancel.ToString(), Number(f.CancelW1), Number(f.CancelWl), f.Fill.ToString(),
                book.SizeBetween(side, w.BandLow, w.BandHigh, tick).ToString(),
            });
        }
        values.AddRange(new[]
        {
            (askRefill - s.AskRefillWritten).ToString(), s.Ask.EpisodesUp.ToString(), s.Ask.EpisodesDown.ToString(),
            (bidRefill - s.BidRefillWritten).ToString(), s.Bid.EpisodesUp.ToString(), s.Bid.EpisodesDown.ToString(),
        });
        for (var k = 0; k < LargeLimits.Length; k++)
            values.AddRange(new[] { s.LargeBuy[k].ToString(), s.LargeSell[k].ToString() });
        values.Add(bid != MboRecord.UndefinedPrice && ask != MboRecord.UndefinedPrice && bid >= ask ? "1" : "0");
        s.Rows.Add(string.Join(",", values));

        // the next second
        s.AskRefillWritten = askRefill;
        s.BidRefillWritten = bidRefill;
        s.Buy = s.Sell = s.Trades = 0;
        s.Last = MboRecord.UndefinedPrice;
        s.High = long.MinValue;
        s.Low = long.MaxValue;
        foreach (var f in new[] { s.Ask, s.Bid })
        {
            f.Add = f.Cancel = f.Fill = f.EpisodesUp = f.EpisodesDown = 0;
            f.AddW1 = f.AddWl = f.CancelW1 = f.CancelWl = 0;
        }
        Array.Clear(s.LargeBuy);
        Array.Clear(s.LargeSell);
        s.Second++;
    }

    private static string PriceOrEmpty(long price) => price == MboRecord.UndefinedPrice ? "" : Extractor.Price(price);

    private static string Number(double value) => value.ToString("0.####", CultureInfo.InvariantCulture);

    public static IEnumerable<string> Header()
    {
        var columns = new List<string> { "window_id", "second", "buy", "sell", "trades", "last", "high", "low", "bid", "ask" };
        foreach (var side in new[] { "ask", "bid" })
            columns.AddRange(new[] { "add", "add_w1", "add_wl", "cancel", "cancel_w1", "cancel_wl", "fill", "rest" }
                .Select(c => $"{side}_{c}"));
        columns.AddRange(new[] { "ask_refill", "ask_ep_up", "ask_ep_down", "bid_refill", "bid_ep_up", "bid_ep_down" });
        foreach (var limit in LargeLimits)
            columns.AddRange(new[] { $"large_buy_{limit}", $"large_sell_{limit}" });
        columns.Add("crossed");
        return columns;
    }

    // rows of the windows in start order
    public IEnumerable<string> Rows() => states.Where(s => s.Started).SelectMany(s => s.Rows);

    public static List<PivotWindow> ReadWindows(string path)
    {
        var windows = new List<PivotWindow>();
        foreach (var line in File.ReadLines(path).Skip(1))
        {
            // window_id,instrument_id,start_second,end_second,band_low,band_high
            var p = line.Split(',');
            long Raw(int i) => (long)Math.Round(double.Parse(p[i], CultureInfo.InvariantCulture) * 1e9);
            windows.Add(new PivotWindow
            {
                Id = p[0],
                InstrumentId = uint.Parse(p[1], CultureInfo.InvariantCulture),
                StartSecond = long.Parse(p[2], CultureInfo.InvariantCulture),
                EndSecond = long.Parse(p[3], CultureInfo.InvariantCulture),
                BandLow = Raw(4),
                BandHigh = Raw(5),
            });
        }
        return windows;
    }
}

public static class PivotProcessor
{
    // the windows that start in the UTC day of the file
    public static void Process(string path, string outDirectory, List<PivotWindow> allWindows, bool force, object consoleSync)
    {
        var name = FileProcessor.BaseName(path);
        var output = Path.Combine(outDirectory, "pivots", name + ".csv.gz");
        if (!force && File.Exists(output))
        {
            lock (consoleSync) Console.WriteLine($"{name}: already done, skipped");
            return;
        }

        var stopwatch = System.Diagnostics.Stopwatch.StartNew();
        using var stream = DbnReader.Open(path);
        var metadata = DbnReader.ReadMetadata(stream);
        var start = (long)(metadata.Start / 1_000_000_000);
        var end = metadata.End == MboRecord.UndefinedTimestamp ? start + 86_400 : (long)(metadata.End / 1_000_000_000);
        var windows = allWindows.Where(w => w.StartSecond >= start && w.StartSecond < end).ToList();

        var collector = new PivotFlowCollector(windows);
        if (windows.Count > 0)
        {
            DbnReader.ReadRecords(stream, collector.Process);
            collector.Finish();
        }

        Directory.CreateDirectory(Path.GetDirectoryName(output)!);
        var temp = output + ".tmp";
        using (var file = File.Create(temp))
        using (var gzip = new GZipStream(file, CompressionLevel.Optimal))
        using (var writer = new StreamWriter(gzip, new UTF8Encoding(false), 1 << 16))
        {
            writer.WriteLine(string.Join(",", PivotFlowCollector.Header()));
            foreach (var row in collector.Rows())
                writer.WriteLine(row);
        }
        File.Move(temp, output, true);

        lock (consoleSync)
            Console.WriteLine(windows.Count == 0
                ? $"{name}: no windows"
                : $"{name}: {windows.Count} windows in {stopwatch.Elapsed.TotalSeconds:0.0} s");
    }
}
