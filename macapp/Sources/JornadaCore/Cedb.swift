import Foundation

/// Windows CE object-store database (CEDB) records, librapi2-compatible.
/// Mirrors jornada/cedb.py: a record travels over RAPI as an array of 16-byte
/// CEPROPVAL entries followed by the string and blob payloads they point to.
/// Pointer fields are byte offsets from the start of the buffer and every
/// payload starts 4-byte aligned — what librapi2's database.c writes and what
/// its pointer fix-up expects when reading.

/// CEVT_* value types — the low word of a CEPROPID.
public enum CedbType: Hashable, Sendable {
    case i2, i4, r8, bool, ui2, ui4, string, filetime, blob
    /// A type this codec does not know; its 8-byte value union is kept raw.
    case unknown(UInt16)

    public init(code: UInt16) {
        switch code {
        case 2: self = .i2
        case 3: self = .i4
        case 5: self = .r8
        case 11: self = .bool
        case 18: self = .ui2
        case 19: self = .ui4
        case 31: self = .string
        case 64: self = .filetime
        case 65: self = .blob
        default: self = .unknown(code)
        }
    }

    /// The CEVT_* value.
    public var code: UInt16 {
        switch self {
        case .i2: return 2
        case .i4: return 3
        case .r8: return 5
        case .bool: return 11
        case .ui2: return 18
        case .ui4: return 19
        case .string: return 31
        case .filetime: return 64
        case .blob: return 65
        case .unknown(let code): return code
        }
    }

    /// Short name as jornada.cedb's `kind_name` ("i2", "string", … or "0x007f").
    public var name: String {
        switch self {
        case .i2: return "i2"
        case .i4: return "i4"
        case .r8: return "r8"
        case .bool: return "bool"
        case .ui2: return "ui2"
        case .ui4: return "ui4"
        case .string: return "string"
        case .filetime: return "filetime"
        case .blob: return "blob"
        case .unknown(let code): return String(format: "0x%04x", code)
        }
    }
}

/// A property's decoded value; which case applies follows its `CedbType`.
public enum PropValue: Hashable, Sendable {
    case int(Int32)        // i2, i4
    case uint(UInt32)      // ui2, ui4
    case bool(Bool)
    case double(Double)    // r8
    case filetime(UInt64)  // 100 ns ticks since 1601-01-01
    case string(String)
    case blob(Data)
    case raw(Data)         // unknown type: the 8-byte CEVALUNION as received
    case missing           // CEDB_PROPNOTFOUND

    /// The zero/empty value of `kind` (what a CEDB_PROPDELETE entry carries).
    static func empty(for kind: CedbType) -> PropValue {
        switch kind {
        case .i2, .i4: return .int(0)
        case .ui2, .ui4: return .uint(0)
        case .bool: return .bool(false)
        case .r8: return .double(0)
        case .filetime: return .filetime(0)
        case .string: return .string("")
        case .blob: return .blob(Data())
        case .unknown: return .raw(Data(count: 8))
        }
    }
}

/// One property of a record: identifier, CEVT type, value and wFlags.
public struct PropVal: Hashable, Sendable {
    public let propId: UInt16
    public let kind: CedbType
    public let value: PropValue
    public let flags: UInt16

    public init(propId: UInt16, kind: CedbType, value: PropValue, flags: UInt16 = 0) {
        self.propId = propId
        self.kind = kind
        self.value = value
        self.flags = flags
    }

    /// The CEPROPID: identifier in the high word, CEVT type in the low word.
    public var cepropid: UInt32 { Cedb.propid(propId, kind) }
    public var isDeleted: Bool { flags & Cedb.propDelete != 0 }
    public var isMissing: Bool { flags & Cedb.propNotFound != 0 }

    public static func i2(_ propId: UInt16, _ value: Int16) -> PropVal {
        PropVal(propId: propId, kind: .i2, value: .int(Int32(value)))
    }

    public static func i4(_ propId: UInt16, _ value: Int32) -> PropVal {
        PropVal(propId: propId, kind: .i4, value: .int(value))
    }

    public static func ui2(_ propId: UInt16, _ value: UInt16) -> PropVal {
        PropVal(propId: propId, kind: .ui2, value: .uint(UInt32(value)))
    }

    public static func ui4(_ propId: UInt16, _ value: UInt32) -> PropVal {
        PropVal(propId: propId, kind: .ui4, value: .uint(value))
    }

