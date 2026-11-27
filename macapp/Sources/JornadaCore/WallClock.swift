import Foundation

/// Naive calendar values — the Swift twins of Python's `date` and `datetime`
/// without a time zone (jornada/pim/timeconv.py). The device keeps one clock
/// that reads the Mac's local wall-clock, so every PIM time is carried as the
/// calendar fields the device displays; the modern side converts at its edge
/// (see `DeviceTime`). Pure integer arithmetic on the proleptic Gregorian
/// calendar, so no `Calendar` or `TimeZone` can leak into a fingerprint.

/// A calendar day (Python `date`).
public struct NaiveDate: Hashable, Sendable, Comparable, CustomStringConvertible {
    public let year: Int
    public let month: Int
    public let day: Int

    public init(_ year: Int, _ month: Int, _ day: Int) {
        self.year = year
        self.month = month
        self.day = day
    }

    /// Days since 1970-01-01 (negative before), Howard Hinnant's civil-from-days.
    public init(daysSinceUnixEpoch days: Int) {
        let shifted = days + 719_468
        let era = (shifted >= 0 ? shifted : shifted - 146_096) / 146_097
        let dayOfEra = shifted - era * 146_097
        let yearOfEra = (dayOfEra - dayOfEra / 1460 + dayOfEra / 36524 - dayOfEra / 146_096) / 365
        let dayOfYear = dayOfEra - (365 * yearOfEra + yearOfEra / 4 - yearOfEra / 100)
        let shiftedMonth = (5 * dayOfYear + 2) / 153
        let day = dayOfYear - (153 * shiftedMonth + 2) / 5 + 1
        let month = shiftedMonth < 10 ? shiftedMonth + 3 : shiftedMonth - 9
        let year = yearOfEra + era * 400 + (month <= 2 ? 1 : 0)
        self.init(year, month, day)
    }

    /// "YYYY-MM-DD" (Python `date.isoformat`).
    public init?(iso text: String) {
        let parts = text.split(separator: "-", omittingEmptySubsequences: false)
        guard parts.count == 3, let year = Int(parts[0]), let month = Int(parts[1]), let day = Int(parts[2]),
              (1...12).contains(month), (1...31).contains(day) else { return nil }
        self.init(year, month, day)
    }

    /// Days since 1970-01-01 (Howard Hinnant's days-from-civil).
    public var daysSinceUnixEpoch: Int {
        let year = month <= 2 ? self.year - 1 : self.year
        let era = (year >= 0 ? year : year - 399) / 400
        let yearOfEra = year - era * 400
        let dayOfYear = (153 * (month + (month > 2 ? -3 : 9)) + 2) / 5 + day - 1
        let dayOfEra = yearOfEra * 365 + yearOfEra / 4 - yearOfEra / 100 + dayOfYear
        return era * 146_097 + dayOfEra - 719_468
    }

    public var iso: String { String(format: "%04d-%02d-%02d", year, month, day) }
    public var description: String { iso }

    public func adding(days: Int) -> NaiveDate { NaiveDate(daysSinceUnixEpoch: daysSinceUnixEpoch + days) }

    /// Whole days from `other` to `self`.
    public func days(since other: NaiveDate) -> Int { daysSinceUnixEpoch - other.daysSinceUnixEpoch }

    public static func < (lhs: NaiveDate, rhs: NaiveDate) -> Bool {
        lhs.daysSinceUnixEpoch < rhs.daysSinceUnixEpoch
    }
}

/// A wall-clock moment at second precision (Python naive `datetime`).
public struct NaiveDateTime: Hashable, Sendable, Comparable, CustomStringConvertible {
    public let date: NaiveDate
    public let hour: Int
    public let minute: Int
    public let second: Int

    public init(date: NaiveDate, hour: Int = 0, minute: Int = 0, second: Int = 0) {
        self.date = date
        self.hour = hour
        self.minute = minute
        self.second = second
    }

    public init(_ year: Int, _ month: Int, _ day: Int, _ hour: Int = 0, _ minute: Int = 0, _ second: Int = 0) {
        self.init(date: NaiveDate(year, month, day), hour: hour, minute: minute, second: second)
    }

    /// Seconds since 1970-01-01T00:00:00 on the naive calendar.
    public init(secondsSinceUnixEpoch total: Int) {
        let days = total >= 0 ? total / 86_400 : -((-total + 86_399) / 86_400)
        let ofDay = total - days * 86_400
        self.init(date: NaiveDate(daysSinceUnixEpoch: days), hour: ofDay / 3600, minute: ofDay % 3600 / 60,
                  second: ofDay % 60)
    }

    /// ISO 8601 date or datetime; a date-only text is midnight, a fraction or a
    /// zone suffix ("Z", "+02:00") is accepted and dropped (the value stays naive).
    public init?(iso text: String) {
        let cleaned = text.trimmingCharacters(in: .whitespaces)
        guard cleaned.count >= 10 else { return nil }
        let datePart = String(cleaned.prefix(10))
        guard let date = NaiveDate(iso: datePart) else { return nil }
        let rest = cleaned.dropFirst(10)
        guard let separator = rest.first else {
            self.init(date: date)
            return
        }
        guard separator == "T" || separator == " " else { return nil }
        let timeText = rest.dropFirst().prefix { $0 != "Z" && $0 != "+" && $0 != "-" }
        let clock = timeText.split(separator: ".").first.map(String.init) ?? ""
        let fields = clock.split(separator: ":", omittingEmptySubsequences: false).map { Int($0) }
        guard fields.count >= 2, let hour = fields[0], let minute = fields[1],
              (0...23).contains(hour), (0...59).contains(minute) else { return nil }
        let second = fields.count > 2 ? fields[2] : 0
        guard let second, (0...59).contains(second) else { return nil }
        self.init(date: date, hour: hour, minute: minute, second: second)
    }

    public var year: Int { date.year }
    public var month: Int { date.month }
    public var day: Int { date.day }

    public var secondsSinceUnixEpoch: Int { date.daysSinceUnixEpoch * 86_400 + hour * 3600 + minute * 60 + second }

    /// "YYYY-MM-DDTHH:MM:SS" (Python `isoformat(timespec="seconds")`).
    public var iso: String { String(format: "%@T%02d:%02d:%02d", date.iso, hour, minute, second) }
    /// "YYYY-MM-DDTHH:MM" (Python `isoformat(timespec="minutes")`).
    public var isoMinutes: String { String(format: "%@T%02d:%02d", date.iso, hour, minute) }
    public var description: String { iso }

    public var midnight: NaiveDateTime { NaiveDateTime(date: date) }
    public var isMidnight: Bool { hour == 0 && minute == 0 && second == 0 }

    public func adding(seconds: Int) -> NaiveDateTime { NaiveDateTime(secondsSinceUnixEpoch: secondsSinceUnixEpoch + seconds) }
    public func adding(minutes: Int) -> NaiveDateTime { adding(seconds: minutes * 60) }
    public func adding(days: Int) -> NaiveDateTime {
        NaiveDateTime(date: date.adding(days: days), hour: hour, minute: minute, second: second)
    }

    /// Whole seconds from `other` to `self`.
    public func seconds(since other: NaiveDateTime) -> Int { secondsSinceUnixEpoch - other.secondsSinceUnixEpoch }

    public static func < (lhs: NaiveDateTime, rhs: NaiveDateTime) -> Bool {
        lhs.secondsSinceUnixEpoch < rhs.secondsSinceUnixEpoch
    }
}
