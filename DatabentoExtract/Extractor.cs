using System.Diagnostics;
using System.Globalization;
using System.IO.Compression;
using System.Text;

namespace DatabentoExtract;

public enum VolumeSource
{
    // trades (T) of the aggressor: side = aggressor side. On GLBX.MDP3 a sweep is one trade record per price level
    // and the trade volume matches the exchange volume (checked on 2026-05-29 against the NinjaTrader profile).
    Trades,
    // fills (F) of the resting orders, aggressor = opposite side; about 1% more volume than the trades on GLBX.MDP3
    Fills,
}

public sealed class ExtractOptions
{
    public VolumeSource Source { get; init; } = VolumeSource.Trades;
    // instruments with less volume than this part of the largest one of the file are not written out (spreads, far contracts)
    public double MinShare { get; init; } = 0.01;
    public bool Diagnostics { get; init; }
}

// Aggregates one DBN MBO file
public sealed class Extractor(DbnMetadata metadata, ExtractOptions options)
{
    private const int Buy = 0, Sell = 1, Unknown = 2;
    private const long NanosPerSecond = 1_000_000_000;

    private sealed class InstrumentStats(uint id)
    {
        public readonly uint Id = id;
        public long Records, Adds, Cancels, Modifies, Clears, Snapshots, Others;
        public long TradeCount, FillCount;
        public readonly long[] TradeVolume = new long[3];
        public readonly long[] FillVolume = new long[3];
        public long MinPrice = long.MaxValue, MaxPrice = long.MinValue;
        public long SourceVolume;
    }

    private sealed class Bar
    {
        public long Open, High, Low, Close;
        public long Volume, Buy, Sell;
    }

    private readonly Dictionary<uint, InstrumentStats> instruments = [];
    private readonly Dictionary<(uint Id, long Minute, long Price), long[]> minuteVolumes = [];
    private readonly Dictionary<(uint Id, long Second), Bar> secondBars = [];

    // diagnostics
    private readonly Dictionary<byte, long> recordTypes = [];
    private readonly Dictionary<(char Action, char Side), long> actionSides = [];
    private readonly Dictionary<(uint Id, long Price), long[]> tradeFillByPrice = [];   // T buy, sell, none, F buy, sell, none
    private long recvOrderViolations;
    private ulong lastTsRecv;
    private readonly TradeGroupStats tradeGroups = new();

    public long RecordCount { get; private set; }

    public void Process(byte rtype, ReadOnlySpan<byte> record)
    {
        RecordCount++;

        if (options.Diagnostics)
            recordTypes[rtype] = recordTypes.GetValueOrDefault(rtype) + 1;

        if (rtype != MboRecord.RType || record.Length < MboRecord.Size)
            return;

        var mbo = new MboRecord(record);
        var stats = GetStats(mbo.InstrumentId);
        stats.Records++;

        if (options.Diagnostics)
        {
            actionSides[(mbo.Action, mbo.Side)] = actionSides.GetValueOrDefault((mbo.Action, mbo.Side)) + 1;
            if (mbo.TsRecv < lastTsRecv)
                recvOrderViolations++;
            lastTsRecv = mbo.TsRecv;
            tradeGroups.Add(mbo);
        }

        if ((mbo.Flags & MboRecord.FlagSnapshot) != 0)
        {
            stats.Snapshots++;
            return;
        }

        switch (mbo.Action)
        {
            case 'A': stats.Adds++; break;
            case 'C': stats.Cancels++; break;
            case 'M': stats.Modifies++; break;
            case 'R': stats.Clears++; break;
            case 'T': OnTrade(stats, mbo); break;
            case 'F': OnFill(stats, mbo); break;
            default: stats.Others++; break;
        }
    }

    private InstrumentStats GetStats(uint id)
    {
        if (!instruments.TryGetValue(id, out var stats))
            instruments[id] = stats = new InstrumentStats(id);
        return stats;
    }

    private void OnTrade(InstrumentStats stats, MboRecord mbo)
    {
        if (mbo.Price == MboRecord.UndefinedPrice)
            return;

        // the side of the trade is the aggressor side
        var side = mbo.Side switch { 'B' => Buy, 'A' => Sell, _ => Unknown };

        stats.TradeCount++;
        stats.TradeVolume[side] += mbo.OrderSize;
        UpdatePriceRange(stats, mbo.Price);

        if (options.Diagnostics)
            GetTradeFill(mbo.InstrumentId, mbo.Price)[side] += mbo.OrderSize;

        if (options.Source == VolumeSource.Trades)
            AddVolume(stats, mbo, side);
    }

