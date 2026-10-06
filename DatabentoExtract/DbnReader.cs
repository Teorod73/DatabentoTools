using System.Buffers.Binary;
using System.Text;
using ZstdSharp;

namespace DatabentoExtract;

// DBN metadata: only the parts the extractor needs
public sealed class DbnMetadata
{
    public int Version { get; init; }
    public string Dataset { get; init; } = "";
    public ushort Schema { get; init; }
    public ulong Start { get; init; }
    public ulong End { get; init; }
    public byte StypeIn { get; init; }
    public byte StypeOut { get; init; }
    public List<string> Symbols { get; } = [];
    public int MappingCount { get; set; }

    // instrument id -> raw symbol (e.g. ESH4 or ESH4-ESM4) from the symbology mappings
    public Dictionary<uint, string> InstrumentSymbols { get; } = [];
}

// MBO record, layout of DBN v1-v3 (56 bytes)
public readonly struct MboRecord
{
    public const byte RType = 0xA0;
    public const int Size = 56;
    public const long UndefinedPrice = long.MaxValue;
    public const ulong UndefinedTimestamp = ulong.MaxValue;

    public const byte FlagLast = 0x80;
    public const byte FlagSnapshot = 0x20;

    public readonly uint InstrumentId;
    public readonly ulong TsEvent;
    public readonly ulong OrderId;
    public readonly long Price;            // 1e-9 units
    public readonly uint OrderSize;
    public readonly byte Flags;
    public readonly char Action;           // A add, C cancel, M modify, R clear, T trade, F fill, N none
    public readonly char Side;             // A ask, B bid, N none
    public readonly ulong TsRecv;
    public readonly uint Sequence;

    public MboRecord(ReadOnlySpan<byte> record)
    {
        InstrumentId = BinaryPrimitives.ReadUInt32LittleEndian(record[4..]);
        TsEvent = BinaryPrimitives.ReadUInt64LittleEndian(record[8..]);
        OrderId = BinaryPrimitives.ReadUInt64LittleEndian(record[16..]);
        Price = BinaryPrimitives.ReadInt64LittleEndian(record[24..]);
        OrderSize = BinaryPrimitives.ReadUInt32LittleEndian(record[32..]);
        Flags = record[36];
        Action = (char)record[38];
        Side = (char)record[39];
        TsRecv = BinaryPrimitives.ReadUInt64LittleEndian(record[40..]);
        Sequence = BinaryPrimitives.ReadUInt32LittleEndian(record[52..]);
    }

    // the matching engine time if known, otherwise the receive time
    public ulong Timestamp => TsEvent != UndefinedTimestamp ? TsEvent : TsRecv;
}

public delegate void RecordHandler(byte rtype, ReadOnlySpan<byte> record);

public static class DbnReader
{
    private static readonly byte[] ZstdMagic = [0x28, 0xB5, 0x2F, 0xFD];

    // .dbn.zst (zstd, also multi frame) or plain .dbn
    public static Stream Open(string path)
    {
        var file = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read, 1 << 20, FileOptions.SequentialScan);

        Span<byte> magic = stackalloc byte[4];
        var read = file.Read(magic);
        file.Position = 0;

        if (read == 4 && magic.SequenceEqual(ZstdMagic))
            return new BufferedStream(new DecompressionStream(file), 1 << 20);

