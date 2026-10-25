import Foundation

/// The device codecs for the three Pocket Outlook databases (jornada/pim/codecs.py,
/// jornada/pim/common.py, jornada/pim/store.py `Codec`).

/// How a database's records map to a neutral model.
public struct PimCodec: Sendable {
    public let database: String
    public let decode: @Sendable (Record) throws -> any SyncRecord
    /// Properties for CeWriteRecordProps; `existing` lets cleared fields be deleted.
    public let encode: @Sendable (any SyncRecord, Record?) throws -> [PropVal]
    public let isReadOnly: @Sendable (any SyncRecord) -> Bool
    public let createIfMissing: Bool
    public let databaseType: UInt32

    public init(database: String, decode: @escaping @Sendable (Record) throws -> any SyncRecord,
                encode: @escaping @Sendable (any SyncRecord, Record?) throws -> [PropVal],
                isReadOnly: @escaping @Sendable (any SyncRecord) -> Bool = { _ in false },
                createIfMissing: Bool = false, databaseType: UInt32 = 0) {
        self.database = database
        self.decode = decode
        self.encode = encode
        self.isReadOnly = isReadOnly
        self.createIfMissing = createIfMissing
        self.databaseType = databaseType
    }

    public static let appointments = PimCodec(
        database: PimIds.appointmentsDatabase,
        decode: { AppointmentCodec.decode($0) },
        encode: { record, existing in AppointmentCodec.encode(try expect(record), existing: existing) },
        isReadOnly: { ($0 as? Appointment).map(AppointmentCodec.isReadOnly) ?? false })

    public static let contacts = PimCodec(
        database: PimIds.contactsDatabase,
        decode: { ContactCodec.decode($0) },
        encode: { record, existing in ContactCodec.encode(try expect(record), existing: existing) })

    public static let tasks = PimCodec(
        database: PimIds.tasksDatabase,
        decode: { TaskCodec.decode($0) },
        encode: { record, existing in TaskCodec.encode(try expect(record), existing: existing) })

    public static let all = [appointments, contacts, tasks]

    public static func codec(for databaseName: String) -> PimCodec? {
        all.first { $0.database.caseInsensitiveCompare(databaseName) == .orderedSame }
    }

    /// The same codec with `createIfMissing` changed (Python's `replace(codec, create_if_missing=...)`).
    public func with(createIfMissing: Bool) -> PimCodec {
        PimCodec(database: database, decode: decode, encode: encode, isReadOnly: isReadOnly,
                 createIfMissing: createIfMissing, databaseType: databaseType)
    }

    static func expect<Model: SyncRecord>(_ record: any SyncRecord) throws -> Model {
        guard let model = record as? Model else {
            throw StoreError("expected a \(Model.self) record, got \(type(of: record))")
        }
        return model
    }
}

/// Helpers shared by the codecs (jornada/pim/common.py). Property writers return
/// the properties to append, so a codec builds its list by concatenation.
enum PimCodecSupport {
    static func splitCategories(_ value: PropValue?) -> [String] {
        guard case .string(let text)? = value else { return [] }
        return text.split(separator: ",").map { PimText.clean(String($0)) }.filter { !$0.isEmpty }
    }

    static func joinCategories(_ categories: [String]) -> String {
        categories.map(PimText.clean).filter { !$0.isEmpty }.joined(separator: ",")
    }

    static func string(_ record: Record, _ propId: UInt16) -> String {
        record.string(propId) ?? ""
    }

    /// Integer-like values (`int_of`): i2/i4/ui2/ui4, bool as 0/1, a FILETIME's ticks; else `fallback`.
    static func int(_ record: Record, _ propId: UInt16, default fallback: Int = 0) -> Int {
        switch record.value(propId) {
        case .int(let number)?: return Int(number)
        case .uint(let number)?: return Int(number)
        case .bool(let flag)?: return flag ? 1 : 0
        case .filetime(let ticks)?: return Int(clamping: ticks)
        default: return fallback
        }
    }

    /// FILETIME ticks from a filetime (or non-negative integer) property.
    static func ticks(_ record: Record, _ propId: UInt16) -> UInt64? {
        switch record.value(propId) {
        case .filetime(let ticks)?: return ticks
        case .int(let number)? where number >= 0: return UInt64(number)
        case .uint(let number)?: return UInt64(number)
        default: return nil
        }
    }

    static func date(_ record: Record, _ propId: UInt16) -> NaiveDate? {
        ticks(record, propId).flatMap(DeviceTime.date(fromFiletime:))
    }

    static func hasProp(_ existing: Record?, _ propId: UInt16) -> Bool {
        existing?.get(propId) != nil
    }

    /// A string property, or a deletion when it was set before and is now empty.
    static func stringProps(_ propId: UInt16, _ value: String, existing: Record?) -> [PropVal] {
        let cleaned = PimText.clean(value)
        if !cleaned.isEmpty { return [.string(propId, cleaned)] }
        return hasProp(existing, propId) ? [.deleted(propId: propId, kind: .string)] : []
    }

    static func notesProps(_ propId: UInt16, _ text: String, existing: Record?) -> [PropVal] {
        if !PimText.clean(text).isEmpty { return [.blob(propId, NotesBlob.encode(text))] }
        return hasProp(existing, propId) ? [.deleted(propId: propId, kind: .blob)] : []
    }

    static func categoriesProps(_ categories: [String], existing: Record?) -> [PropVal] {
        stringProps(PimIds.categories, joinCategories(categories), existing: existing)
    }

    static func dateProps(_ propId: UInt16, _ day: NaiveDate?, existing: Record?) -> [PropVal] {
        if let day { return [.filetime(propId, DeviceTime.filetime(from: day))] }
        return hasProp(existing, propId) ? [.deleted(propId: propId, kind: .filetime)] : []
    }

    static func notes(_ record: Record) -> String {
        switch record.value(PimIds.notes) {
        case .string(let text)?: return text.replacingOccurrences(of: "\r\n", with: "\n")
        case .blob(let data)?: return NotesBlob.decode(data)
        default: return NotesBlob.decode(nil)
        }
    }
}
