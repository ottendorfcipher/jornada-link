import Foundation
import JornadaCore

/// One sync run on the RAPI service queue: list both sides, plan, optionally
/// apply, refresh hashes. Mirrors `run_sync` in jornada/sync_cli.py so the app
/// and the CLI behave the same way against the same state file.
enum SyncRun {
    struct Outcome: Sendable {
        let plan: SyncPlan
        let state: SyncState
        let result: SyncResult?
        let deviceCount: Int
    }

    static func perform(client: RapiClient, codec: PimCodec, remote: any SyncStore, state: SyncState,
                        options: SyncOptions, apply: Bool, log: @escaping @Sendable (String) -> Void,
                        localFilter: (@Sendable (any SyncRecord) -> Bool)? = nil,
                        saveState: (@Sendable (SyncState) throws -> Void)? = nil) throws -> Outcome {
        let raw = DeviceStore(client: client, codec: codec, log: log)
        let device: any SyncStore = localFilter.map { FilteredStore(raw, keep: $0) } ?? raw
        let localItems = try device.list()
        let remoteItems = try remote.list()
        log("device: \(localItems.count) record(s); \(remote.name): \(remoteItems.count) record(s); \(state.links.count) linked")
        let plan = SyncEngine.plan(local: localItems, remote: remoteItems, state: state, options: options)
        log("plan: \(plan.summary())")
        guard apply else {
            return Outcome(plan: plan, state: state, result: nil, deviceCount: localItems.count)
        }
        let (applied, result) = SyncEngine.apply(plan, local: device, remote: remote, state: state, log: { log("  " + $0) })
        try saveState?(applied)   // links first: a failed re-read must not lose them
        let touched = !result.touchedLocal.isEmpty || !result.touchedRemote.isEmpty
        var final = applied
        var deviceCount = localItems.count
        if touched {
            do {
                let refreshedLocal = try device.list()
                final = SyncEngine.refreshHashes(applied, local: refreshedLocal, remote: try remote.list(),
                                                 touchedLocal: result.touchedLocal, touchedRemote: result.touchedRemote)
                deviceCount = refreshedLocal.count
                try saveState?(final)
            } catch {
                log("warning: could not re-read the stores after writing (\(error)); the next run may report the records it just wrote as changed")
            }
        }
        log("done: \(result.summary())")
        return Outcome(plan: plan, state: final, result: result, deviceCount: deviceCount)
    }
}
