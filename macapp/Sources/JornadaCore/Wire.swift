import Foundation

/// Little-endian encode/decode for the Windows CE RAPI wire format
/// (mirrors jornada/wire.py, which is tested against the protocol).
public enum WireError: Error, CustomStringConvertible {
    case truncated(needed: Int, available: Int)
    case malformed(String)

    public var description: String {
        switch self {
        case .truncated(let needed, let available):
            return "buffer truncated: needed \(needed) bytes, \(available) available"
        case .malformed(let reason):
            return "malformed data: \(reason)"
        }
    }
}

public struct WireWriter {
    public private(set) var data = Data()
    public init() {}

    public mutating func u16(_ value: UInt16) {
        withUnsafeBytes(of: value.littleEndian) { data.append(contentsOf: $0) }
    }

    public mutating func u32(_ value: UInt32) {
        withUnsafeBytes(of: value.littleEndian) { data.append(contentsOf: $0) }
    }

    public mutating func bytes(_ raw: Data) { data.append(raw) }

    /// UTF-16LE string with NUL terminator (a Windows WCHAR array).
    public static func wstr(_ text: String) -> Data {
        var out = Data()
        for unit in text.utf16 {
            withUnsafeBytes(of: unit.littleEndian) { out.append(contentsOf: $0) }
        }
        out.append(contentsOf: [0, 0])
        return out
    }

    /// rapi_buffer_write_string: 1, chars incl. NUL, data — or 0 for nil.
    public mutating func string(_ text: String?) {
        guard let text else { u32(0); return }
        let raw = Self.wstr(text)
        u32(1)
        u32(UInt32(raw.count / 2))
        bytes(raw)
    }

    /// rapi_buffer_write_optional_string: 1, byte size, 1, data — or 0.
    public mutating func optionalString(_ text: String?) {
        guard let text else { u32(0); return }
        let raw = Self.wstr(text)
        u32(1)
        u32(UInt32(raw.count))
        u32(1)
        bytes(raw)
    }

    /// rapi_buffer_write_optional_in: 1, size, data — or 0.
    public mutating func optionalIn(_ raw: Data?) {
        guard let raw else { u32(0); return }
        u32(1)
        u32(UInt32(raw.count))
        bytes(raw)
    }

    /// rapi_buffer_write_optional_out: 1, size, 0 — or 0.
    public mutating func optionalOut(_ size: UInt32?) {
        guard let size else { u32(0); return }
        u32(1)
        u32(size)
        u32(0)
    }
}

public struct WireReader {
    private let data: Data
    private var offset: Int

    public init(_ data: Data) {
        self.data = Data(data)  // rebase indices at 0
        self.offset = 0
    }

    public var remaining: Int { data.count - offset }

    public mutating func take(_ size: Int) throws -> Data {
        guard size >= 0, offset + size <= data.count else {
            throw WireError.truncated(needed: size, available: remaining)
        }
        defer { offset += size }
        return data.subdata(in: offset..<(offset + size))
    }

    public mutating func u16() throws -> UInt16 {
        try take(2).withUnsafeBytes { $0.loadUnaligned(as: UInt16.self).littleEndian }
    }

    public mutating func u32() throws -> UInt32 {
        try take(4).withUnsafeBytes { $0.loadUnaligned(as: UInt32.self).littleEndian }
    }

    /// Read `count` UTF-16 code units, stopping the string at the first NUL.
    public mutating func wchars(_ count: Int) throws -> String {
        let raw = try take(count * 2)
        var units: [UInt16] = []
        units.reserveCapacity(count)
        var index = raw.startIndex
        while index + 1 < raw.endIndex {
            let unit = UInt16(raw[index]) | (UInt16(raw[index + 1]) << 8)
            if unit == 0 { break }
            units.append(unit)
            index += 2
        }
        return String(decoding: units, as: UTF16.self)
    }

    /// rapi_buffer_read_string: chars without NUL, then chars+1 WCHARs.
    public mutating func string() throws -> String {
        let length = Int(try u32())
        return try wchars(length + 1)
    }

    /// rapi_buffer_read_optional: data or nil.
    public mutating func optional() throws -> Data? {
        let hasParameter = try u32()
        guard hasParameter == 1 else { return nil }
        let size = Int(try u32())
        let hasValue = try u32()
        guard hasValue == 1 else { return nil }
        return try take(size)
    }
}

public enum FileTime {
    /// Win32 FILETIME (100 ns ticks since 1601) to Date; nil when zero.
    public static func toDate(low: UInt32, high: UInt32) -> Date? {
        let ticks = (UInt64(high) << 32) | UInt64(low)
        guard ticks != 0 else { return nil }
        let unixSeconds = (Double(ticks) - 116_444_736_000_000_000) / 10_000_000
        return Date(timeIntervalSince1970: unixSeconds)
    }
}