        return file;
    }

    public static DbnMetadata ReadMetadata(Stream stream)
    {
        var prefix = new byte[8];
        stream.ReadExactly(prefix);

        if (prefix[0] != 'D' || prefix[1] != 'B' || prefix[2] != 'N')
            throw new InvalidDataException("Not a DBN stream");

        var version = prefix[3];
        var length = BinaryPrimitives.ReadInt32LittleEndian(prefix.AsSpan(4));
        var body = new byte[length];
        stream.ReadExactly(body);

        return ParseMetadata(version, body);
    }

    // Fixed part: dataset[16], schema u16, start u64, end u64, limit u64, (v1: record_count u64),
    // stype_in u8, stype_out u8, ts_out u8, (v2+: symbol_cstr_len u16), reserved, together 100 bytes.
    // Then schema_definition_length u32, symbols, partial, not_found and the mappings.
    private static DbnMetadata ParseMetadata(int version, byte[] body)
    {
        var span = body.AsSpan();
        var stypeOffset = version == 1 ? 50 : 42;
        var symbolLength = version == 1 ? 22 : BinaryPrimitives.ReadUInt16LittleEndian(span[45..]);

        var metadata = new DbnMetadata
        {
            Version = version,
            Dataset = CString(span[..16]),
            Schema = BinaryPrimitives.ReadUInt16LittleEndian(span[16..]),
            Start = BinaryPrimitives.ReadUInt64LittleEndian(span[18..]),
            End = BinaryPrimitives.ReadUInt64LittleEndian(span[26..]),
            StypeIn = span[stypeOffset],
            StypeOut = span[stypeOffset + 1],
        };

        var position = 100;
        var schemaDefinitionLength = ReadUInt32(span, ref position);
        position += (int)schemaDefinitionLength;

        foreach (var symbol in ReadSymbols(span, ref position, symbolLength))
            metadata.Symbols.Add(symbol);
        ReadSymbols(span, ref position, symbolLength);    // partial
        ReadSymbols(span, ref position, symbolLength);    // not found

        var mappingCount = ReadUInt32(span, ref position);
        metadata.MappingCount = (int)mappingCount;

        for (var i = 0; i < mappingCount; i++)
        {
            var rawSymbol = CString(span.Slice(position, symbolLength));
            position += symbolLength;

            var intervalCount = ReadUInt32(span, ref position);
            for (var j = 0; j < intervalCount; j++)
            {
                position += 8;  // start and end date
                var symbol = CString(span.Slice(position, symbolLength));
                position += symbolLength;

                // the instrument id is on the output side for instrument_id output symbology
                if (uint.TryParse(symbol, out var id))
                    metadata.InstrumentSymbols[id] = rawSymbol;
                else if (uint.TryParse(rawSymbol, out id))
                    metadata.InstrumentSymbols[id] = symbol;
            }
        }

        return metadata;
    }

    // Calls the handler with every record after the metadata
    public static void ReadRecords(Stream stream, RecordHandler handler)
    {
        var buffer = new byte[1 << 22];
        int start = 0, end = 0;

        while (true)
        {
            var available = end - start;

            if (available > 0)
            {
                var length = buffer[start] * 4;
                if (length == 0)
                    throw new InvalidDataException("Record with zero length");

                if (available >= length)
                {
                    handler(buffer[start + 1], buffer.AsSpan(start, length));
                    start += length;
                    continue;
                }
            }

            // not even one whole record in the buffer: move the rest to the front and read more
            Buffer.BlockCopy(buffer, start, buffer, 0, available);
            start = 0;
            end = available;

            var read = stream.Read(buffer, end, buffer.Length - end);
            if (read == 0)
            {
                if (available > 0)
                    throw new InvalidDataException($"Truncated record at the end of the stream ({available} bytes)");
                return;
            }

            end += read;
        }
    }

    private static uint ReadUInt32(ReadOnlySpan<byte> span, ref int position)
    {
        var value = BinaryPrimitives.ReadUInt32LittleEndian(span[position..]);
        position += 4;
        return value;
    }

    private static List<string> ReadSymbols(ReadOnlySpan<byte> span, ref int position, int symbolLength)
    {
        var count = ReadUInt32(span, ref position);
        var symbols = new List<string>((int)count);

        for (var i = 0; i < count; i++)
        {
            symbols.Add(CString(span.Slice(position, symbolLength)));
            position += symbolLength;
        }

        return symbols;
    }

    private static string CString(ReadOnlySpan<byte> span)
    {
        var end = span.IndexOf((byte)0);
        return Encoding.ASCII.GetString(end < 0 ? span : span[..end]);
    }
}
