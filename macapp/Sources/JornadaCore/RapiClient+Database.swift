import Foundation

/// Object-store database calls (librapi2 0.9.x database.c), mirroring
/// jornada/rapi_database.py call for call: CeOpenDatabase takes an object
/// identifier (find it with `findDatabase(named:)`), CeReadRecordProps
/// returns `last_error, oid, size, count, buffer` and CeWriteRecordProps
/// sends `handle, oid, count, size, buffer` where the buffer is `Cedb.pack`.

/// One entry of a CeFindAllDatabases listing.
public struct DatabaseInfo: Hashable, Sendable, Identifiable {
    /// A sort order: the CEPROPID sorted on and its CEDB_SORT_* flags.
    public struct SortSpec: Hashable, Sendable {
        public let propid: UInt32
        public let flags: UInt32

        public init(propid: UInt32, flags: UInt32 = 0) {
            self.propid = propid
            self.flags = flags
        }
    }

    public let oid: UInt32
    public let name: String
    public let type: UInt32
    public let flags: UInt32
    public let numRecords: UInt16
    public let numSortOrder: UInt16
    public let size: UInt32
    public let lastModified: Date?
    public let sortSpecs: [SortSpec]
    public var id: UInt32 { oid }

    public init(oid: UInt32, name: String, type: UInt32 = 0, flags: UInt32 = 0, numRecords: UInt16 = 0,
                numSortOrder: UInt16 = 0, size: UInt32 = 0, lastModified: Date? = nil,
                sortSpecs: [SortSpec] = []) {
        self.oid = oid
        self.name = name
        self.type = type
        self.flags = flags
        self.numRecords = numRecords
        self.numSortOrder = numSortOrder
        self.size = size
        self.lastModified = lastModified
        self.sortSpecs = sortSpecs
    }
}

extension RapiClient.Command {
    public static let createDatabase: UInt32 = 0x0D
    public static let openDatabase: UInt32 = 0x0E
    public static let deleteDatabase: UInt32 = 0x0F
    public static let readRecordProps: UInt32 = 0x10
    public static let writeRecordProps: UInt32 = 0x11
    public static let deleteRecord: UInt32 = 0x12
    public static let seekDatabase: UInt32 = 0x13
    public static let findAllDatabases: UInt32 = 0x2C
}

extension RapiClient {
    /// CeFindAllDatabases flags: which CEDB_FIND_DATA fields the device returns.
    public struct Fad {
        public static let oid: UInt16 = 0x0001
        public static let flags: UInt16 = 0x0002
        public static let name: UInt16 = 0x0004
        public static let type: UInt16 = 0x0008
        public static let numRecords: UInt16 = 0x0010
        public static let numSortOrder: UInt16 = 0x0020
        public static let size: UInt16 = 0x0040
        public static let lastModified: UInt16 = 0x0080
        public static let sortSpecs: UInt16 = 0x0100
        public static let listing: UInt16 = oid | flags | name | type | numRecords | numSortOrder | size | lastModified
    }

    /// CeSeekDatabase seek types (CEDB_SEEK_*).
    public enum SeekType: UInt32, Sendable {
        case oid = 0x01
        case beginning = 0x02
        case end = 0x04
        case current = 0x08
    }

    /// Win32 error codes the database calls report.
    public struct WinError {
        public static let fileNotFound: UInt32 = 2
        public static let invalidHandle: UInt32 = 6
        public static let invalidParameter: UInt32 = 87
        public static let noMoreItems: UInt32 = 259
        public static let keyDeleted: UInt32 = 1018
    }

    // MARK: - Enumeration

    /// CeFindAllDatabases: every database of `type` (0 = all).
    public func findAllDatabases(type: UInt32 = 0, flags: UInt16 = Fad.listing) throws -> [DatabaseInfo] {
        var writer = WireWriter()
        writer.u32(type)
        writer.u16(flags)
        var reader = try call(Command.findAllDatabases, writer.data)
        let count = Int(try reader.u16())
        return try (0..<count).map { _ in try Self.readFindData(&reader, flags: flags) }
    }

    /// The database called `name` (case-insensitive), or nil.
    public func findDatabase(named name: String) throws -> DatabaseInfo? {
        try findAllDatabases().first { $0.name.caseInsensitiveCompare(name) == .orderedSame }
    }

    private static func readFindData(_ reader: inout WireReader, flags: UInt16) throws -> DatabaseInfo {
        let oid = flags & Fad.oid != 0 ? try reader.u32() : 0
        let nameSize = flags & Fad.name != 0 ? Int(try reader.u32()) : 0
        let dbFlags = flags & Fad.flags != 0 ? try reader.u32() : 0
        let name = flags & Fad.name != 0 ? try reader.wchars(nameSize) : ""
        let type = flags & Fad.type != 0 ? try reader.u32() : 0
        let numRecords = flags & Fad.numRecords != 0 ? try reader.u16() : 0
        let numSortOrder = flags & Fad.numSortOrder != 0 ? try reader.u16() : 0
        let size = flags & Fad.size != 0 ? try reader.u32() : 0
        let modified = flags & Fad.lastModified != 0 ? try readFileTime(&reader) : nil
        let specs = flags & Fad.sortSpecs != 0 ? try readSortSpecs(&reader) : []
        return DatabaseInfo(oid: oid, name: name, type: type, flags: dbFlags, numRecords: numRecords,
                            numSortOrder: numSortOrder, size: size, lastModified: modified, sortSpecs: specs)
    }

    private static func readFileTime(_ reader: inout WireReader) throws -> Date? {
        let low = try reader.u32()
        let high = try reader.u32()
        return FileTime.toDate(low: low, high: high)
    }