    public static func bool(_ propId: UInt16, _ value: Bool) -> PropVal {
        PropVal(propId: propId, kind: .bool, value: .bool(value))
    }

    public static func r8(_ propId: UInt16, _ value: Double) -> PropVal {
        PropVal(propId: propId, kind: .r8, value: .double(value))
    }

    public static func filetime(_ propId: UInt16, _ ticks: UInt64) -> PropVal {
        PropVal(propId: propId, kind: .filetime, value: .filetime(ticks))
    }

    public static func string(_ propId: UInt16, _ value: String) -> PropVal {
        PropVal(propId: propId, kind: .string, value: .string(value))
    }

    public static func blob(_ propId: UInt16, _ value: Data) -> PropVal {
        PropVal(propId: propId, kind: .blob, value: .blob(value))
    }

    /// A property marked for removal on the next write (CEDB_PROPDELETE).
    public static func deleted(propId: UInt16, kind: CedbType) -> PropVal {
        PropVal(propId: propId, kind: kind, value: .empty(for: kind), flags: Cedb.propDelete)
    }
}

/// A database record: its object identifier and properties.
public struct Record: Hashable, Sendable, Identifiable {
    public let oid: UInt32
    public let props: [PropVal]
    public var id: UInt32 { oid }

    public init(oid: UInt32, props: [PropVal]) {
        self.oid = oid
        self.props = props
    }

    public func get(_ propId: UInt16) -> PropVal? { props.first { $0.propId == propId } }
    public func value(_ propId: UInt16) -> PropValue? { get(propId)?.value }

    public func string(_ propId: UInt16) -> String? {
        if case .string(let text)? = value(propId) { return text }
        return nil
    }

    /// Signed and unsigned integer properties (i2, i4, ui2, ui4).
    public func int(_ propId: UInt16) -> Int? {
        switch value(propId) {
        case .int(let number)?: return Int(number)
        case .uint(let number)?: return Int(number)
        default: return nil
        }
    }

    public func filetime(_ propId: UInt16) -> UInt64? {
        if case .filetime(let ticks)? = value(propId) { return ticks }
        return nil
    }

    public func blob(_ propId: UInt16) -> Data? {
        if case .blob(let bytes)? = value(propId) { return bytes }
        return nil
    }
}

/// A record buffer is malformed or a value does not fit its type.
public enum CedbError: Error, CustomStringConvertible, Equatable, Sendable {
    case valueMismatch(propId: UInt16, kind: CedbType)
    case unsupportedType(propId: UInt16, kind: CedbType)
    case tooManyProperties(Int)
    case truncated(count: Int, size: Int)
    case badOffset(what: String, offset: Int, size: Int, recordSize: Int)

    public var description: String {
        switch self {
        case .valueMismatch(let propId, let kind):
            return "property \(Cedb.hex(propId)) value does not fit type \(kind.name)"
        case .unsupportedType(let propId, let kind):
            return "cannot pack property \(Cedb.hex(propId)) of type \(kind.name)"
        case .tooManyProperties(let count):
            return "\(count) properties exceed the 16-bit record property count"
        case .truncated(let count, let size):
            return "\(count) properties need \(count * Cedb.propValSize) bytes, record has \(size)"
        case .badOffset(let what, let offset, let size, let recordSize):
            return "\(what) at offset \(offset) (+\(size)) is outside the \(recordSize)-byte record"
        }
    }
}

/// The CEPROPVAL record codec (jornada.cedb.pack_record / unpack_record).
public enum Cedb {
    public static let propValSize = 16
    /// CEPROPVAL.wFlags
    public static let propNotFound: UInt16 = 0x0100
    public static let propDelete: UInt16 = 0x0200
    /// CeOpenDatabase flag: reading a record advances the cursor.
    public static let autoincrement: UInt32 = 0x0000_0001
    /// CeReadRecordProps flag: the device may grow the caller's buffer.
    public static let allowRealloc: UInt32 = 0x0000_0001
    public static let maxSortOrder = 4
    public static let maxDatabaseNameLength = 32

    /// Round `size` up to the next multiple of four (librapi2's ALIGN).
    public static func align(_ size: Int) -> Int { (size + 3) & ~3 }

