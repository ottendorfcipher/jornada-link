import Foundation

/// Three-way sync between a device store and a modern store (jornada/sync/engine.py).
/// `plan` is a pure function of both listings and the saved state; `apply`
/// executes a plan and returns the new state. Change detection compares each
/// record's content fingerprint with the one stored in its link, so no side
/// needs modification stamps. Records never seen before are paired by their
/// `matchKey` (subject + start, name + email, ...) so a first sync of two
/// populated sides does not duplicate everything.

public enum SyncDirection: String, CaseIterable, Sendable {
    case both = "both"
    case toDevice = "to-device"
    case fromDevice = "from-device"
}

public enum SyncPrefer: String, CaseIterable, Sendable {
    case remote
    case local
}

public struct SyncOptions: Hashable, Sendable {
    public let direction: SyncDirection
    public let prefer: SyncPrefer
    public let propagateDeletes: Bool

    public init(direction: SyncDirection = .both, prefer: SyncPrefer = .remote, propagateDeletes: Bool = true) {
        self.direction = direction
        self.prefer = prefer
        self.propagateDeletes = propagateDeletes
    }

    public var writesLocal: Bool { direction == .both || direction == .toDevice }
    public var writesRemote: Bool { direction == .both || direction == .fromDevice }
}

public enum SyncActionKind: String, CaseIterable, Sendable {
    case createLocal = "create_local"
    case updateLocal = "update_local"
    case deleteLocal = "delete_local"
    case createRemote = "create_remote"
    case updateRemote = "update_remote"
    case deleteRemote = "delete_remote"
    case link
    case unlink
    case conflict
    case skip

    /// "create local" — the kind as Python's summaries print it.
    public var words: String { rawValue.replacingOccurrences(of: "_", with: " ") }

    var marker: String {
        switch self {
        case .createLocal, .updateLocal: return "→ device"
        case .deleteLocal: return "✕ device"
        case .createRemote, .updateRemote: return "→ remote"
        case .deleteRemote: return "✕ remote"
        case .link: return "= link"
        case .unlink: return "≠ unlink"
        case .conflict: return "! conflict"
        case .skip: return "· skip"
        }
    }
}

public struct SyncAction: Sendable {
    public let kind: SyncActionKind
    public let localId: String?
    public let remoteId: String?
    public let record: (any SyncRecord)?
    public let reason: String

    public init(_ kind: SyncActionKind, localId: String? = nil, remoteId: String? = nil,
                record: (any SyncRecord)? = nil, reason: String = "") {
        self.kind = kind
        self.localId = localId
        self.remoteId = remoteId
        self.record = record
        self.reason = reason
    }

    /// `"{marker:10} {label}  (reason)"` as Python formats it.
    public func describe() -> String {
        let marker = kind.marker
        let padded = marker + String(repeating: " ", count: max(0, 10 - marker.unicodeScalars.count))
        let label = record?.label ?? "(record)"
        return "\(padded) \(label)" + (reason.isEmpty ? "" : "  (\(reason))")
    }
}

public struct SyncPlan: Sendable {
    public let actions: [SyncAction]

    public init(actions: [SyncAction] = []) { self.actions = actions }

    public func count(_ kind: SyncActionKind) -> Int { actions.filter { $0.kind == kind }.count }

    public var isEmpty: Bool { actions.allSatisfy { $0.kind == .link || $0.kind == .skip } }

    public var writesDevice: Bool {
        actions.contains { [.createLocal, .updateLocal, .deleteLocal].contains($0.kind) }
    }

    public func summary() -> String {
        let order: [SyncActionKind] = [.createLocal, .updateLocal, .deleteLocal, .createRemote, .updateRemote,
                                       .deleteRemote, .link, .conflict]
        let parts = order.compactMap { kind -> String? in
            let number = count(kind)
            return number > 0 ? "\(number) \(kind.words)" : nil
        }
        return parts.isEmpty ? "nothing to do" : parts.joined(separator: ", ")
    }
}