    private void OnFill(InstrumentStats stats, MboRecord mbo)
    {
        if (mbo.Price == MboRecord.UndefinedPrice)
            return;

        // the side of the fill is the side of the resting order, the aggressor is on the other side
        var side = mbo.Side switch { 'B' => Sell, 'A' => Buy, _ => Unknown };

        stats.FillCount++;
        stats.FillVolume[side] += mbo.OrderSize;
        UpdatePriceRange(stats, mbo.Price);

        if (options.Diagnostics)
            GetTradeFill(mbo.InstrumentId, mbo.Price)[3 + side] += mbo.OrderSize;

        if (options.Source == VolumeSource.Fills)
            AddVolume(stats, mbo, side);
    }

    private static void UpdatePriceRange(InstrumentStats stats, long price)
    {
        stats.MinPrice = Math.Min(stats.MinPrice, price);
        stats.MaxPrice = Math.Max(stats.MaxPrice, price);
    }

    private long[] GetTradeFill(uint id, long price)
    {
        if (!tradeFillByPrice.TryGetValue((id, price), out var volumes))
            tradeFillByPrice[(id, price)] = volumes = new long[6];
        return volumes;
    }

    private void AddVolume(InstrumentStats stats, MboRecord mbo, int side)
    {
        var second = (long)(mbo.Timestamp / NanosPerSecond);
        var minute = second - second % 60;
        var size = (long)mbo.OrderSize;

        stats.SourceVolume += size;

        if (!minuteVolumes.TryGetValue((mbo.InstrumentId, minute, mbo.Price), out var volumes))
            minuteVolumes[(mbo.InstrumentId, minute, mbo.Price)] = volumes = new long[3];
        volumes[side] += size;

        if (!secondBars.TryGetValue((mbo.InstrumentId, second), out var bar))
            secondBars[(mbo.InstrumentId, second)] = bar = new Bar { Open = mbo.Price, High = mbo.Price, Low = mbo.Price };

        bar.High = Math.Max(bar.High, mbo.Price);
        bar.Low = Math.Min(bar.Low, mbo.Price);
        bar.Close = mbo.Price;
        bar.Volume += size;
        if (side == Buy) bar.Buy += size;
        else if (side == Sell) bar.Sell += size;
    }

    public string SymbolOf(uint id) => metadata.InstrumentSymbols.TryGetValue(id, out var symbol) ? symbol : "";

    // instruments written to the minute and second files
    private HashSet<uint> SelectedInstruments()
    {
        var max = instruments.Values.Select(i => i.SourceVolume).DefaultIfEmpty(0).Max();
        return instruments.Values
            .Where(i => i.SourceVolume > 0 && i.SourceVolume >= options.MinShare * max)
            .Select(i => i.Id)
            .ToHashSet();
    }

    public void WriteExtract(string outDirectory, string name)
    {
        var selected = SelectedInstruments();

        WriteGzip(Path.Combine(outDirectory, "minute", name + ".csv.gz"), writer =>
        {
            writer.WriteLine("utc_minute,instrument_id,price,buy,sell,unknown");
            foreach (var (key, volumes) in minuteVolumes.Where(e => selected.Contains(e.Key.Id))
                         .OrderBy(e => e.Key.Id).ThenBy(e => e.Key.Minute).ThenBy(e => e.Key.Price))
                writer.WriteLine($"{key.Minute},{key.Id},{Price(key.Price)},{volumes[Buy]},{volumes[Sell]},{volumes[Unknown]}");
        });

        WriteGzip(Path.Combine(outDirectory, "seconds", name + ".csv.gz"), writer =>
        {
            writer.WriteLine("utc_second,instrument_id,open,high,low,close,volume,buy,sell");
            foreach (var (key, bar) in secondBars.Where(e => selected.Contains(e.Key.Id))
                         .OrderBy(e => e.Key.Id).ThenBy(e => e.Key.Second))
                writer.WriteLine($"{key.Second},{key.Id},{Price(bar.Open)},{Price(bar.High)},{Price(bar.Low)},{Price(bar.Close)},{bar.Volume},{bar.Buy},{bar.Sell}");
        });

        WriteText(Path.Combine(outDirectory, "instruments", name + ".csv"), writer =>
        {
            writer.WriteLine("instrument_id,symbol,selected,records,trade_count,trade_buy,trade_sell,trade_unknown,fill_count,fill_buy,fill_sell,fill_unknown,min_price,max_price,adds,cancels,modifies,clears,snapshots,others");
            foreach (var i in instruments.Values.OrderByDescending(i => i.SourceVolume).ThenBy(i => i.Id))
                writer.WriteLine(string.Join(",",
                    i.Id, SymbolOf(i.Id), selected.Contains(i.Id) ? 1 : 0, i.Records,
                    i.TradeCount, i.TradeVolume[Buy], i.TradeVolume[Sell], i.TradeVolume[Unknown],
                    i.FillCount, i.FillVolume[Buy], i.FillVolume[Sell], i.FillVolume[Unknown],
                    i.MinPrice == long.MaxValue ? "" : Price(i.MinPrice), i.MaxPrice == long.MinValue ? "" : Price(i.MaxPrice),
                    i.Adds, i.Cancels, i.Modifies, i.Clears, i.Snapshots, i.Others));
        });
    }

