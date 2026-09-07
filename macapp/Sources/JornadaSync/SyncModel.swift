import Contacts
import EventKit
import Foundation
import JornadaCore
import SwiftUI

/// The Sync pane's controller: three built-in accounts — the Mac's Calendar,
/// Reminders and Contacts against the Jornada's Appointments, Tasks and
/// Contacts databases. Settings live in UserDefaults; the link state is the
/// Python CLI's JSON format under `~/.jornada-link/sync/state/<module>-app.json`.
@MainActor
final class SyncModel: ObservableObject {
    enum Account: String, CaseIterable, Identifiable {
        case calendar = "Calendar"
        case reminders = "Reminders"
        case contacts = "Contacts"

        var id: String { rawValue }

        /// The sync module name shared with the CLI (`jornada sync`), also the state file prefix.
        var module: String {
            switch self {
            case .calendar: return "calendar"
            case .reminders: return "tasks"
            case .contacts: return "contacts"
            }
        }

        var codec: PimCodec {
            switch self {
            case .calendar: return .appointments
            case .reminders: return .tasks
            case .contacts: return .contacts
            }
        }

        var database: String { codec.database }

        var symbol: String {
            switch self {
            case .calendar: return "calendar"
            case .reminders: return "checklist"
            case .contacts: return "person.crop.circle"
            }
        }

        /// What the target picker chooses: "calendar", "list" or "group".
        var targetNoun: String {
            switch self {
            case .calendar: return "calendar"
            case .reminders: return "list"
            case .contacts: return "group"
            }
        }

        var deviceNoun: String {
            switch self {
            case .calendar: return "appointments"
            case .reminders: return "tasks"
            case .contacts: return "contacts"
            }
        }
    }

    /// The account name the state files use (`calendar-app.json`, ...).
    static let accountName = "app"

    struct Settings: Equatable {
        var target: String?
        var direction: SyncDirection = .both
        var prefer: SyncPrefer = .remote
        var propagateDeletes = true

        var options: SyncOptions { SyncOptions(direction: direction, prefer: prefer, propagateDeletes: propagateDeletes) }
    }

    struct Status {
        var busy = false
        /// true = granted, false = denied, nil = not asked yet.
        var access: Bool?
        var targets: [SyncTarget] = []
        var deviceCount: Int?
        var lastSync: String?
        var plan: SyncPlan?
        var planIsPreview = true
        var summary: String?
        var errors: [String] = []
        var error: String?
    }

    @Published private(set) var settings: [Account: Settings] = [:]
    @Published private(set) var status: [Account: Status] = [:]

    private let rapi: RapiService
    private let log: (String) -> Void
    private let defaults: UserDefaults
    private let events = EKEventStore()
    private let contactStore = CNContactStore()

    init(rapi: RapiService, log: @escaping (String) -> Void, defaults: UserDefaults = .standard) {
        self.rapi = rapi
        self.log = log
        self.defaults = defaults
        for account in Account.allCases {
            settings[account] = Self.loadSettings(account, from: defaults)
            status[account] = Status(access: Self.currentAccess(account), lastSync: Self.savedLastSync(account))
        }
    }

    func settings(for account: Account) -> Settings { settings[account] ?? Settings() }
    func status(for account: Account) -> Status { status[account] ?? Status() }

    // MARK: - Settings

    func setTarget(_ target: String?, for account: Account) { change(account) { $0.target = target } }
    func setDirection(_ direction: SyncDirection, for account: Account) { change(account) { $0.direction = direction } }
    func setPrefer(_ prefer: SyncPrefer, for account: Account) { change(account) { $0.prefer = prefer } }
    func setPropagateDeletes(_ flag: Bool, for account: Account) { change(account) { $0.propagateDeletes = flag } }

    private func change(_ account: Account, _ edit: (inout Settings) -> Void) {
        var updated = settings(for: account)
        edit(&updated)
        settings[account] = updated
        Self.save(updated, for: account, to: defaults)
    }

    private static func key(_ account: Account, _ name: String) -> String { "sync.\(account.module).\(name)" }

    private static func loadSettings(_ account: Account, from defaults: UserDefaults) -> Settings {
        Settings(target: defaults.string(forKey: key(account, "target")),
                 direction: defaults.string(forKey: key(account, "direction")).flatMap(SyncDirection.init(rawValue:)) ?? .both,
                 prefer: defaults.string(forKey: key(account, "prefer")).flatMap(SyncPrefer.init(rawValue:)) ?? .remote,
                 propagateDeletes: defaults.object(forKey: key(account, "deletes")) as? Bool ?? true)
    }

    private static func save(_ settings: Settings, for account: Account, to defaults: UserDefaults) {
        defaults.set(settings.target, forKey: key(account, "target"))
        defaults.set(settings.direction.rawValue, forKey: key(account, "direction"))
        defaults.set(settings.prefer.rawValue, forKey: key(account, "prefer"))
        defaults.set(settings.propagateDeletes, forKey: key(account, "deletes"))
    }

    private static func savedLastSync(_ account: Account) -> String? {
        (try? SyncPaths.statePath(module: account.module, account: accountName)).map(SyncStateFile.load)?.lastSync
    }

    // MARK: - Status

    private func update(_ account: Account, _ edit: (inout Status) -> Void) {
        var updated = status(for: account)
        edit(&updated)
        status[account] = updated
    }

