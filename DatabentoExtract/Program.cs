using System.Diagnostics;
using DatabentoExtract;

const string Usage = """
    Databento MBO extract

    Usage:
      DatabentoExtract diag <file.dbn.zst> <outDir> [options]
          One file with diagnostics: record types, trade/fill comparison per price, the extract of the file.
      DatabentoExtract extract <inputDir|file> <outDir> [options]
          Every *.dbn and *.dbn.zst file of the directory, already extracted files are skipped.

    Options:
      --source trades|fills   volume per price from the trades (default) or from the fills
      --min-share <0..1>      instruments below this part of the largest volume of the file are not written (default 0.01)
      --parallel <n>          number of files processed at the same time (default 2)
      --force                 extract again the already extracted files

    Output (per input file):
      minute/<name>.csv.gz       utc_minute, instrument_id, price, buy, sell, unknown (aggressor volume)
      seconds/<name>.csv.gz      utc_second, instrument_id, open, high, low, close, volume, buy, sell
      instruments/<name>.csv     every instrument of the file with its symbol and record statistics
    """;

if (args.Length < 3 || args[0] is not ("diag" or "extract"))
{
    Console.WriteLine(Usage);
    return 1;
}

var mode = args[0];
var input = args[1];
var outDirectory = args[2];

var source = VolumeSource.Trades;
var minShare = 0.01;
var parallel = 2;
var force = false;

for (var i = 3; i < args.Length; i++)
{
    switch (args[i])
    {
        case "--source":
            source = args[++i].ToLowerInvariant() switch
            {
                "fills" => VolumeSource.Fills,
                "trades" => VolumeSource.Trades,
                var other => throw new ArgumentException($"Unknown source: {other}"),
            };
            break;
        case "--min-share":
            minShare = double.Parse(args[++i], System.Globalization.CultureInfo.InvariantCulture);
            break;
        case "--parallel":
            parallel = int.Parse(args[++i]);
            break;
        case "--force":
            force = true;
            break;
        default:
            Console.WriteLine($"Unknown option: {args[i]}");
            Console.WriteLine(Usage);
            return 1;
    }
}

var options = new ExtractOptions { Source = source, MinShare = minShare, Diagnostics = mode == "diag" };
var consoleSync = new object();
Directory.CreateDirectory(outDirectory);

if (mode == "diag")
{
    FileProcessor.Process(input, outDirectory, options, true, consoleSync);
    return 0;
}

var files = Directory.Exists(input)
    ? Directory.EnumerateFiles(input)
        .Where(f => f.EndsWith(".dbn", StringComparison.OrdinalIgnoreCase) || f.EndsWith(".dbn.zst", StringComparison.OrdinalIgnoreCase))
        .OrderBy(f => f, StringComparer.Ordinal)
        .ToList()
    : [input];

Console.WriteLine($"{files.Count} files, source: {source}, parallel: {parallel}");

var stopwatch = Stopwatch.StartNew();
var failed = 0;

Parallel.ForEach(files, new ParallelOptions { MaxDegreeOfParallelism = parallel }, file =>
{
    try
    {
        FileProcessor.Process(file, outDirectory, options, force, consoleSync);
    }
    catch (Exception e)
    {
        Interlocked.Increment(ref failed);
        lock (consoleSync)
            Console.WriteLine($"{Path.GetFileName(file)}: FAILED {e.GetType().Name}: {e.Message}");
    }
});

Console.WriteLine($"Done in {stopwatch.Elapsed:hh\\:mm\\:ss}, failed: {failed}");
return failed == 0 ? 0 : 2;
