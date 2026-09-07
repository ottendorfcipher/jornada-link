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

/// One record as a store presents it: opaque id, neutral record, optional version tag.
public struct SyncItem: Sendable {
    public let id: String
    public let record: any SyncRecord
    public let version: String?

    public init(id: String, record: any SyncRecord, version: String? = nil) {
        self.id = id
        self.record = record
        self.version = version
    }

    public var fingerprint: String { record.fingerprint }
    public var matchKey: String { record.matchKey }
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
