using System.Globalization;

namespace DatabentoExtract;

/// <summary>
/// Diagnostics of the trades above the visible resting size (diag mode): how native icebergs and implied volume
/// look in the MBO records.
///
/// An event is the records of an instrument up to the last flag (or a later timestamp). Per event and price of the
/// resting side: visible = resting size before the first trade there, trade = aggressor volume, fill = F volume.
/// A group is hidden when trade > visible. For the orders filled in the event it counts the fills larger than the
/// visible size of the order, the M records that set a larger size than what was left after the fills (display
/// refresh of an iceberg), the C records, and the new adds at the price on the resting side in the same event.
/// The records of the first hidden events of the instrument and of its next event are written out.
/// </summary>
public sealed class HiddenDiagnostics
{
    private const int DumpEvents = 40;

    private sealed class Order
    {
        public char Side;
        public long Price;
        public long Size;
    }

    private sealed class Group
    {
        public long Visible, Trade, Fill;
    }

    private sealed class Instrument
    {
        public readonly Dictionary<ulong, Order> Orders = [];
        public readonly Dictionary<long, long> Bids = [];
        public readonly Dictionary<long, long> Asks = [];

        // current event
        public ulong EventTs;
        public readonly List<MboRecord> Records = [];
        public readonly Dictionary<(char Side, long Price), Group> Groups = [];
        public readonly Dictionary<ulong, long> Filled = [];        // order -> filled size in the event
        public readonly HashSet<(char Side, long Price)> Added = [];
        public long FillsAboveDisplay, Refreshes, Cancels;
        public bool DumpNext;
        public readonly List<string> Dump = [];
        public int Dumped;

        // totals
        public long AggressorVolume, GroupCount, HiddenGroups, HiddenVolume;
        public long HiddenFullyFilled, HiddenUnfilledGroups, UnfilledVolume;
        public long HiddenWithFillAboveDisplay, HiddenWithRefresh, HiddenWithNewAdd, HiddenWithCancel;
        public long AllFillsAboveDisplay, AllRefreshes;

        public long Size(char side, long price) => (side == 'B' ? Bids : Asks).GetValueOrDefault(price);

        public void Change(char side, long price, long delta)
        {
            var levels = side == 'B' ? Bids : Asks;
            var size = levels.GetValueOrDefault(price) + delta;
            if (size > 0) levels[price] = size;
            else levels.Remove(price);
        }
    }

    private readonly Dictionary<uint, Instrument> instruments = [];

    public void Process(MboRecord mbo)
    {
        if (!instruments.TryGetValue(mbo.InstrumentId, out var x))
            instruments[mbo.InstrumentId] = x = new Instrument();

        var ts = mbo.Timestamp;
        if (x.Records.Count > 0 && ts != x.EventTs)
            EndEvent(x);
        x.EventTs = ts;
        x.Records.Add(mbo);

        switch (mbo.Action)
        {
            case 'T':
                if (mbo.Price != MboRecord.UndefinedPrice && mbo.Side is 'A' or 'B')
                {
                    var key = (mbo.Side == 'B' ? 'A' : 'B', mbo.Price);
                    if (!x.Groups.TryGetValue(key, out var group))
                        x.Groups[key] = group = new Group { Visible = x.Size(key.Item1, mbo.Price) };
                    group.Trade += mbo.OrderSize;
                    x.AggressorVolume += mbo.OrderSize;
                }
                break;
            case 'F':
                if (x.Orders.TryGetValue(mbo.OrderId, out var filled))
                {
                    var before = x.Filled.GetValueOrDefault(mbo.OrderId);
                    if (before + mbo.OrderSize > filled.Size)
                    {
                        x.FillsAboveDisplay++;
                        x.AllFillsAboveDisplay++;
                    }
                    x.Filled[mbo.OrderId] = before + mbo.OrderSize;
                    if (x.Groups.TryGetValue((filled.Side, filled.Price), out var group))
                        group.Fill += mbo.OrderSize;
                }
                break;
            case 'A':
                if (mbo.Side is 'A' or 'B' && mbo.Price != MboRecord.UndefinedPrice)
                {
                    x.Orders[mbo.OrderId] = new Order { Side = mbo.Side, Price = mbo.Price, Size = mbo.OrderSize };
                    x.Change(mbo.Side, mbo.Price, mbo.OrderSize);
                    x.Added.Add((mbo.Side, mbo.Price));
                }
                break;
            case 'C':
                if (x.Orders.TryGetValue(mbo.OrderId, out var cancelled))
                {
                    if (x.Filled.ContainsKey(mbo.OrderId))
                        x.Cancels++;
                    var reduce = Math.Min(cancelled.Size, mbo.OrderSize);
                    cancelled.Size -= reduce;
                    x.Change(cancelled.Side, cancelled.Price, -reduce);
                    if (cancelled.Size <= 0)
                        x.Orders.Remove(mbo.OrderId);
                }
                break;
            case 'M':
                if (!x.Orders.TryGetValue(mbo.OrderId, out var modified))
                {
                    if (mbo.Side is 'A' or 'B' && mbo.Price != MboRecord.UndefinedPrice)
                    {
                        x.Orders[mbo.OrderId] = new Order { Side = mbo.Side, Price = mbo.Price, Size = mbo.OrderSize };
                        x.Change(mbo.Side, mbo.Price, mbo.OrderSize);
                    }
                    break;
                }
                if (x.Filled.TryGetValue(mbo.OrderId, out var fill) && mbo.OrderSize > Math.Max(0, modified.Size - fill))
                {
                    x.Refreshes++;
                    x.AllRefreshes++;
                }
                x.Change(modified.Side, modified.Price, -modified.Size);
                modified.Side = mbo.Side;
                modified.Price = mbo.Price;
                modified.Size = mbo.OrderSize;
                x.Change(modified.Side, modified.Price, modified.Size);
                if (modified.Size <= 0)
                    x.Orders.Remove(mbo.OrderId);
                break;
            case 'R':
                x.Orders.Clear();
                x.Bids.Clear();
                x.Asks.Clear();
                break;
        }

        if ((mbo.Flags & MboRecord.FlagLast) != 0)
            EndEvent(x);
    }

