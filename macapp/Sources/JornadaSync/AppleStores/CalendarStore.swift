import EventKit
import Foundation
import JornadaCore

/// The Mac's Calendar (EventKit) as a `SyncStore`: events of one calendar from
/// 30 days ago to 365 days ahead. Recurring events are listed per occurrence,
/// marked `recurring`, and never written — the device cannot model the rule and
/// the engine only ever edits one-off copies of them.
final class CalendarStore: SyncStore, @unchecked Sendable {
    static let daysBefore = 30
    static let daysAhead = 365

    let name = "calendar"
    private let store: EKEventStore
    private let calendarIdentifier: String
    private let windowStart: Date
    private let windowEnd: Date

    init(store: EKEventStore, calendarIdentifier: String, now: Date = Date()) {
        self.store = store
        self.calendarIdentifier = calendarIdentifier
        windowStart = now.addingTimeInterval(-TimeInterval(Self.daysBefore) * 86_400)
        windowEnd = now.addingTimeInterval(TimeInterval(Self.daysAhead) * 86_400)
    }

    private func calendar() throws -> EKCalendar {
        guard let calendar = store.calendar(withIdentifier: calendarIdentifier) else {
            throw StoreError("the chosen calendar no longer exists")
        }
        return calendar
    }

    // MARK: - SyncStore

    func list() throws -> [SyncItem] {
        let predicate = store.predicateForEvents(withStart: windowStart, end: windowEnd, calendars: [try calendar()])
        var seen = Set<String>()
        return store.events(matching: predicate)
            .sorted { $0.startDate < $1.startDate }
            .compactMap { event in
                let id = Self.itemId(for: event)
                guard seen.insert(id).inserted else { return nil }
                return SyncItem(id: id, record: Self.record(from: event))
            }
    }

    func create(_ record: any SyncRecord) throws -> String {
        let appointment: Appointment = try expectRecord(record, store: "Calendar")
        let event = EKEvent(eventStore: store)
        event.calendar = try calendar()
        Self.apply(appointment, to: event)
        try store.save(event, span: .thisEvent, commit: true)
        return event.calendarItemIdentifier
    }

    func update(id: String, record: any SyncRecord) throws -> String? {
        let appointment: Appointment = try expectRecord(record, store: "Calendar")
        let event = try writableEvent(for: id)
        Self.apply(appointment, to: event)
        try store.save(event, span: .thisEvent, commit: true)
        return nil
    }

    func delete(id: String) throws {
        try store.remove(try writableEvent(for: id), span: .thisEvent, commit: true)
    }

    // MARK: - Identity

    static func isRecurring(_ event: EKEvent) -> Bool { event.hasRecurrenceRules || event.isDetached }

    /// One-off events are their calendar item; occurrences add their original date so each is distinct.
    static func itemId(for event: EKEvent) -> String {
        guard isRecurring(event), let occurrence = event.occurrenceDate else { return event.calendarItemIdentifier }
        return "\(event.calendarItemIdentifier)#\(DeviceTime.wallClock(occurrence).iso)"
    }

    private func writableEvent(for id: String) throws -> EKEvent {
        let base = id.split(separator: "#", maxSplits: 1).first.map(String.init) ?? id
        guard let event = store.calendarItem(withIdentifier: base) as? EKEvent else {
            throw StoreError("the event no longer exists in Calendar")
        }
        guard !Self.isRecurring(event) else {
            throw StoreError("'\(event.title ?? "")' repeats; the Calendar copy is left unchanged")
        }
        return event
    }

    // MARK: - Mapping

    static func record(from event: EKEvent) -> Appointment {
        let start = DeviceTime.wallClock(event.startDate)
        let end = DeviceTime.wallClock(event.endDate)
        let span = event.isAllDay ? allDaySpan(start: start, end: end) : (start, max(end, start))
        return Appointment(summary: event.title ?? "", start: span.0, end: span.1, allDay: event.isAllDay,
                           location: event.location ?? "", notes: event.notes ?? "",
                           busyStatus: busyStatus(of: event.availability),
                           reminderMinutes: reminderMinutes(of: event.alarms), recurring: isRecurring(event))
    }

    /// EventKit ends an all-day event at 23:59:59 of its last day (some sources use
    /// the next midnight); neutral records end at the midnight after the last day.
    static func allDaySpan(start: NaiveDateTime, end: NaiveDateTime) -> (NaiveDateTime, NaiveDateTime) {
        let first = start.midnight
        let last = end.isMidnight && end > first ? end : end.midnight.adding(days: 1)
        return (first, max(last, first.adding(days: 1)))
    }

    static func busyStatus(of availability: EKEventAvailability) -> String {
        switch availability {
        case .free: return "free"
        case .tentative: return "tentative"
        case .unavailable: return "out_of_office"
        default: return "busy"
        }
    }

    static func availability(for busyStatus: String) -> (EKEventAvailability, EKCalendarEventAvailabilityMask) {
        switch busyStatus {
        case "free": return (.free, .free)
        case "tentative": return (.tentative, .tentative)
        case "out_of_office": return (.unavailable, .unavailable)
        default: return (.busy, .busy)
        }
    }

    /// Minutes before the start of the first relative alarm; absolute alarms are ignored.
    static func reminderMinutes(of alarms: [EKAlarm]?) -> Int? {
        alarms?.first { $0.absoluteDate == nil }.map { max(Int((-$0.relativeOffset / 60).rounded()), 0) }
    }

    static func apply(_ appointment: Appointment, to event: EKEvent) {
        let record = appointment.normalized()
        event.title = record.summary.isEmpty ? "(no subject)" : record.summary
        event.location = record.location.isEmpty ? nil : record.location
        event.notes = record.notes.isEmpty ? nil : record.notes
        event.isAllDay = record.allDay
        if record.allDay {
            let first = record.start.midnight
            let last = max(record.end.midnight, first.adding(days: 1))
            event.startDate = DeviceTime.date(first)
            event.endDate = DeviceTime.date(last).addingTimeInterval(-1)
        } else {
            event.startDate = DeviceTime.date(record.start)
            event.endDate = DeviceTime.date(max(record.end, record.start))
        }
        event.alarms = record.reminderMinutes.map { [EKAlarm(relativeOffset: -TimeInterval($0 * 60))] }
        let (availability, mask) = availability(for: record.busyStatus)
        if event.calendar?.supportedEventAvailabilities.contains(mask) == true {
            event.availability = availability
        }
    }
}
