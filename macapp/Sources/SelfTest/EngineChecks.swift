import Foundation
import JornadaCore

// Sync engine self-test (case `engine x`): tests/test_sync_engine.py with
// in-memory stores, no network.

/// An in-memory SyncStore that keeps insertion order (like Python's dict) and can refuse operations.
final class MemoryStore: SyncStore, @unchecked Sendable {
    let name = "memory"
    private(set) var entries: [(id: String, record: any SyncRecord)]
    private let failOn: Set<String>
    private var next = 1

    init(_ records: [(String, any SyncRecord)] = [], failOn: Set<String> = []) {
        entries = records.map { (id: $0.0, record: $0.1) }
        self.failOn = failOn
    }

    var ids: [String] { entries.map(\.id) }

    subscript(id: String) -> (any SyncRecord)? { entries.first { $0.id == id }?.record }

    func set(_ id: String, _ record: any SyncRecord) {
        if let index = entries.firstIndex(where: { $0.id == id }) {
            entries[index] = (id, record)
        } else {
            entries.append((id, record))
        }
    }

    private func refuse(_ op: String) throws {
        if failOn.contains(op) { throw StoreError("\(op) refused by test store") }
    }

    func list() throws -> [SyncItem] { entries.map { SyncItem(id: $0.id, record: $0.record) } }

    func create(_ record: any SyncRecord) throws -> String {
        try refuse("create")
        let id = "m\(next)"
        next += 1
        entries.append((id, record))
        return id
    }

    func update(id: String, record: any SyncRecord) throws -> String? {
        try refuse("update")
        guard self[id] != nil else { throw StoreError("no record \(id)") }
        set(id, record)
        return nil
    }

    func delete(id: String) throws {
        try refuse("delete")
        guard let index = entries.firstIndex(where: { $0.id == id }) else { throw StoreError("no record \(id)") }
        entries.remove(at: index)
    }
}

func appt(_ summary: String, hour: Int = 9, notes: String = "") -> Appointment {
    Appointment(summary: summary, start: NaiveDateTime(2026, 9, 7, hour), end: NaiveDateTime(2026, 9, 7, hour + 1), notes: notes)
}

func linked(_ localId: String, _ remoteId: String, _ record: any SyncRecord) -> SyncLink {
    SyncLink(localId: localId, remoteId: remoteId, localHash: record.fingerprint, remoteHash: record.fingerprint)
}

func kinds(_ plan: SyncPlan) -> [SyncActionKind] { plan.actions.map(\.kind) }

func items(_ store: MemoryStore) -> [SyncItem] { (try? store.list()) ?? [] }

func notes(_ record: (any SyncRecord)?) -> String? { (record as? Appointment)?.notes }

func engineChecks() {
    firstSyncChecks()
    preferenceChecks()
    steadyStateChecks()
    conflictChecks()
    deletionChecks()
    oneWayChecks()
    errorContinuationChecks()
    refreshAndSummaryChecks()
}

func firstSyncChecks() {
    let local = MemoryStore([("1", appt("Dentist")), ("2", appt("Lunch", hour: 12))])
    let remote = MemoryStore([("r1", appt("Lunch", hour: 12)), ("r2", appt("Gym", hour: 18))])
    let plan = SyncEngine.plan(local: items(local), remote: items(remote), state: SyncState())
    let triples = plan.actions.map { "\($0.kind.rawValue)|\($0.localId ?? "-")|\($0.remoteId ?? "-")" }.sorted()
    check("first sync creates both ways and pairs matches", triples == ["create_local|-|r2", "create_remote|1|-", "link|2|r1"])
    let (state, result) = SyncEngine.apply(plan, local: local, remote: remote, state: SyncState())
    check("apply counts and no errors", result.count(.createRemote) == 1 && result.count(.link) == 1 && result.count(.createLocal) == 1
          && result.counts.count == 3 && result.errors.isEmpty)
    let pairs = Dictionary(state.links.map { ($0.localId, $0.remoteId) }, uniquingKeysWith: { first, _ in first })
    check("links after the first sync", pairs == ["1": "m1", "2": "r1", "m1": "r2"])
    check("records were created on both sides", (remote["m1"] as? Appointment) == appt("Dentist") && (local["m1"] as? Appointment) == appt("Gym", hour: 18))
    check("last_sync is stamped", state.lastSync != nil && state.lastSync?.count == 19)
    check("result summary lists the counts in order", result.summary() == "1 create remote, 1 link, 1 create local")
}

func preferenceChecks() {
    let local = MemoryStore([("1", appt("Lunch", hour: 12, notes: "device"))])
    let remote = MemoryStore([("r1", appt("Lunch", hour: 12, notes: "cloud"))])
    let plan = SyncEngine.plan(local: items(local), remote: items(remote), state: SyncState())
    check("paired records with different content follow the preference (remote)", kinds(plan) == [.link, .updateLocal])
    let plan2 = SyncEngine.plan(local: items(local), remote: items(remote), state: SyncState(), options: SyncOptions(prefer: .local))
    check("paired records with different content follow the preference (local)", kinds(plan2) == [.link, .updateRemote])
}

func steadyStateChecks() {
    let local = MemoryStore([("1", appt("A"))])
    let remote = MemoryStore([("r1", appt("A"))])
    var state = SyncState(links: [linked("1", "r1", appt("A"))])
    check("steady state plans nothing", SyncEngine.plan(local: items(local), remote: items(remote), state: state).isEmpty)
    local.set("1", appt("A", notes: "edited on device"))
    let plan = SyncEngine.plan(local: items(local), remote: items(remote), state: state)
    check("device edit flows to remote", plan.actions.map { ($0.kind, $0.reason) }.elementsEqual([(.updateRemote, "changed on the device")], by: ==))
    state = SyncEngine.apply(plan, local: local, remote: remote, state: state).state
    check("remote updated", notes(remote["r1"]) == "edited on device")
    remote.set("r1", appt("A", notes: "edited remotely"))
    let plan2 = SyncEngine.plan(local: items(local), remote: items(remote), state: state)
    check("remote edit flows to device", kinds(plan2) == [.updateLocal])
    state = SyncEngine.apply(plan2, local: local, remote: remote, state: state).state
    check("device updated and steady again", notes(local["1"]) == "edited remotely"
          && SyncEngine.plan(local: items(local), remote: items(remote), state: state).isEmpty)
}

