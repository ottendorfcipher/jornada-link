import Foundation

/// Time conventions between the device, the neutral records and the Mac
/// (jornada/pim/timeconv.py). A FILETIME on the device is a **naive
/// wall-clock**: its calendar fields are what the device displays, because the
/// Jornada's clock is set to the Mac's local wall-clock. The Mac side converts
/// EventKit / Contacts `Date`s to the local calendar's wall-clock fields and back.
public enum DeviceTime {
    static let ticksPerSecond: UInt64 = 10_000_000
    /// 1601-01-01 as days since the Unix epoch (negative).
    static let filetimeEpochDays = NaiveDate(1601, 1, 1).daysSinceUnixEpoch

    // MARK: - FILETIME ⇄ naive wall-clock

    /// FILETIME ticks → wall-clock fields (nil for 0), truncated to whole seconds.
    public static func naive(fromFiletime ticks: UInt64) -> NaiveDateTime? {
        guard ticks > 0 else { return nil }
        let seconds = ticks / ticksPerSecond
        let days = Int(seconds / 86_400) + filetimeEpochDays
        let ofDay = Int(seconds % 86_400)
        return NaiveDateTime(date: NaiveDate(daysSinceUnixEpoch: days), hour: ofDay / 3600,
                             minute: ofDay % 3600 / 60, second: ofDay % 60)
    }

    public static func date(fromFiletime ticks: UInt64) -> NaiveDate? {
        naive(fromFiletime: ticks)?.date
    }

    /// Wall-clock → FILETIME ticks; moments before 1601 clamp to 0 (Python raises).
    public static func filetime(from moment: NaiveDateTime) -> UInt64 {
        let days = moment.date.daysSinceUnixEpoch - filetimeEpochDays
        let seconds = days * 86_400 + moment.hour * 3600 + moment.minute * 60 + moment.second
        guard seconds > 0 else { return 0 }
        return UInt64(seconds) * ticksPerSecond
    }

    public static func filetime(from day: NaiveDate) -> UInt64 {
        filetime(from: NaiveDateTime(date: day))
    }

    // MARK: - Mac side: local wall-clock ⇄ Date

    /// The Gregorian calendar in `zone` (default: the Mac's current zone).
    public static func calendar(in zone: TimeZone = .current) -> Calendar {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = zone
        return calendar
    }

    /// The wall-clock fields `date` shows in `zone` (Python `to_wall_clock`).
    public static func wallClock(_ date: Date, zone: TimeZone = .current) -> NaiveDateTime {
        let parts = calendar(in: zone).dateComponents([.year, .month, .day, .hour, .minute, .second], from: date)
        return NaiveDateTime(parts.year ?? 1970, parts.month ?? 1, parts.day ?? 1,
                             parts.hour ?? 0, parts.minute ?? 0, parts.second ?? 0)
    }

    public static func localDate(_ date: Date, zone: TimeZone = .current) -> NaiveDate {
        wallClock(date, zone: zone).date
    }

    /// The instant at which `zone` shows `moment` (Python `to_aware`); a wall-clock
    /// that does not exist (a DST gap) resolves to the next valid instant.
    public static func date(_ moment: NaiveDateTime, zone: TimeZone = .current) -> Date {
        let components = DateComponents(year: moment.year, month: moment.month, day: moment.day,
                                        hour: moment.hour, minute: moment.minute, second: moment.second)
        return calendar(in: zone).date(from: components)
            ?? Date(timeIntervalSince1970: TimeInterval(moment.secondsSinceUnixEpoch - zone.secondsFromGMT()))
    }

    /// Local midnight of `day`.
    public static func date(_ day: NaiveDate, zone: TimeZone = .current) -> Date {
        date(NaiveDateTime(date: day), zone: zone)
    }

    public static func today(zone: TimeZone = .current) -> NaiveDate {
        localDate(Date(), zone: zone)
    }

    /// Year/month/day components → NaiveDate when all three are present.
    public static func date(from components: DateComponents?) -> NaiveDate? {
        guard let components, let year = components.year, let month = components.month,
              let day = components.day else { return nil }
        return NaiveDate(year, month, day)
    }
}
