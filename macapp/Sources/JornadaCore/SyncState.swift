import Foundation

/// Per-account sync state (jornada/sync/state.py): which device record is linked
/// to which remote item, and the content hashes both sides had when they were
/// last in agreement. The JSON layout and the file location match the Python
/// CLI's, so the two can share a state file.

public struct SyncLink: Hashable, Sendable {
    public let localId: String
    public let remoteId: String
    public let localHash: String
    public let remoteHash: String

    public init(localId: String, remoteId: String, localHash: String, remoteHash: String) {
        self.localId = localId
        self.remoteId = remoteId
        self.localHash = localHash
        self.remoteHash = remoteHash
    }
}

public struct SyncState: Hashable, Sendable {
    public static let version = 1

    public let links: [SyncLink]
    public let lastSync: String?
    public let remoteToken: String?

    public init(links: [SyncLink] = [], lastSync: String? = nil, remoteToken: String? = nil) {
        self.links = links
        self.lastSync = lastSync
        self.remoteToken = remoteToken
    }

    public init(dict: [String: Any]) {
        let links = (dict["links"] as? [Any] ?? []).compactMap { entry -> SyncLink? in
            guard let link = entry as? [String: Any], let local = link["local"], let remote = link["remote"] else {
                return nil
            }
            return SyncLink(localId: String(describing: local), remoteId: String(describing: remote),
                            localHash: link["local_hash"].map { String(describing: $0) } ?? "",
                            remoteHash: link["remote_hash"].map { String(describing: $0) } ?? "")
        }
        self.init(links: links, lastSync: dict["last_sync"] as? String, remoteToken: dict["remote_token"] as? String)
    }

    public func byLocal() -> [String: SyncLink] {
        Dictionary(links.map { ($0.localId, $0) }, uniquingKeysWith: { _, last in last })
    }

    public func byRemote() -> [String: SyncLink] {
        Dictionary(links.map { ($0.remoteId, $0) }, uniquingKeysWith: { _, last in last })
    }

    /// The state with `link` added, replacing any link that shares either id.
    public func with(_ link: SyncLink) -> SyncState {
        let kept = links.filter { $0.localId != link.localId && $0.remoteId != link.remoteId }
        return SyncState(links: kept + [link], lastSync: lastSync, remoteToken: remoteToken)
    }

    public func without(localId: String? = nil, remoteId: String? = nil) -> SyncState {
        let kept = links.filter { link in
            !((localId != nil && link.localId == localId) || (remoteId != nil && link.remoteId == remoteId))
        }
        return SyncState(links: kept, lastSync: lastSync, remoteToken: remoteToken)
    }

    public func with(lastSync stamp: String?) -> SyncState {
        SyncState(links: links, lastSync: stamp, remoteToken: remoteToken)
    }

    public func with(links: [SyncLink]) -> SyncState {
        SyncState(links: links, lastSync: lastSync, remoteToken: remoteToken)
    }

    public func toDict() -> [String: Any] {
        ["version": Self.version, "last_sync": DictField.optional(lastSync),
         "remote_token": DictField.optional(remoteToken),
         "links": links.map { ["local": $0.localId, "remote": $0.remoteId,
                               "local_hash": $0.localHash, "remote_hash": $0.remoteHash] as [String: Any] }]
    }
}

/// Where the sync state lives (jornada/sync/accounts.py): `$JORNADA_SYNC_DIR` or
/// `~/.jornada-link/sync`, with one `state/<module>-<account>.json` per account.
public enum SyncPaths {
    public static let environmentVariable = "JORNADA_SYNC_DIR"

    public static func syncDirectory() -> URL {
        if let override = ProcessInfo.processInfo.environment[environmentVariable],
           !override.trimmingCharacters(in: .whitespaces).isEmpty {
            return URL(fileURLWithPath: (override as NSString).expandingTildeInPath)
        }
        return PppController.stateDirectory().appendingPathComponent("sync")
    }

    /// Account (and module) names are path components: 1–64 of `[A-Za-z0-9_.-]`, not starting with `.`/`-`.
    public static func isValidName(_ name: String) -> Bool {
        let allowed = CharacterSet(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-")
        guard let first = name.unicodeScalars.first, (1...64).contains(name.unicodeScalars.count),
              first.properties.isAlphabetic || CharacterSet.decimalDigits.contains(first), first.isASCII else { return false }
        return name.unicodeScalars.allSatisfy { allowed.contains($0) }
    }

    public static func statePath(module: String, account: String, directory: URL? = nil) throws -> URL {
        guard isValidName(module), isValidName(account) else {
            throw StoreError("sync account name '\(account)' must be 1-64 letters, digits, '.', '_' or '-'")
        }
        return (directory ?? syncDirectory()).appendingPathComponent("state").appendingPathComponent("\(module)-\(account).json")
    }
}

public enum SyncStateFile {
    /// The saved state, or an empty one when the file is absent or unreadable.
    public static func load(_ url: URL) -> SyncState {
        guard let data = try? Data(contentsOf: url),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return SyncState() }
        return SyncState(dict: json)
    }

    /// Atomically write the state as Python's `write_state` does (indent 2, sorted keys).
    public static func save(_ state: SyncState, to url: URL) throws {
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        let text = try PythonJSON.dumps(state.toDict(), sortKeys: true, indent: 2)
        try AtomicFile.write(Data(text.utf8), to: url)
    }
}
