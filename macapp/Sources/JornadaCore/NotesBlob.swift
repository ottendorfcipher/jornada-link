import Foundation

/// The NOTES blob of appointments, tasks and contacts (jornada/pim/notes_blob.py).
/// Pocket Outlook stores notes as 8-bit text in the device code page (cp1252)
/// with CRLF line ends, padded with a trailing 0x03 when the length would be
/// odd; a note may instead hold Pocket Word ink data, which is kept opaque.
public enum NotesBlob {
    static let pad: UInt8 = 0x03
    static let inkMagic = Data("{\\pwi".utf8)

    /// Neutral text (LF line ends) → device blob.
    public static func encode(_ text: String) -> Data {
        let normalized = text.replacingOccurrences(of: "\r\n", with: "\n")
            .replacingOccurrences(of: "\r", with: "\n")
            .replacingOccurrences(of: "\n", with: "\r\n")
        let raw = CodePage1252.encode(normalized)
        return raw.count % 2 == 1 ? raw + Data([pad]) : raw
    }

    /// Device blob → neutral text (LF line ends); ink notes decode to "".
    public static func decode(_ blob: Data?) -> String {
        guard let blob, !blob.isEmpty, !isInk(blob) else { return "" }
        var raw = blob
        if raw.last == pad { raw.removeLast() }
        while raw.last == 0 { raw.removeLast() }
        return CodePage1252.decode(raw).replacingOccurrences(of: "\r\n", with: "\n")
            .replacingOccurrences(of: "\r", with: "\n")
    }

    public static func isInk(_ blob: Data?) -> Bool {
        guard let blob, blob.count >= inkMagic.count else { return false }
        return blob.prefix(inkMagic.count) == inkMagic
    }
}

/// Windows-1252 exactly as CPython's codec behaves: unencodable characters
/// become "?" (`errors="replace"`) and the five undefined bytes decode to U+FFFD.
public enum CodePage1252 {
    /// Code points of bytes 0x80–0x9F; nil where cp1252 leaves the byte undefined.
    static let highTable: [UInt32?] = [
        0x20AC, nil, 0x201A, 0x0192, 0x201E, 0x2026, 0x2020, 0x2021, 0x02C6, 0x2030, 0x0160, 0x2039, 0x0152, nil, 0x017D, nil,
        nil, 0x2018, 0x2019, 0x201C, 0x201D, 0x2022, 0x2013, 0x2014, 0x02DC, 0x2122, 0x0161, 0x203A, 0x0153, nil, 0x017E, 0x0178,
    ]
    static let reverseHigh: [UInt32: UInt8] = {
        var table: [UInt32: UInt8] = [:]
        for (offset, scalar) in highTable.enumerated() {
            if let scalar { table[scalar] = UInt8(0x80 + offset) }
        }
        return table
    }()

    public static func encode(_ text: String) -> Data {
        Data(text.unicodeScalars.map { scalar -> UInt8 in
            if scalar.value < 0x80 || (0xA0...0xFF).contains(scalar.value) { return UInt8(scalar.value) }
            return reverseHigh[scalar.value] ?? UInt8(ascii: "?")
        })
    }

    public static func decode(_ data: Data) -> String {
        var scalars = String.UnicodeScalarView()
        for byte in data {
            let value: UInt32
            if (0x80...0x9F).contains(byte) {
                value = highTable[Int(byte) - 0x80] ?? 0xFFFD
            } else {
                value = UInt32(byte)
            }
            scalars.append(Unicode.Scalar(value) ?? "\u{FFFD}")
        }
        return String(scalars)
    }
}