    /// Compose a CEPROPID from the 16-bit property identifier and CEVT type.
    public static func propid(_ propId: UInt16, _ kind: CedbType) -> UInt32 {
        (UInt32(propId) << 16) | UInt32(kind.code)
    }

    static func hex(_ propId: UInt16) -> String { String(format: "0x%04x", propId) }

    // MARK: - Packing

    /// Serialize properties into a CeWriteRecordProps buffer.
    public static func pack(_ props: [PropVal]) throws -> Data {
        let payloads = props.map(payload(of:))
        let offsets = payloadOffsets(sizes: payloads.map(\.count), from: align(props.count * propValSize))
        let header = try zip(props, offsets).reduce(into: Data()) { data, pair in
            data.append(try entry(pair.0, payloadOffset: pair.1))
        }
        return header + payloadSection(payloads)
    }

    /// The bytes a property appends after the CEPROPVAL array (empty for scalars).
    private static func payload(of prop: PropVal) -> Data {
        switch (prop.kind, prop.value) {
        case (.string, .string(let text)): return WireWriter.wstr(text)
        case (.blob, .blob(let bytes)): return bytes
        default: return Data()
        }
    }

    /// Byte offset of each payload from the buffer start (0 for an empty payload).
    private static func payloadOffsets(sizes: [Int], from start: Int) -> [Int] {
        var next = start
        return sizes.map { size in
            defer { next = align(next + size) }
            return size == 0 ? 0 : next
        }
    }

    /// Payloads back to back, each padded to a 4-byte boundary.
    private static func payloadSection(_ payloads: [Data]) -> Data {
        payloads.reduce(into: Data()) { section, payload in
            section.append(payload)
            section.append(Data(count: align(section.count) - section.count))
        }
    }

    /// One 16-byte CEPROPVAL: propid, wLenData (0), wFlags, value union.
    private static func entry(_ prop: PropVal, payloadOffset: Int) throws -> Data {
        var writer = WireWriter()
        writer.u32(prop.cepropid)
        writer.u16(0)
        writer.u16(prop.flags)
        writer.bytes(try packValue(prop, payloadOffset: payloadOffset))
        return writer.data
    }

    /// The 8-byte CEVALUNION as librapi2's PreparePropValForWriting emits it.
    private static func packValue(_ prop: PropVal, payloadOffset: Int) throws -> Data {
        let offset = UInt32(truncatingIfNeeded: payloadOffset)
        switch (prop.kind, prop.value) {
        case (.string, .string):
            return pair(offset, 0)
        case (.blob, .blob(let bytes)):
            return pair(UInt32(truncatingIfNeeded: bytes.count), offset)
        case (.i2, .int(let number)), (.i4, .int(let number)):
            return pair(UInt32(bitPattern: number), 0)
        case (.ui2, .uint(let number)), (.ui4, .uint(let number)):
            return pair(number, 0)
        case (.bool, .bool(let flag)):
            return pair(flag ? 1 : 0, 0)
        case (.filetime, .filetime(let ticks)):
            return pair(UInt32(truncatingIfNeeded: ticks), UInt32(truncatingIfNeeded: ticks >> 32))
        case (.r8, .double(let number)):
            let bits = number.bitPattern
            return pair(UInt32(truncatingIfNeeded: bits), UInt32(truncatingIfNeeded: bits >> 32))
        case (.unknown, _):
            throw CedbError.unsupportedType(propId: prop.propId, kind: prop.kind)
        default:
            throw CedbError.valueMismatch(propId: prop.propId, kind: prop.kind)
        }
    }

    private static func pair(_ low: UInt32, _ high: UInt32) -> Data {
        var writer = WireWriter()
        writer.u32(low)
        writer.u32(high)
        return writer.data
    }

    // MARK: - Unpacking

    /// Parse a CeReadRecordProps buffer holding `count` properties.
    public static func unpack(_ data: Data, count: Int) throws -> [PropVal] {
        let buffer = RecordBuffer(data)
        guard count >= 0, count * propValSize <= buffer.count else {
            throw CedbError.truncated(count: count, size: buffer.count)
        }
        return try (0..<count).map { index in try unpackEntry(buffer, at: index * propValSize) }
    }

