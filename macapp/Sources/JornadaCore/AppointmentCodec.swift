import Foundation

/// Appointments Database ⇄ `Appointment` (jornada/pim/appointments.py).
/// Conventions follow SynCE's librra: an all-day event starts at midnight with
/// type 1 and a duration of `(days - 1) * 1440 + 1` minutes; timed events carry
/// their length in minutes. Recurring appointments decode (`recurring`) but are
/// never rewritten.
public enum AppointmentCodec {
    public static let minutesPerDay = 1440
    static let fallbackStart = NaiveDateTime(1970, 1, 1)
    static let busyNames: [Int: String] = [PimIds.busyFree: "free", PimIds.busyTentative: "tentative",
                                           PimIds.busyBusy: "busy", PimIds.busyOutOfOffice: "out_of_office"]
    static let busyCodes: [String: Int] = Dictionary(uniqueKeysWithValues: busyNames.map { ($1, $0) })

    /// Length in days of an all-day event from its stored duration (tolerant of both conventions).
    public static func daysFromDuration(_ minutes: Int) -> Int {
        if minutes <= 1 { return 1 }
        if minutes % minutesPerDay == 0 { return minutes / minutesPerDay }
        if (minutes - 1) % minutesPerDay == 0 { return (minutes - 1) / minutesPerDay + 1 }
        return (minutes + minutesPerDay - 1) / minutesPerDay
    }

    public static func durationForDays(_ days: Int) -> Int {
        (max(days, 1) - 1) * minutesPerDay + 1
    }

    public static func decode(_ record: Record) -> Appointment {
        let rawStart = PimCodecSupport.ticks(record, PimIds.apptStart).flatMap(DeviceTime.naive(fromFiletime:))
            ?? fallbackStart
        let duration = PimCodecSupport.int(record, PimIds.apptDuration)
        let allDay = PimCodecSupport.int(record, PimIds.apptType, default: PimIds.apptTypeNormal) == PimIds.apptTypeAllDay
        let start = allDay ? rawStart.midnight : rawStart
        let end = allDay ? start.adding(days: daysFromDuration(duration)) : start.adding(minutes: max(duration, 0))
        let reminderEnabled = PimCodecSupport.int(record, PimIds.reminderEnabled) != 0
        let recurring = PimCodecSupport.int(record, PimIds.apptOccurrence) == PimIds.occurrenceRepeated
            || record.get(PimIds.apptRecurrencePattern) != nil
        return Appointment(
            summary: PimCodecSupport.string(record, PimIds.subject),
            start: start,
            end: end,
            allDay: allDay,
            location: PimCodecSupport.string(record, PimIds.apptLocation),
            notes: PimCodecSupport.notes(record),
            categories: PimCodecSupport.splitCategories(record.value(PimIds.categories)),
            busyStatus: busyNames[PimCodecSupport.int(record, PimIds.apptBusyStatus, default: PimIds.busyBusy)] ?? "busy",
            isPrivate: PimCodecSupport.int(record, PimIds.sensitivity) == PimIds.sensitivityPrivate,
            reminderMinutes: reminderEnabled ? PimCodecSupport.int(record, PimIds.reminderMinutes) : nil,
            recurring: recurring)
    }

    /// Properties for CeWriteRecordProps; `existing` lets cleared fields be deleted.
    public static func encode(_ appointment: Appointment, existing: Record? = nil) -> [PropVal] {
        let appt = appointment.normalized()
        let timing = timingProps(appt)
        let fixed: [PropVal] = [
            .string(PimIds.subject, appt.summary.isEmpty ? "(no subject)" : appt.summary),
            .filetime(PimIds.apptStart, DeviceTime.filetime(from: timing.start)),
            .i4(PimIds.apptDuration, Int32(clamping: timing.duration)),
            .i4(PimIds.apptType, Int32(timing.kind)),
            .i2(PimIds.apptOccurrence, Int16(PimIds.occurrenceOnce)),
            .i2(PimIds.apptBusyStatus, Int16(busyCodes[appt.busyStatus] ?? PimIds.busyBusy)),
            .i2(PimIds.sensitivity, Int16(appt.isPrivate ? PimIds.sensitivityPrivate : PimIds.sensitivityPublic)),
            .i4(PimIds.unknown0002, 0),
        ]
        return fixed
            + PimCodecSupport.stringProps(PimIds.apptLocation, appt.location, existing: existing)
            + PimCodecSupport.notesProps(PimIds.notes, appt.notes, existing: existing)
            + PimCodecSupport.categoriesProps(appt.categories, existing: existing)
            + reminderProps(appt.reminderMinutes, existing: existing)
    }

    /// Start, duration in minutes and APPT_TYPE for a normalized appointment.
    static func timingProps(_ appt: Appointment) -> (start: NaiveDateTime, duration: Int, kind: Int) {
        if appt.allDay {
            let start = appt.start.midnight
            let days = max(appt.end.midnight.date.days(since: start.date), 1)
            return (start, durationForDays(days), PimIds.apptTypeAllDay)
        }
        let minutes = (Double(appt.end.seconds(since: appt.start)) / 60).rounded(.toNearestOrEven)
        return (appt.start, max(Int(minutes), 0), PimIds.apptTypeNormal)
    }

    static func reminderProps(_ minutes: Int?, existing: Record?) -> [PropVal] {
        guard let minutes else {
            let disabled: [PropVal] = [.i2(PimIds.reminderEnabled, 0)]
            return PimCodecSupport.hasProp(existing, PimIds.reminderMinutes)
                ? disabled + [.deleted(propId: PimIds.reminderMinutes, kind: .i4)] : disabled
        }
        return [
            .i2(PimIds.reminderEnabled, 1),
            .i4(PimIds.reminderMinutes, Int32(clamping: max(minutes, 0))),
            .i4(PimIds.reminderOptions, Int32(PimIds.defaultReminderOptions)),
            .string(PimIds.reminderSound, PimIds.defaultReminderSound),
        ]
    }

    /// Recurring device appointments are left alone: the pattern blob is not modelled.
    public static func isReadOnly(_ appointment: Appointment) -> Bool { appointment.recurring }
}