    /// Always CEDB_MAXSORTORDER entries on the wire; unused ones are (0, 0).
    private static func readSortSpecs(_ reader: inout WireReader) throws -> [DatabaseInfo.SortSpec] {
        try (0..<Cedb.maxSortOrder).map { _ in
            DatabaseInfo.SortSpec(propid: try reader.u32(), flags: try reader.u32())
        }
    }

    // MARK: - Lifecycle

    /// CeOpenDatabase by object identifier; the handle is released with `closeHandle`.
    public func openDatabase(oid: UInt32, flags: UInt32 = Cedb.autoincrement,
                             sortPropId: UInt32 = 0) throws -> UInt32 {
        var writer = WireWriter()
        writer.u32(oid)
        writer.u32(sortPropId)
        writer.u32(flags)
        let reply = try callSimple(Command.openDatabase, writer.data)
        guard reply.returnValue != Self.invalidHandle, reply.returnValue != 0 else {
            throw RapiError.remote(String(format: "open database 0x%08x", oid), winError: reply.lastError)
        }
        return reply.returnValue
    }

    /// CeCreateDatabase; returns the new database's object identifier.
    public func createDatabase(named name: String, type: UInt32 = 0,
                               sortSpecs: [DatabaseInfo.SortSpec] = []) throws -> UInt32 {
        guard sortSpecs.count <= Cedb.maxSortOrder else {
            throw RapiError.protocolError("at most \(Cedb.maxSortOrder) sort orders are allowed")
        }
        var writer = WireWriter()
        writer.u32(type)
        writer.u16(UInt16(sortSpecs.count))
        for spec in sortSpecs {
            writer.u32(spec.propid)
            writer.u32(spec.flags)
        }
        writer.string(name)
        let reply = try callSimple(Command.createDatabase, writer.data)
        guard reply.returnValue != 0 else {
            throw RapiError.remote("create database \"\(name)\"", winError: reply.lastError)
        }
        return reply.returnValue
    }

    public func deleteDatabase(oid: UInt32) throws {
        var writer = WireWriter()
        writer.u32(oid)
        try checkBool("CeDeleteDatabase", try callSimple(Command.deleteDatabase, writer.data))
    }

    // MARK: - Records

    /// CeReadRecordProps at the cursor (which advances when the database was
    /// opened with autoincrement). Returns nil once the cursor is past the last record.
    public func readRecord(handle: UInt32, flags: UInt32 = Cedb.allowRealloc) throws -> Record? {
        var writer = WireWriter()
        writer.u32(handle)
        writer.u32(flags)
        writer.u32(0)  // no property filter: every property comes back
        writer.u32(0)
        writer.u32(0)  // the device sizes the buffer
        writer.u16(0)
        var reply = try callSimple(Command.readRecordProps, writer.data)
        let size = Int(try reply.reader.u32())
        let count = Int(try reply.reader.u16())
        let data = size > 0 ? try reply.reader.take(size) : Data()
        guard reply.returnValue != 0 else {
            if reply.lastError == WinError.noMoreItems { return nil }
            throw RapiError.remote("CeReadRecordProps", winError: reply.lastError)
        }
        return Record(oid: reply.returnValue, props: try Cedb.unpack(data, count: count))
    }

    /// Every record from the cursor onwards (open the database with autoincrement).
    public func readAllRecords(handle: UInt32) throws -> [Record] {
        var records: [Record] = []
        while let record = try readRecord(handle: handle) {
            records.append(record)
        }
        return records
    }

    /// CeWriteRecordProps: `oid` 0 creates a record, otherwise updates it
    /// (listed properties replace, CEDB_PROPDELETE ones are removed). Returns the OID.
    public func writeRecord(handle: UInt32, props: [PropVal], oid: UInt32 = 0) throws -> UInt32 {
        guard props.count <= Int(UInt16.max) else { throw CedbError.tooManyProperties(props.count) }
        let data = try Cedb.pack(props)
        var writer = WireWriter()
        writer.u32(handle)
        writer.u32(oid)
        writer.u16(UInt16(props.count))
        writer.u32(UInt32(data.count))
        writer.bytes(data)
        let reply = try callSimple(Command.writeRecordProps, writer.data)
        guard reply.returnValue != 0 else {
            throw RapiError.remote("CeWriteRecordProps", winError: reply.lastError)
        }
        return reply.returnValue
    }

    public func deleteRecord(handle: UInt32, oid: UInt32) throws {
        var writer = WireWriter()
        writer.u32(handle)
        writer.u32(oid)
        try checkBool("CeDeleteRecord", try callSimple(Command.deleteRecord, writer.data))
    }

    /// CeSeekDatabase: `value` is an OID for `.oid`, otherwise a (possibly negative)
    /// record offset. Returns the record's OID (0 when nothing is there) and its index.
    public func seekDatabase(handle: UInt32, type: SeekType, value: Int = 0) throws -> (oid: UInt32, index: UInt32) {
        var writer = WireWriter()
        writer.u32(handle)
        writer.u32(type.rawValue)
        writer.u32(UInt32(truncatingIfNeeded: value))
        var reply = try callSimple(Command.seekDatabase, writer.data)
        let index = try reply.reader.u32()
        return (reply.returnValue, index)
    }

    // MARK: - Convenience

    /// Find, open and read every record of the database called `name`, then close it.
    public func readAllRecords(named name: String) throws -> (DatabaseInfo, [Record]) {
        guard let info = try findDatabase(named: name) else {
            throw RapiError.remote("find database \"\(name)\"", winError: WinError.fileNotFound)
        }
        let handle = try openDatabase(oid: info.oid)
        defer { try? closeHandle(handle) }
        return (info, try readAllRecords(handle: handle))
    }
}