    public void WriteDiagnostics(string outDirectory, string name, TimeSpan elapsed)
    {
        var main = instruments.Values.OrderByDescending(i => i.TradeVolume.Sum() + i.FillVolume.Sum()).FirstOrDefault();

        WriteText(Path.Combine(outDirectory, name + "_diag_summary.txt"), writer =>
        {
            writer.WriteLine($"File: {name}");
            writer.WriteLine($"Processing time: {elapsed.TotalSeconds:0.0} s, records: {RecordCount}");
            writer.WriteLine();
            writer.WriteLine($"DBN version: {metadata.Version}, dataset: {metadata.Dataset}, schema: {metadata.Schema}, stype in/out: {metadata.StypeIn}/{metadata.StypeOut}");
            writer.WriteLine($"Start: {Utc(metadata.Start)}, end: {Utc(metadata.End)}");
            writer.WriteLine($"Symbols: {string.Join(" ", metadata.Symbols)}");
            writer.WriteLine($"Mappings: {metadata.MappingCount}, instrument ids with symbol: {metadata.InstrumentSymbols.Count}");
            foreach (var (id, symbol) in metadata.InstrumentSymbols.OrderBy(e => e.Value).Take(40))
                writer.WriteLine($"  {id} {symbol}");
            writer.WriteLine();

            writer.WriteLine("Record types (rtype: count):");
            foreach (var (rtype, count) in recordTypes.OrderBy(e => e.Key))
                writer.WriteLine($"  0x{rtype:X2}: {count}");
            writer.WriteLine();

            writer.WriteLine("MBO action/side (count):");
            foreach (var ((action, side), count) in actionSides.OrderBy(e => e.Key.Action).ThenBy(e => e.Key.Side))
                writer.WriteLine($"  {action} {side}: {count}");
            writer.WriteLine();

            writer.WriteLine($"ts_recv order violations: {recvOrderViolations}");
            writer.WriteLine();

            tradeGroups.Write(writer);
            writer.WriteLine();

            if (main != null)
            {
                writer.WriteLine($"Largest instrument: {main.Id} {SymbolOf(main.Id)}");
                writer.WriteLine($"  trades: {main.TradeCount}, volume buy/sell/unknown: {main.TradeVolume[Buy]}/{main.TradeVolume[Sell]}/{main.TradeVolume[Unknown]}, total {main.TradeVolume.Sum()}");
                writer.WriteLine($"  fills:  {main.FillCount}, volume buy/sell/unknown: {main.FillVolume[Buy]}/{main.FillVolume[Sell]}/{main.FillVolume[Unknown]}, total {main.FillVolume.Sum()}");

                var prices = tradeFillByPrice.Where(e => e.Key.Id == main.Id).ToList();
                var differentPrices = prices.Count(e => e.Value[0] + e.Value[1] + e.Value[2] != e.Value[3] + e.Value[4] + e.Value[5]);
                var differentVolume = prices.Sum(e => Math.Abs(e.Value[0] + e.Value[1] + e.Value[2] - e.Value[3] - e.Value[4] - e.Value[5]));
                writer.WriteLine($"  price levels: {prices.Count}, levels where trade and fill volume differ: {differentPrices}, sum of the differences: {differentVolume}");
            }
        });

        if (main != null)
        {
            WriteText(Path.Combine(outDirectory, name + "_diag_compare.csv"), writer =>
            {
                writer.WriteLine("instrument_id,price,trade_buy,trade_sell,trade_unknown,fill_buy,fill_sell,fill_unknown");
                foreach (var (key, v) in tradeFillByPrice.Where(e => e.Key.Id == main.Id).OrderBy(e => e.Key.Price))
                    writer.WriteLine($"{key.Id},{Price(key.Price)},{v[0]},{v[1]},{v[2]},{v[3]},{v[4]},{v[5]}");
            });
        }
    }

    public static string Price(long price) =>
        (price / 1_000_000_000m).ToString("0.#########", CultureInfo.InvariantCulture);

    private static string Utc(ulong nanos) =>
        nanos == MboRecord.UndefinedTimestamp ? "-" : DateTime.UnixEpoch.AddTicks((long)(nanos / 100)).ToString("yyyy-MM-dd HH:mm:ss.fff", CultureInfo.InvariantCulture);

