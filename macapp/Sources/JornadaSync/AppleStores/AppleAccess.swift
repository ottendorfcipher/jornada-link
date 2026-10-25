import Contacts
import EventKit
import Foundation
import JornadaCore

/// A calendar, reminder list or contact group the user can sync with.
struct SyncTarget: Identifiable, Hashable, Sendable {
    let id: String
    let title: String
    let detail: String

    /// The contacts pseudo-target: every contact of the default account.
    static let allContacts = SyncTarget(id: "", title: "All Contacts", detail: "default account")
}

/// Authorization checks and target listings for the Apple data stores. Access is
/// asked for with the modern full-access APIs; a denial surfaces as a readable error.
enum AppleAccess {
    /// true = granted, false = denied/restricted, nil = not asked yet.
    static func eventStatus(_ entity: EKEntityType) -> Bool? {
        switch EKEventStore.authorizationStatus(for: entity) {
        case .fullAccess: return true
        case .notDetermined: return nil
        default: return false
        }
    }

    static func contactsStatus() -> Bool? {
        switch CNContactStore.authorizationStatus(for: .contacts) {
        case .notDetermined: return nil
        case .denied, .restricted: return false
        default: return true
        }
    }

    static func deniedMessage(_ app: String) -> String {
        "\(app) access was not granted — allow Jornada Sync under System Settings ▸ Privacy & Security ▸ \(app)"
    }

    /// Writable calendars (or reminder lists) grouped by account, as pickable targets.
    static func calendars(_ store: EKEventStore, entity: EKEntityType) -> [SyncTarget] {
        store.calendars(for: entity)
            .filter(\.allowsContentModifications)
            .sorted { ($0.source?.title ?? "", $0.title) < ($1.source?.title ?? "", $1.title) }
            .map { SyncTarget(id: $0.calendarIdentifier, title: $0.title, detail: $0.source?.title ?? "") }
    }

    /// "All Contacts" followed by every group of the default account.
    static func contactGroups(_ store: CNContactStore) -> [SyncTarget] {
        let groups = (try? store.groups(matching: nil)) ?? []
        return [SyncTarget.allContacts] + groups.map { SyncTarget(id: $0.identifier, title: $0.name, detail: "group") }
    }
}

/// Narrow a sync record to the type a store handles.
func expectRecord<Model: SyncRecord>(_ record: any SyncRecord, store: String) throws -> Model {
    guard let model = record as? Model else {
        throw StoreError("\(store) cannot store a \(type(of: record)) record")
    }
    return model
}
