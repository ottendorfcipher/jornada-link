import Foundation

/// The device side of a sync: one Pocket Outlook database read and written over
/// RAPI (jornada/pim/store.py `DeviceStore`). Every write is preceded, once per
/// session, by a JSON snapshot of the whole database under the Jornada Backup
/// folder — the object store is battery-backed RAM and a sync must never be the
/// only copy of a record. Used from one background queue at a time (the RAPI
/// service queue), which is why the small caches need no locking.
public final class DeviceStore: SyncStore, @unchecked Sendable {
    public let name = "device"
    private let client: RapiClient
    private let codec: PimCodec
    private let snapshotDirectory: URL
    private let log: @Sendable (String) -> Void
    /// Raw records by oid as last listed (plus the ones created since); feeds updates and the snapshot.
    private var raw: [UInt32: Record] = [:]
    private var snapshotTaken: URL?

    /// `~/Documents/Jornada Backup/PIM Snapshots` (next to the send mirror, which honours JORNADA_MIRROR_DIR).
    public static var defaultSnapshotDirectory: URL {
        SendMirror.root.deletingLastPathComponent().appendingPathComponent("PIM Snapshots")
    }

    public init(client: RapiClient, codec: PimCodec, snapshotDirectory: URL? = nil,
                log: @escaping @Sendable (String) -> Void = { _ in }) {
        self.client = client
        self.codec = codec
        self.snapshotDirectory = snapshotDirectory ?? Self.defaultSnapshotDirectory
        self.log = log
    }

    public var database: String { codec.database }

    // MARK: - Database handle

    private func open() throws -> UInt32 {
        if let info = try client.findDatabase(named: codec.database) {
            return try client.openDatabase(oid: info.oid)
        }
        guard codec.createIfMissing else {
            throw StoreError("the device has no database called '\(codec.database)'")
        }
        let oid = try client.createDatabase(named: codec.database, type: codec.databaseType)
        log("created database '\(codec.database)' on the device")
        return try client.openDatabase(oid: oid)
    }

    private func records() throws -> [Record] {
        let handle = try open()
        defer { try? client.closeHandle(handle) }
        return try client.readAllRecords(handle: handle)
    }

    /// Run `work` with an open handle, closing it afterwards.
    private func withHandle<T>(_ work: (UInt32) throws -> T) throws -> T {
        let handle = try open()
        defer { try? client.closeHandle(handle) }
        return try work(handle)
    }

    // MARK: - SyncStore

    public func list() throws -> [SyncItem] {
        let records = try records()
        raw = Dictionary(records.map { ($0.oid, $0) }, uniquingKeysWith: { _, last in last })
        return records.map { record in
            do {
                let decoded = try codec.decode(record)
                return SyncItem(id: String(record.oid), record: decoded, readOnly: codec.isReadOnly(decoded))
            } catch {
                log(String(format: "record 0x%08x cannot be decoded (%@); it is left alone", record.oid, "\(error)"))
                return SyncItem.unreadable(id: String(record.oid), problem: "\(error)")
            }
        }
    }

    /// A record that is recurring, or that cannot be decoded at all, is never rewritten or deleted.
    private func isReadOnly(_ existing: Record) -> Bool {
        guard let decoded = try? codec.decode(existing) else { return true }
        return codec.isReadOnly(decoded)
    }

    public func create(_ record: any SyncRecord) throws -> String {
        let props = try codec.encode(record, nil)
        try ensureSnapshot()
        let oid = try withHandle { try client.writeRecord(handle: $0, props: props) }
        raw[oid] = Record(oid: oid, props: props)
        return String(oid)
    }

    public func update(id: String, record: any SyncRecord) throws -> String? {
        let oid = try Self.oid(from: id)
        let existing = raw[oid]
        if let existing, isReadOnly(existing) {
            throw StoreError(String(format: "record 0x%08x is recurring; the device copy is left unchanged", oid))
        }
        let props = try codec.encode(record, existing)
        try ensureSnapshot()
        try withHandle { _ = try client.writeRecord(handle: $0, props: props, oid: oid) }
        return nil
    }

    public func delete(id: String) throws {
        let oid = try Self.oid(from: id)
        if let existing = raw[oid], isReadOnly(existing) {
            throw StoreError(String(format: "record 0x%08x is recurring; the device copy is left unchanged", oid))
        }
        try ensureSnapshot()
        try withHandle { try client.deleteRecord(handle: $0, oid: oid) }
        raw.removeValue(forKey: oid)
    }

    static func oid(from id: String) throws -> UInt32 {
        guard let oid = UInt32(id) else { throw StoreError("'\(id)' is not a device record id") }
        return oid
    }

    // MARK: - Safety net

    private func ensureSnapshot() throws {
        if snapshotTaken == nil { snapshotTaken = try snapshot() }
    }

    /// Write every raw record of the database as JSON; returns the file URL.
    /// Same file naming and layout as Python's `DeviceStore.snapshot`, so
    /// `jornada db restore` can read it.
    @discardableResult
    public func snapshot(directory: URL? = nil, now: Date = Date()) throws -> URL {
        let targetDirectory = directory ?? snapshotDirectory
        try FileManager.default.createDirectory(at: targetDirectory, withIntermediateDirectories: true)
        let records = raw.isEmpty ? try self.records() : raw.values.sorted { $0.oid < $1.oid }
        let stamp = Self.stamp(now)
        let safe = String(codec.database.map { $0.isLetter || $0.isNumber ? $0 : "_" })
        let target = targetDirectory.appendingPathComponent("\(safe).\(stamp).json")
        let payload: KeyValuePairs<String, Any> = [
            "database": codec.database, "created": stamp, "records": records.map(Self.json(of:)),
        ]
        let text = try PythonJSON.dumps(payload, indent: 1, ensureASCII: false)
        try AtomicFile.write(Data(text.utf8), to: target)
        log("snapshot of '\(codec.database)' (\(records.count) records) → \(target.path)")
        return target
    }

    static func stamp(_ date: Date) -> String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyyMMdd-HHmmss"
        return formatter.string(from: date)
    }

    /// `Record.to_json()` of jornada/cedb.py: oid plus id/kind/value/flags per property.
    static func json(of record: Record) -> KeyValuePairs<String, Any> {
        ["oid": record.oid, "props": record.props.map(json(of:))]
    }

    static func json(of prop: PropVal) -> KeyValuePairs<String, Any> {
        ["id": prop.propId, "kind": prop.kind.name, "value": json(of: prop.value), "flags": prop.flags]
    }

    static func json(of value: PropValue) -> Any {
        switch value {
        case .int(let number): return number
        case .uint(let number): return number
        case .bool(let flag): return flag
        case .double(let number): return number
        case .filetime(let ticks): return ticks
        case .string(let text): return text
        case .blob(let data), .raw(let data): return data.map { String(format: "%02x", $0) }.joined()
        case .missing: return NSNull()
        }
    }
}

/// Write-to-temporary-then-rename, as Python's `os.replace` based writers do.
public enum AtomicFile {
    public static func write(_ data: Data, to target: URL) throws {
        let temporary = target.deletingLastPathComponent()
            .appendingPathComponent(".\(target.lastPathComponent).\(ProcessInfo.processInfo.processIdentifier).tmp")
        try data.write(to: temporary)
        guard rename(temporary.path, target.path) == 0 else {
            try? FileManager.default.removeItem(at: temporary)
            throw CocoaError(.fileWriteUnknown, userInfo: [NSFilePathErrorKey: target.path])
        }
    }
}