    private static void WriteGzip(string path, Action<StreamWriter> write)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        var temp = path + ".tmp";
        using (var file = File.Create(temp))
        using (var gzip = new GZipStream(file, CompressionLevel.Optimal))
        using (var writer = new StreamWriter(gzip, new UTF8Encoding(false), 1 << 16))
            write(writer);
        File.Move(temp, path, true);
    }

    private static void WriteText(string path, Action<StreamWriter> write)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        var temp = path + ".tmp";
        using (var writer = new StreamWriter(temp, false, new UTF8Encoding(false)))
            write(writer);
        File.Move(temp, path, true);
    }

    // How the fills follow the trades: one trade with fills at several prices means the trade record
    // aggregates a sweep and only the fills give the exact volume per price.
    private sealed class TradeGroupStats
    {
        private bool open;
        private uint id;
        private ulong timestamp;
        private long tradePrice, tradeSize, fillSize;
        private long fillMinPrice, fillMaxPrice;
        private int fillCount;

        private long trades, tradesWithoutFills, matched, sizeMismatch, multiPrice, multiPriceVolume, otherPriceFills;
        private long fillsWithoutTrade;

        public void Add(MboRecord mbo)
        {
            if ((mbo.Flags & MboRecord.FlagSnapshot) != 0)
                return;

            if (mbo.Action == 'T')
            {
                Close();
                open = true;
                id = mbo.InstrumentId;
                timestamp = mbo.Timestamp;
                tradePrice = mbo.Price;
                tradeSize = mbo.OrderSize;
                fillSize = 0;
                fillCount = 0;
                fillMinPrice = long.MaxValue;
                fillMaxPrice = long.MinValue;
                trades++;
            }
            else if (mbo.Action == 'F')
            {
                if (open && mbo.InstrumentId == id && mbo.Timestamp == timestamp)
                {
                    fillSize += mbo.OrderSize;
                    fillCount++;
                    fillMinPrice = Math.Min(fillMinPrice, mbo.Price);
                    fillMaxPrice = Math.Max(fillMaxPrice, mbo.Price);
                }
                else
                    fillsWithoutTrade++;
            }
            else if (mbo.InstrumentId == id)
                Close();
        }

        private void Close()
        {
            if (!open)
                return;
            open = false;

            if (fillCount == 0)
            {
                tradesWithoutFills++;
                return;
            }

            if (fillSize == tradeSize) matched++;
            else sizeMismatch++;

            if (fillMinPrice != fillMaxPrice)
            {
                multiPrice++;
                multiPriceVolume += tradeSize;
            }
            else if (fillMinPrice != tradePrice)
                otherPriceFills++;
        }

        public void Write(TextWriter writer)
        {
            Close();
            writer.WriteLine("Trades (T) and the following fills (F) with the same instrument and timestamp:");
            writer.WriteLine($"  trades: {trades}");
            writer.WriteLine($"  without fills: {tradesWithoutFills}");
            writer.WriteLine($"  fill size = trade size: {matched}, different: {sizeMismatch}");
            writer.WriteLine($"  fills at several prices: {multiPrice} (trade volume {multiPriceVolume})");
            writer.WriteLine($"  fills at one price, different from the trade price: {otherPriceFills}");
            writer.WriteLine($"  fills without a preceding trade: {fillsWithoutTrade}");
        }
    }
}

public static class FileProcessor
{
    public static void Process(string path, string outDirectory, ExtractOptions options, bool force, object consoleSync)
    {
        var name = BaseName(path);
        var marker = Path.Combine(outDirectory, "instruments", name + ".csv");

        if (!force && !options.Diagnostics && File.Exists(marker))
        {
            lock (consoleSync)
                Console.WriteLine($"{name}: already done, skipped");
            return;
        }

        var stopwatch = Stopwatch.StartNew();

        using var stream = DbnReader.Open(path);
        var metadata = DbnReader.ReadMetadata(stream);
        var extractor = new Extractor(metadata, options);
        DbnReader.ReadRecords(stream, extractor.Process);

        extractor.WriteExtract(outDirectory, name);
        if (options.Diagnostics)
            extractor.WriteDiagnostics(outDirectory, name, stopwatch.Elapsed);

        lock (consoleSync)
            Console.WriteLine($"{name}: {extractor.RecordCount:N0} records in {stopwatch.Elapsed.TotalSeconds:0.0} s");
    }

    // e.g. glbx-mdp3-20240102.mbo.dbn.zst -> glbx-mdp3-20240102
    public static string BaseName(string path)
    {
        var name = Path.GetFileName(path);
        foreach (var extension in new[] { ".zst", ".dbn", ".mbo" })
        {
            if (name.EndsWith(extension, StringComparison.OrdinalIgnoreCase))
                name = name[..^extension.Length];
        }
        return name;
    }
}