    /// Re-read access, targets and (when connected) the device record counts.
    func refresh(connected: Bool) async {
        for account in Account.allCases {
            update(account) { $0.access = Self.currentAccess(account) }
            if status(for: account).access == true { loadTargets(account) }
        }
        if connected { await refreshDeviceCounts() }
    }

    func refreshDeviceCounts() async {
        do {
            let databases = try await rapi.run("databases") { try $0.findAllDatabases() }
            for account in Account.allCases {
                let info = databases.first { $0.name.caseInsensitiveCompare(account.database) == .orderedSame }
                update(account) { $0.deviceCount = info.map { Int($0.numRecords) } }
            }
        } catch {
            log("could not list the device databases: \(error)")
        }
    }

    // MARK: - Access and targets

    private static func currentAccess(_ account: Account) -> Bool? {
        switch account {
        case .calendar: return AppleAccess.eventStatus(.event)
        case .reminders: return AppleAccess.eventStatus(.reminder)
        case .contacts: return AppleAccess.contactsStatus()
        }
    }

    /// Ask for access (the system prompt appears once) and load the targets.
    func requestAccess(_ account: Account) async {
        do {
            if try await ensureAccess(account) == false {
                update(account) { $0.error = AppleAccess.deniedMessage(account.rawValue) }
            }
        } catch {
            update(account) { $0.error = "\(error)" }
        }
    }

    private func ensureAccess(_ account: Account) async throws -> Bool {
        if status(for: account).access == true {
            if status(for: account).targets.isEmpty { loadTargets(account) }
            return true
        }
        let granted: Bool
        switch account {
        case .calendar: granted = try await events.requestFullAccessToEvents()
        case .reminders: granted = try await events.requestFullAccessToReminders()
        case .contacts: granted = try await contactStore.requestAccess(for: .contacts)
        }
        update(account) { $0.access = granted }
        if granted { loadTargets(account) }
        return granted
    }

    private func loadTargets(_ account: Account) {
        let targets: [SyncTarget]
        let preferred: String?
        switch account {
        case .calendar:
            targets = AppleAccess.calendars(events, entity: .event)
            preferred = events.defaultCalendarForNewEvents?.calendarIdentifier
        case .reminders:
            targets = AppleAccess.calendars(events, entity: .reminder)
            preferred = events.defaultCalendarForNewReminders()?.calendarIdentifier
        case .contacts:
            targets = AppleAccess.contactGroups(contactStore)
            preferred = SyncTarget.allContacts.id
        }
        update(account) { $0.targets = targets }
        let current = settings(for: account).target
        if current == nil || !targets.contains(where: { $0.id == current }) {
            setTarget(preferred ?? targets.first?.id, for: account)
        }
    }

    private func makeStore(_ account: Account) async throws -> any SyncStore {
        guard try await ensureAccess(account) else { throw StoreError(AppleAccess.deniedMessage(account.rawValue)) }
        guard let target = settings(for: account).target else {
            throw StoreError("choose a \(account.targetNoun) to sync with first")
        }
        switch account {
        case .calendar: return CalendarStore(store: events, calendarIdentifier: target)
        case .reminders: return RemindersStore(store: events, listIdentifier: target)
        case .contacts: return ContactsStore(store: contactStore, groupIdentifier: target.isEmpty ? nil : target)
        }
    }

    // MARK: - Runs

    /// Plan only: shows what a sync would do.
    func preview(_ account: Account) {
        Task { await run(account, apply: false) }
    }

    /// Plan, apply on the RAPI queue, refresh hashes, save the state.
    func sync(_ account: Account) {
        Task { await run(account, apply: true) }
    }

    private func run(_ account: Account, apply: Bool) async {
        guard !status(for: account).busy else { return }
        update(account) { $0.busy = true; $0.error = nil; $0.errors = [] }
        defer { update(account) { $0.busy = false } }
        let prefix = "sync \(account.module): "
        do {
            let remote = try await makeStore(account)
            let stateURL = try SyncPaths.statePath(module: account.module, account: Self.accountName)
            let state = SyncStateFile.load(stateURL)
            let options = settings(for: account).options
            let codec = account.codec
            let relay: @Sendable (String) -> Void = { [weak self] line in
                Task { @MainActor in self?.log(prefix + line) }
            }
            // The device side of the calendar is narrowed to the same window as Calendar.app,
            // so appointments outside it are absent on both sides rather than "deleted".
            let localFilter: (@Sendable (any SyncRecord) -> Bool)? = {
                if account == .calendar { return CalendarStore.windowFilter() }
                return nil
            }()
            let saveState: (@Sendable (SyncState) throws -> Void)? = {
                if !apply { return nil }
                return { state in try SyncStateFile.save(state, to: stateURL) }
            }()
            let outcome = try await rapi.run("sync \(account.module)") { client in
                try SyncRun.perform(client: client, codec: codec, remote: remote, state: state, options: options,
                                    apply: apply, log: relay, localFilter: localFilter, saveState: saveState)
            }
            if apply { try SyncStateFile.save(outcome.state, to: stateURL) }
            update(account) {
                $0.plan = outcome.plan
                $0.planIsPreview = !apply
                $0.deviceCount = outcome.deviceCount
                $0.errors = outcome.result?.errors ?? []
                $0.summary = apply ? "Synced: \(outcome.result?.summary() ?? "")" : "Preview: \(outcome.plan.summary())"
                if apply { $0.lastSync = outcome.state.lastSync }
            }
        } catch {
            update(account) { $0.error = "\(error)" }
            log(prefix + "failed: \(error)")
        }
    }
}