public struct SyncResult: Sendable {
    /// Counts in the order the kinds were first seen (Python's dict order).
    public private(set) var counts: [(kind: SyncActionKind, number: Int)] = []
    public private(set) var errors: [String] = []
    public private(set) var touchedLocal: Set<String> = []
    public private(set) var touchedRemote: Set<String> = []

    public init() {}

    public func count(_ kind: SyncActionKind) -> Int { counts.first { $0.kind == kind }?.number ?? 0 }

    func bumped(_ kind: SyncActionKind) -> SyncResult {
        var copy = self
        if let index = copy.counts.firstIndex(where: { $0.kind == kind }) {
            copy.counts[index].number += 1
        } else {
            copy.counts.append((kind, 1))
        }
        return copy
    }

    func touchingLocal(_ id: String) -> SyncResult {
        var copy = self
        copy.touchedLocal.insert(id)
        return copy
    }

    func touchingRemote(_ id: String) -> SyncResult {
        var copy = self
        copy.touchedRemote.insert(id)
        return copy
    }

    func failing(_ message: String) -> SyncResult {
        var copy = self
        copy.errors.append(message)
        return copy
    }

    public func summary() -> String {
        let parts = counts.map { "\($0.number) \($0.kind.words)" }
        let text = parts.isEmpty ? "nothing changed" : parts.joined(separator: ", ")
        return errors.isEmpty ? text : "\(text); \(errors.count) error(s)"
    }
}

public enum SyncEngine {
    // MARK: - Planning

    public static func plan(local: [SyncItem], remote: [SyncItem], state: SyncState,
                            options: SyncOptions = SyncOptions()) -> SyncPlan {
        let localById = Dictionary(local.map { ($0.id, $0) }, uniquingKeysWith: { _, last in last })
        let remoteById = Dictionary(remote.map { ($0.id, $0) }, uniquingKeysWith: { _, last in last })
        let linked = state.links.flatMap { link in
            planLink(link, local: localById[link.localId], remote: remoteById[link.remoteId], options: options)
        }
        let linkedLocal = Set(state.links.map(\.localId))
        let linkedRemote = Set(state.links.map(\.remoteId))
        let fresh = pairNew(local.filter { !linkedLocal.contains($0.id) },
                            remote.filter { !linkedRemote.contains($0.id) }, options: options)
        return SyncPlan(actions: linked + fresh)
    }

    static func planLink(_ link: SyncLink, local: SyncItem?, remote: SyncItem?, options: SyncOptions) -> [SyncAction] {
        guard let remote else {
            guard let local else {
                return [SyncAction(.unlink, localId: link.localId, remoteId: link.remoteId, reason: "gone on both sides")]
            }
            if options.propagateDeletes && options.writesLocal {
                return [SyncAction(.deleteLocal, localId: link.localId, remoteId: link.remoteId, record: local.record,
                                   reason: "deleted remotely")]
            }
            return [SyncAction(.unlink, localId: link.localId, remoteId: link.remoteId, record: local.record,
                               reason: "deleted remotely; device copy kept")]
        }
        guard let local else {
            if options.propagateDeletes && options.writesRemote {
                return [SyncAction(.deleteRemote, localId: link.localId, remoteId: link.remoteId, record: remote.record,
                                   reason: "deleted on the device")]
            }
            return [SyncAction(.unlink, localId: link.localId, remoteId: link.remoteId, record: remote.record,
                               reason: "deleted on the device; remote copy kept")]
        }
        let localChanged = local.fingerprint != link.localHash
        let remoteChanged = remote.fingerprint != link.remoteHash
        if !localChanged && !remoteChanged { return [] }
        if localChanged && remoteChanged && local.fingerprint == remote.fingerprint {
            return [SyncAction(.link, localId: local.id, remoteId: remote.id, record: local.record,
                               reason: "same change on both sides")]
        }
        if localChanged && remoteChanged { return [resolveConflict(local, remote, options: options)] }
        if localChanged { return [push(local, remote, options: options, reason: "changed on the device")] }
        return [pull(local, remote, options: options, reason: "changed remotely")]
    }

