import Foundation

/// `json.dumps` as CPython formats it, so JSON written here is byte-identical to
/// the Python implementation's: `sort_keys`, `indent`, `ensure_ascii`, the
/// default `", "` / `": "` separators (`","` between items when indented), and
/// the same string escapes (`\"`, `\\`, `\n`, `\r`, `\t`, `\b`, `\f`, other
/// controls as `\u00xx`; everything else raw unless `ensureASCII`).
///
/// Values are the JSONSerialization family: `String`, `Bool`, integers,
/// `Double`, `NSNull`/`nil`, `[Any]`, `[String: Any]` (plus `KeyValuePairs<String, Any>`
/// for an object whose key order must be kept). Record fingerprints
/// hash this output, so it must never drift from CPython.
public enum PythonJSON {
    public struct UnsupportedValue: Error, CustomStringConvertible {
        public let typeName: String
        public var description: String { "cannot encode a \(typeName) as JSON" }
    }

    public static func dumps(_ value: Any, sortKeys: Bool = false, indent: Int? = nil,
                             ensureASCII: Bool = true) throws -> String {
        var output = ""
        try write(unwrap(value), into: &output, sortKeys: sortKeys, indent: indent, level: 0, ensureASCII: ensureASCII)
        return output
    }

    private static func write(_ value: Any, into output: inout String, sortKeys: Bool, indent: Int?,
                              level: Int, ensureASCII: Bool) throws {
        switch value {
        case let text as String:
            output += quoted(text, ensureASCII: ensureASCII)
        case let number as NSNumber:
            output += format(number)
        case is NSNull:
            output += "null"
        case let list as [Any]:
            try writeSequence(list.map(unwrap), into: &output, open: "[", close: "]", indent: indent, level: level) { item, out in
                try write(item, into: &out, sortKeys: sortKeys, indent: indent, level: level + 1, ensureASCII: ensureASCII)
            }
        case let pairs as KeyValuePairs<String, Any>:
            // An ordered object: what Python emits for a dict in insertion order.
            try writeSequence(Array(pairs), into: &output, open: "{", close: "}", indent: indent, level: level) { pair, out in
                out += quoted(pair.key, ensureASCII: ensureASCII) + ": "
                try write(unwrap(pair.value), into: &out, sortKeys: sortKeys, indent: indent, level: level + 1,
                          ensureASCII: ensureASCII)
            }
        case let dictionary as [String: Any]:
            let keys = sortKeys ? dictionary.keys.sorted { Array($0.utf8).lexicographicallyPrecedes(Array($1.utf8)) }
                                : Array(dictionary.keys)
            try writeSequence(keys, into: &output, open: "{", close: "}", indent: indent, level: level) { key, out in
                out += quoted(key, ensureASCII: ensureASCII) + ": "
                try write(unwrap(dictionary[key] as Any), into: &out, sortKeys: sortKeys, indent: indent,
                          level: level + 1, ensureASCII: ensureASCII)
            }
        default:
            throw UnsupportedValue(typeName: String(describing: type(of: value)))
        }
    }

    private static func writeSequence<Element>(_ items: [Element], into output: inout String, open: String,
                                               close: String, indent: Int?, level: Int,
                                               body: (Element, inout String) throws -> Void) rethrows {
        guard !items.isEmpty else {
            output += open + close
            return
        }
        let inner = indent.map { "\n" + String(repeating: " ", count: $0 * (level + 1)) } ?? ""
        let outer = indent.map { "\n" + String(repeating: " ", count: $0 * level) } ?? ""
        let separator = indent == nil ? ", " : ","
        output += open + inner
        for (index, item) in items.enumerated() {
            if index > 0 { output += separator + inner }
            try body(item, &output)
        }
        output += outer + close
    }

    /// Optionals inside `Any` become their payload or `NSNull`.
    private static func unwrap(_ value: Any) -> Any {
        let mirror = Mirror(reflecting: value)
        guard mirror.displayStyle == .optional else { return value }
        return mirror.children.first.map { unwrap($0.value) } ?? NSNull()
    }

    private static func format(_ number: NSNumber) -> String {
        if CFGetTypeID(number) == CFBooleanGetTypeID() { return number.boolValue ? "true" : "false" }
        switch String(cString: number.objCType) {
        case "d", "f": return formatDouble(number.doubleValue)
        case "Q", "L", "I", "S", "C": return String(number.uint64Value)
        default: return String(number.int64Value)
        }
    }

    /// Python's `float.__repr__` for the common cases (shortest round-trip, ".0" kept).
    private static func formatDouble(_ value: Double) -> String {
        if value.isNaN { return "NaN" }
        if value.isInfinite { return value < 0 ? "-Infinity" : "Infinity" }
        if value == value.rounded(), abs(value) < 1e16 { return String(format: "%.1f", value) }
        return "\(value)"
    }

    static func quoted(_ text: String, ensureASCII: Bool) -> String {
        var result = "\""
        for scalar in text.unicodeScalars {
            switch scalar {
            case "\"": result += "\\\""
            case "\\": result += "\\\\"
            case "\n": result += "\\n"
            case "\r": result += "\\r"
            case "\t": result += "\\t"
            case "\u{08}": result += "\\b"
            case "\u{0C}": result += "\\f"
            default:
                if scalar.value < 0x20 || (ensureASCII && scalar.value > 0x7E && scalar.value != 0x7F) {
                    result += escaped(scalar)
                } else {
                    result.unicodeScalars.append(scalar)
                }
            }
        }
        return result + "\""
    }

    private static func escaped(_ scalar: Unicode.Scalar) -> String {
        guard scalar.value > 0xFFFF else { return String(format: "\\u%04x", scalar.value) }
        let offset = scalar.value - 0x10000
        return String(format: "\\u%04x\\u%04x", 0xD800 + (offset >> 10), 0xDC00 + (offset & 0x3FF))
    }
}