    public void Finish()
    {
        foreach (var x in instruments.Values)
            if (x.Records.Count > 0)
                EndEvent(x);
    }

    private void EndEvent(Instrument x)
    {
        var hidden = false;
        foreach (var ((side, price), g) in x.Groups)
        {
            x.GroupCount++;
            if (g.Trade <= g.Visible)
                continue;
            hidden = true;
            x.HiddenGroups++;
            x.HiddenVolume += g.Trade - g.Visible;
            if (g.Fill >= g.Trade) x.HiddenFullyFilled++;
            else
            {
                x.HiddenUnfilledGroups++;
                x.UnfilledVolume += g.Trade - g.Fill;
            }
            if (x.FillsAboveDisplay > 0) x.HiddenWithFillAboveDisplay++;
            if (x.Refreshes > 0) x.HiddenWithRefresh++;
            if (x.Cancels > 0) x.HiddenWithCancel++;
            if (x.Added.Contains((side, price))) x.HiddenWithNewAdd++;
        }

        if (x.DumpNext || (hidden && x.Dumped < DumpEvents))
        {
            var groups = string.Join("; ", x.Groups.Select(e =>
                $"{e.Key.Side} {Extractor.Price(e.Key.Price)}: visible {e.Value.Visible}, trade {e.Value.Trade}, fill {e.Value.Fill}"));
            x.Dump.Add(x.DumpNext && !hidden ? "  next event:" : $"event {++x.Dumped}: {groups}");
            foreach (var r in x.Records)
                x.Dump.Add(string.Create(CultureInfo.InvariantCulture,
                    $"    {r.Timestamp} {r.InstrumentId} {r.Action} {r.Side} {(r.Price == MboRecord.UndefinedPrice ? "-" : Extractor.Price(r.Price))} {r.OrderSize} order {r.OrderId} flags 0x{r.Flags:X2} seq {r.Sequence}"));
            x.DumpNext = hidden && !x.DumpNext;
        }

        x.Records.Clear();
        x.Groups.Clear();
        x.Filled.Clear();
        x.Added.Clear();
        x.FillsAboveDisplay = x.Refreshes = x.Cancels = 0;
    }

    public void Write(StreamWriter writer, uint mainId)
    {
        if (!instruments.TryGetValue(mainId, out var x))
            return;
        double Share(long part, long whole) => whole == 0 ? 0 : 100.0 * part / whole;

        writer.WriteLine($"Trades above the visible resting size, instrument {mainId}");
        writer.WriteLine($"  aggressor volume: {x.AggressorVolume}, event and price groups with trades: {x.GroupCount}");
        writer.WriteLine($"  hidden groups (trade > visible): {x.HiddenGroups} ({Share(x.HiddenGroups, x.GroupCount):0.00}%), " +
                         $"hidden volume: {x.HiddenVolume} ({Share(x.HiddenVolume, x.AggressorVolume):0.00}% of the aggressor volume)");
        writer.WriteLine($"  of the hidden groups: fill volume >= trade {x.HiddenFullyFilled}, fill volume < trade {x.HiddenUnfilledGroups} " +
                         $"(trade volume without fill: {x.UnfilledVolume}, implied or not reported)");
        writer.WriteLine($"  of the hidden groups, the event has: a fill above the visible order size {x.HiddenWithFillAboveDisplay}, " +
                         $"an M that sets a larger size than left after the fills (refresh) {x.HiddenWithRefresh}, " +
                         $"a C of a filled order {x.HiddenWithCancel}, a new add at the price {x.HiddenWithNewAdd}");
        writer.WriteLine($"  all events: fills above the visible order size {x.AllFillsAboveDisplay}, refresh M records {x.AllRefreshes}");
        writer.WriteLine();
        writer.WriteLine($"Records of the first {x.Dumped} hidden events and of the event after them (ts_event instrument action side price size order flags sequence):");
        foreach (var line in x.Dump)
            writer.WriteLine(line);
    }
}