func conflictChecks() {
    let state = SyncState(links: [linked("1", "r1", appt("A"))])
    let local = MemoryStore([("1", appt("A", notes: "x"))])
    let remote = MemoryStore([("r1", appt("A", notes: "y"))])
    let first = { (options: SyncOptions) in SyncEngine.plan(local: items(local), remote: items(remote), state: state, options: options).actions.first?.kind }
    check("conflict: remote wins by default", first(SyncOptions()) == .updateLocal)
    check("conflict: device wins when preferred", first(SyncOptions(prefer: .local)) == .updateRemote)
    check("conflict: one-way from-device pushes", first(SyncOptions(direction: .fromDevice)) == .updateRemote)
    check("conflict: one-way to-device pulls even when the device is preferred",
          first(SyncOptions(direction: .toDevice, prefer: .local)) == .updateLocal)
    let same = MemoryStore([("r1", appt("A", notes: "x"))])
    check("identical double edit just relinks", SyncEngine.plan(local: items(local), remote: items(same), state: state).actions.first?.kind == .link)
}

func deletionChecks() {
    let record = appt("A")
    let state = SyncState(links: [linked("1", "r1", record), linked("2", "r2", appt("B"))])
    let local = MemoryStore([("1", record)])            // "2" deleted on the device
    let remote = MemoryStore([("r2", appt("B"))])       // "r1" deleted remotely
    let plan = SyncEngine.plan(local: items(local), remote: items(remote), state: state)
    check("deletions propagate", kinds(plan).map(\.rawValue).sorted() == ["delete_local", "delete_remote"])
    let (newState, _) = SyncEngine.apply(plan, local: local, remote: remote, state: state)
    check("both sides emptied and links dropped", local.ids.isEmpty && remote.ids.isEmpty && newState.links.isEmpty)
    let kept = SyncEngine.plan(local: items(MemoryStore([("1", record)])), remote: items(MemoryStore([("r2", appt("B"))])), state: state,
                               options: SyncOptions(propagateDeletes: false))
    check("without propagation deletions unlink", kinds(kept) == [.unlink, .unlink])
    check("gone on both sides unlinks", kinds(SyncEngine.plan(local: [], remote: [], state: state)) == [.unlink, .unlink])
}

func oneWayChecks() {
    let local = MemoryStore([("1", appt("Device only"))])
    let remote = MemoryStore([("r1", appt("Remote only"))])
    let toDevice = SyncEngine.plan(local: items(local), remote: items(remote), state: SyncState(), options: SyncOptions(direction: .toDevice))
    check("to-device never writes remote", kinds(toDevice).map(\.rawValue).sorted() == ["create_local", "skip"] && toDevice.writesDevice)
    let fromDevice = SyncEngine.plan(local: items(local), remote: items(remote), state: SyncState(), options: SyncOptions(direction: .fromDevice))
    check("from-device never writes the device", kinds(fromDevice).map(\.rawValue).sorted() == ["create_remote", "skip"] && !fromDevice.writesDevice)
}

func errorContinuationChecks() {
    let local = MemoryStore([("1", appt("A"))], failOn: ["create"])
    let remote = MemoryStore([("r1", appt("B", hour: 10)), ("r2", appt("C", hour: 11))])
    let plan = SyncEngine.plan(local: items(local), remote: items(remote), state: SyncState())
    let (state, result) = SyncEngine.apply(plan, local: local, remote: remote, state: SyncState())
    check("apply continues after errors and reports them", result.count(.createRemote) == 1 && result.errors.count == 2
          && result.errors.allSatisfy { $0.contains("refused") } && state.links.count == 1)
    check("result summary counts the errors", result.summary() == "1 create remote; 2 error(s)")
}

func refreshAndSummaryChecks() {
    let stored = appt("A", notes: "normalized")
    let state = SyncState(links: [SyncLink(localId: "1", remoteId: "r1", localHash: "old", remoteHash: "old")])
    let refreshed = SyncEngine.refreshHashes(state, local: [SyncItem(id: "1", record: stored)], remote: [SyncItem(id: "r1", record: stored)],
                                             touchedLocal: ["1"], touchedRemote: [])
    check("refresh_hashes records what the touched side stored",
          refreshed.links.first?.localHash == stored.fingerprint && refreshed.links.first?.remoteHash == "old")
    let plan = SyncEngine.plan(local: [SyncItem(id: "1", record: PimTask(summary: "Buy milk", due: NaiveDate(2026, 1, 1)))], remote: [], state: SyncState())
    check("plan summary and describe", plan.summary() == "1 create remote" && plan.actions.first?.describe().contains("Buy milk") == true)
    check("empty plan summary", SyncEngine.plan(local: [], remote: [], state: SyncState()).summary() == "nothing to do")
    let action = SyncAction(.updateLocal, localId: "1", remoteId: "r1", record: appt("Lunch"), reason: "changed remotely")
    check("describe pads the marker like Python's {:10}", action.describe() == "→ device   Lunch  (changed remotely)")
    check("a record-less action describes as (record)", SyncAction(.unlink, localId: "1", remoteId: "r1").describe() == "≠ unlink   (record)")
}
