import EventKit
import Foundation
import JornadaCore

/// The Mac's Reminders (EventKit `.reminder` entity) as a `SyncStore`: every
/// reminder of one list, complete or not.
final class RemindersStore: SyncStore, @unchecked Sendable {
    static let fetchTimeout: TimeInterval = 30

    let name = "reminders"
    private let store: EKEventStore
    private let listIdentifier: String

    init(store: EKEventStore, listIdentifier: String) {
        self.store = store
        self.listIdentifier = listIdentifier
    }

    private func reminderList() throws -> EKCalendar {
        guard let list = store.calendar(withIdentifier: listIdentifier) else {
            throw StoreError("the chosen reminders list no longer exists")
        }
        return list
    }

    // MARK: - SyncStore

    func list() throws -> [SyncItem] {
        let reminders = try fetch(store.predicateForReminders(in: [try reminderList()]))
        return reminders.map { SyncItem(id: $0.calendarItemIdentifier, record: Self.record(from: $0)) }
    }

    func create(_ record: any SyncRecord) throws -> String {
        let task: PimTask = try expectRecord(record, store: "Reminders")
        let reminder = EKReminder(eventStore: store)
        reminder.calendar = try reminderList()
        Self.apply(task, to: reminder)
        try store.save(reminder, commit: true)
        return reminder.calendarItemIdentifier
    }

    func update(id: String, record: any SyncRecord) throws -> String? {
        let task: PimTask = try expectRecord(record, store: "Reminders")
        let reminder = try reminder(for: id)
        Self.apply(task, to: reminder)
        try store.save(reminder, commit: true)
        return nil
    }

    func delete(id: String) throws {
        try store.remove(try reminder(for: id), commit: true)
    }

    private func reminder(for id: String) throws -> EKReminder {
        guard let reminder = store.calendarItem(withIdentifier: id) as? EKReminder else {
            throw StoreError("the reminder no longer exists")
        }
        return reminder
    }

    /// `fetchReminders(matching:)` only reports asynchronously; the caller is on a
    /// background queue, so wait for it (bounded, so a silent EventKit cannot wedge the sync).
    private func fetch(_ predicate: NSPredicate) throws -> [EKReminder] {
        final class Box: @unchecked Sendable { var reminders: [EKReminder]? }
        let box = Box()
        let done = DispatchSemaphore(value: 0)
        _ = store.fetchReminders(matching: predicate) { found in
            box.reminders = found ?? []
            done.signal()
        }
        guard done.wait(timeout: .now() + Self.fetchTimeout) == .success, let reminders = box.reminders else {
            throw StoreError("Reminders did not answer in time")
        }
        return reminders
    }

    // MARK: - Mapping

    static func record(from reminder: EKReminder) -> PimTask {
        PimTask(summary: reminder.title ?? "", due: DeviceTime.date(from: reminder.dueDateComponents),
                start: DeviceTime.date(from: reminder.startDateComponents), completed: completion(of: reminder),
                priority: priorityName(reminder.priority), notes: reminder.notes ?? "")
    }

    static func completion(of reminder: EKReminder) -> NaiveDate? {
        guard reminder.isCompleted else { return nil }
        return reminder.completionDate.map { DeviceTime.localDate($0) } ?? DeviceTime.today()
    }

    /// Reminders priorities: 1–4 high, 5 medium, 6–9 low, 0 none.
    static func priorityName(_ value: Int) -> String {
        switch value {
        case 1...4: return "high"
        case 6...9: return "low"
        default: return "normal"
        }
    }

    static func priorityValue(_ name: String) -> Int {
        switch name {
        case "high": return 1
        case "low": return 9
        default: return 0
        }
    }

    static func components(_ day: NaiveDate) -> DateComponents {
        DateComponents(calendar: DeviceTime.calendar(), year: day.year, month: day.month, day: day.day)
    }

    static func apply(_ task: PimTask, to reminder: EKReminder) {
        let record = task.normalized()
        reminder.title = record.summary.isEmpty ? "(no subject)" : record.summary
        reminder.notes = record.notes.isEmpty ? nil : record.notes
        reminder.dueDateComponents = record.due.map(components)
        reminder.startDateComponents = record.start.map(components)
        reminder.priority = priorityValue(record.priority)
        if let completed = record.completed {
            let day = completed == TaskCodec.unknownCompletionDate ? DeviceTime.today() : completed
            reminder.isCompleted = true
            reminder.completionDate = DeviceTime.date(day)
        } else {
            reminder.isCompleted = false
        }
    }
}