    private static func unpackEntry(_ buffer: RecordBuffer, at start: Int) throws -> PropVal {
        let cepropid = buffer.u32(at: start)
        let flags = buffer.u16(at: start + 6)
        let propId = UInt16(truncatingIfNeeded: cepropid >> 16)
        let kind = CedbType(code: UInt16(truncatingIfNeeded: cepropid))
        guard flags & propNotFound == 0 else {
            return PropVal(propId: propId, kind: kind, value: .missing, flags: flags)
        }
        let value = try unpackValue(kind: kind, propId: propId, buffer: buffer, union: start + 8)
        return PropVal(propId: propId, kind: kind, value: value, flags: flags)
    }

    private static func unpackValue(kind: CedbType, propId: UInt16, buffer: RecordBuffer,
                                    union: Int) throws -> PropValue {
        switch kind {
        case .string:
            return .string(try readString(buffer, at: Int(buffer.u32(at: union))))
        case .blob:
            let count = Int(buffer.u32(at: union))
            let offset = Int(buffer.u32(at: union + 4))
            return .blob(try slice(buffer, offset: offset, size: count, what: "blob \(hex(propId))"))
        case .i2: return .int(Int32(Int16(bitPattern: buffer.u16(at: union))))
        case .ui2: return .uint(UInt32(buffer.u16(at: union)))
        case .i4: return .int(Int32(bitPattern: buffer.u32(at: union)))
        case .ui4: return .uint(buffer.u32(at: union))
        case .bool: return .bool(buffer.u32(at: union) != 0)
        case .filetime: return .filetime(buffer.u64(at: union))
        case .r8: return .double(Double(bitPattern: buffer.u64(at: union)))
        case .unknown: return .raw(buffer.bytes(at: union, size: 8))
        }
    }

    /// NUL-terminated UTF-16LE string at `offset` (stops at the buffer end).
    private static func readString(_ buffer: RecordBuffer, at offset: Int) throws -> String {
        guard offset >= 0, offset < buffer.count else {
            throw CedbError.badOffset(what: "string", offset: offset, size: 0, recordSize: buffer.count)
        }
        var units: [UInt16] = []
        var index = offset
        while index + 1 < buffer.count {
            let unit = buffer.u16(at: index)
            if unit == 0 { break }
            units.append(unit)
            index += 2
        }
        return String(decoding: units, as: UTF16.self)
    }

    private static func slice(_ buffer: RecordBuffer, offset: Int, size: Int, what: String) throws -> Data {
        guard offset >= 0, size >= 0, offset + size <= buffer.count else {
            throw CedbError.badOffset(what: what, offset: offset, size: size, recordSize: buffer.count)
        }
        return buffer.bytes(at: offset, size: size)
    }
}

/// Little-endian reads over a record buffer; offsets are relative to its start
/// and callers bounds-check them.
private struct RecordBuffer {
    private let data: Data

    init(_ data: Data) { self.data = data }

    var count: Int { data.count }

    func bytes(at offset: Int, size: Int) -> Data {
        let start = data.startIndex + offset
        return data.subdata(in: start..<(start + size))
    }

    func u16(at offset: Int) -> UInt16 {
        bytes(at: offset, size: 2).withUnsafeBytes { $0.loadUnaligned(as: UInt16.self).littleEndian }
    }

    func u32(at offset: Int) -> UInt32 {
        bytes(at: offset, size: 4).withUnsafeBytes { $0.loadUnaligned(as: UInt32.self).littleEndian }
    }

    func u64(at offset: Int) -> UInt64 {
        UInt64(u32(at: offset)) | (UInt64(u32(at: offset + 4)) << 32)
    }
}

extension FileTime {
    /// Ticks (100 ns) between 1601-01-01 and 1970-01-01.
    public static let epochDelta: UInt64 = 116_444_736_000_000_000

    /// 100 ns ticks since 1601 to Date; nil for zero (jornada.cedb.filetime_to_unix).
    public static func toDate(ticks: UInt64) -> Date? {
        guard ticks != 0 else { return nil }
        let seconds = ticks >= epochDelta
            ? Double(ticks - epochDelta) / 10_000_000
            : -Double(epochDelta - ticks) / 10_000_000
        return Date(timeIntervalSince1970: seconds)
    }

    /// Date to 100 ns ticks since 1601 (jornada.cedb.unix_to_filetime); clamps at 1601.
    public static func fromDate(_ date: Date) -> UInt64 {
        let ticks = (date.timeIntervalSince1970 * 10_000_000).rounded(.toNearestOrEven)
        return UInt64(clamping: Int64(ticks) + Int64(epochDelta))
    }
}