    static func push(_ local: SyncItem, _ remote: SyncItem, options: SyncOptions, reason: String) -> SyncAction {
        if options.writesRemote {
            return SyncAction(.updateRemote, localId: local.id, remoteId: remote.id, record: local.record, reason: reason)
        }
        return SyncAction(.skip, localId: local.id, remoteId: remote.id, record: local.record,
                          reason: reason + "; not writing remote")
    }

    static func pull(_ local: SyncItem, _ remote: SyncItem, options: SyncOptions, reason: String) -> SyncAction {
        if options.writesLocal {
            return SyncAction(.updateLocal, localId: local.id, remoteId: remote.id, record: remote.record, reason: reason)
        }
        return SyncAction(.skip, localId: local.id, remoteId: remote.id, record: remote.record,
                          reason: reason + "; not writing device")
    }

    /// Both sides changed: in one-way mode the source side wins, otherwise `prefer` decides.
    static func resolveConflict(_ local: SyncItem, _ remote: SyncItem, options: SyncOptions) -> SyncAction {
        let pull = SyncAction(.updateLocal, localId: local.id, remoteId: remote.id, record: remote.record,
                              reason: "changed on both sides; remote wins")
        let push = SyncAction(.updateRemote, localId: local.id, remoteId: remote.id, record: local.record,
                              reason: "changed on both sides; device wins")
        if !options.writesLocal {
            return options.writesRemote ? push : SyncAction(.conflict, localId: local.id, remoteId: remote.id,
                                                            record: local.record, reason: "changed on both sides; left as is")
        }
        if !options.writesRemote { return pull }
        return options.prefer == .remote ? pull : push
    }

    static func pairNew(_ newLocal: [SyncItem], _ newRemote: [SyncItem], options: SyncOptions) -> [SyncAction] {
        var unmatched: [String: [SyncItem]] = [:]
        var keyOrder: [String] = []
        for item in newRemote {
            if unmatched[item.matchKey] == nil { keyOrder.append(item.matchKey) }
            unmatched[item.matchKey, default: []].append(item)
        }
        var actions: [SyncAction] = []
        for item in newLocal {
            if let partner = unmatched[item.matchKey]?.first {
                unmatched[item.matchKey]?.removeFirst()
                actions += pair(item, partner, options: options)
            } else if options.writesRemote {
                actions.append(SyncAction(.createRemote, localId: item.id, record: item.record, reason: "new on the device"))
            } else {
                actions.append(SyncAction(.skip, localId: item.id, record: item.record,
                                          reason: "new on the device; not writing remote"))
            }
        }
        for key in keyOrder {
            for item in unmatched[key] ?? [] {
                if options.writesLocal {
                    actions.append(SyncAction(.createLocal, remoteId: item.id, record: item.record, reason: "new remotely"))
                } else {
                    actions.append(SyncAction(.skip, remoteId: item.id, record: item.record,
                                              reason: "new remotely; not writing device"))
                }
            }
        }
        return actions
    }

    static func pair(_ local: SyncItem, _ remote: SyncItem, options: SyncOptions) -> [SyncAction] {
        let link = SyncAction(.link, localId: local.id, remoteId: remote.id, record: local.record, reason: "matched by content")
        if local.fingerprint == remote.fingerprint { return [link] }
        return [link, resolveConflict(local, remote, options: options)]
    }

    // MARK: - Applying

