import Foundation

/// The two-sided store contract the sync engine syncs between (jornada/sync/base.py).

/// A store could not perform an operation; the message is safe to show the user.
public struct StoreError: Error, CustomStringConvertible, Sendable, Equatable {
    public let message: String

    public init(_ message: String) { self.message = message }

    public var description: String { message }
}

/// What the engine needs from a record: content hash, pairing key, display text.
public protocol SyncRecord: Sendable {
    var fingerprint: String { get }
    var matchKey: String { get }
    var label: String { get }
}

/// Stands in for a record a store found but could not decode.
public struct UnreadableRecord: SyncRecord {
    public let problem: String
    public init(problem: String) { self.problem = problem }
    public var fingerprint: String { "" }
    public var matchKey: String { "" }
    public var label: String { "(unreadable record)" }
}

/// One record as a store presents it: opaque id, neutral record, optional version tag.
/// A record the store could not decode is still listed (`problem` set) so that its
/// absence is never read as a deletion; `readOnly` marks records the store refuses
/// to change (recurring device appointments).
public struct SyncItem: Sendable {
    public let id: String
    public let record: any SyncRecord
    public let version: String?
    public let problem: String?
    public let readOnly: Bool

    public init(id: String, record: any SyncRecord, version: String? = nil, problem: String? = nil,
                readOnly: Bool = false) {
        self.id = id
        self.record = record
        self.version = version
        self.problem = problem
        self.readOnly = readOnly
    }

    public static func unreadable(id: String, problem: String) -> SyncItem {
        SyncItem(id: id, record: UnreadableRecord(problem: problem), problem: problem)
    }

    public var unreadable: Bool { problem != nil }
    public var fingerprint: String { unreadable ? "" : record.fingerprint }
    public var matchKey: String { unreadable ? "" : record.matchKey }
}

/// A store whose listing is narrowed by a predicate (a time window, say); writes pass through.
/// Apply the same filter to both sides so records outside it are absent on both.
public final class FilteredStore: SyncStore, @unchecked Sendable {
    public let name: String
    private let inner: any SyncStore
    private let keep: @Sendable (any SyncRecord) -> Bool

    public init(_ inner: any SyncStore, keep: @escaping @Sendable (any SyncRecord) -> Bool) {
        self.name = inner.name
        self.inner = inner
        self.keep = keep
    }

    public func list() throws -> [SyncItem] { try inner.list().filter { $0.unreadable || keep($0.record) } }
    public func create(_ record: any SyncRecord) throws -> String { try inner.create(record) }
    public func update(id: String, record: any SyncRecord) throws -> String? { try inner.update(id: id, record: record) }
    public func delete(id: String) throws { try inner.delete(id: id) }
}

/// Implemented by the device stores and every modern backend. Stores are used
/// from one background queue at a time; they need not be thread-safe beyond that.
public protocol SyncStore: Sendable {
    var name: String { get }
    func list() throws -> [SyncItem]
    func create(_ record: any SyncRecord) throws -> String
    func update(id: String, record: any SyncRecord) throws -> String?
    func delete(id: String) throws
}