    /// Execute `plan`; errors are collected per action and never stop the run.
    public static func apply(_ plan: SyncPlan, local: any SyncStore, remote: any SyncStore, state: SyncState,
                             log: (String) -> Void = { _ in }, now: () -> Date = Date.init) -> (state: SyncState, result: SyncResult) {
        var current = state
        var result = SyncResult()
        for action in plan.actions {
            do {
                (current, result) = try applyOne(action, local: local, remote: remote, state: current, result: result)
                if action.kind != .skip { log(action.describe()) }
            } catch {
                result = result.failing("\(action.describe()): \(error)")
                log("!! \(action.describe()): \(error)")
            }
        }
        return (current.with(lastSync: stamp(now())), result)
    }

    static func applyOne(_ action: SyncAction, local: any SyncStore, remote: any SyncStore, state: SyncState,
                         result: SyncResult) throws -> (SyncState, SyncResult) {
        let fingerprint = action.record?.fingerprint ?? ""
        let link = { (localId: String, remoteId: String) in
            SyncLink(localId: localId, remoteId: remoteId, localHash: fingerprint, remoteHash: fingerprint)
        }
        switch action.kind {
        case .createRemote:
            let remoteId = try remote.create(try required(action.record))
            return (state.with(link(action.localId ?? "", remoteId)), result.bumped(.createRemote).touchingRemote(remoteId))
        case .createLocal:
            let localId = try local.create(try required(action.record))
            return (state.with(link(localId, action.remoteId ?? "")), result.bumped(.createLocal).touchingLocal(localId))
        case .updateRemote:
            let remoteId = action.remoteId ?? ""
            _ = try remote.update(id: remoteId, record: try required(action.record))
            return (state.with(link(action.localId ?? "", remoteId)), result.bumped(.updateRemote).touchingRemote(remoteId))
        case .updateLocal:
            let localId = action.localId ?? ""
            _ = try local.update(id: localId, record: try required(action.record))
            return (state.with(link(localId, action.remoteId ?? "")), result.bumped(.updateLocal).touchingLocal(localId))
        case .deleteLocal:
            try local.delete(id: action.localId ?? "")
            return (state.without(localId: action.localId), result.bumped(.deleteLocal))
        case .deleteRemote:
            try remote.delete(id: action.remoteId ?? "")
            return (state.without(remoteId: action.remoteId), result.bumped(.deleteRemote))
        case .link:
            return (state.with(link(action.localId ?? "", action.remoteId ?? "")), result.bumped(.link))
        case .unlink:
            return (state.without(localId: action.localId, remoteId: action.remoteId), result.bumped(.unlink))
        case .conflict, .skip:
            return (state, result.bumped(action.kind))
        }
    }

    static func required(_ record: (any SyncRecord)?) throws -> any SyncRecord {
        guard let record else { throw StoreError("action carries no record") }
        return record
    }

    /// After writing, record what each side actually stored so normalization
    /// differences (a device that trims text, a server that reformats dates) do
    /// not read as new edits.
    public static func refreshHashes(_ state: SyncState, local: [SyncItem], remote: [SyncItem],
                                     touchedLocal: Set<String>, touchedRemote: Set<String>) -> SyncState {
        let localById = Dictionary(local.map { ($0.id, $0) }, uniquingKeysWith: { _, last in last })
        let remoteById = Dictionary(remote.map { ($0.id, $0) }, uniquingKeysWith: { _, last in last })
        let links = state.links.map { link -> SyncLink in
            let localHash = touchedLocal.contains(link.localId) ? localById[link.localId]?.fingerprint ?? link.localHash : link.localHash
            let remoteHash = touchedRemote.contains(link.remoteId) ? remoteById[link.remoteId]?.fingerprint ?? link.remoteHash : link.remoteHash
            return SyncLink(localId: link.localId, remoteId: link.remoteId, localHash: localHash, remoteHash: remoteHash)
        }
        return state.with(links: links)
    }

    /// "%Y-%m-%d %H:%M:%S" in local time, as Python's `apply` stamps `last_sync`.
    static func stamp(_ date: Date) -> String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyy-MM-dd HH:mm:ss"
        return formatter.string(from: date)
    }
}
