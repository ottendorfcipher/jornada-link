import CryptoKit
import Foundation

/// Neutral PIM records shared by the device codecs and the Apple stores
/// (jornada/pim/models.py). Every record is an immutable value with a JSON
/// round-trip (`toDict` / `init(dict:)`), a content `fingerprint` for change
/// detection and a `matchKey` used to pair records that were never synced.
/// The fingerprint is the SHA-1 of the canonical JSON of the normalized record
/// with `uid` removed — byte-identical to Python's, so state files interchange.

public enum PimVocabulary {
    public static let busyStates = ["free", "tentative", "busy", "out_of_office"]
    public static let priorities = ["high", "normal", "low"]
    public static let phoneKinds = ["work", "work2", "home", "home2", "mobile", "work_fax", "home_fax",
                                    "pager", "car", "radio", "assistant"]
    public static let addressKinds = ["home", "work", "other"]
}

public protocol PimRecord: SyncRecord, Hashable {
    func normalized() -> Self
    func toDict() -> [String: Any]
    init?(dict: [String: Any])
}

extension PimRecord {
    /// `json.dumps(normalized().to_dict() - uid, sort_keys=True, ensure_ascii=False)`.
    public func canonicalJSON() -> String {
        var content = normalized().toDict()
        content.removeValue(forKey: "uid")
        return (try? PythonJSON.dumps(content, sortKeys: true, ensureASCII: false)) ?? ""
    }

    public var fingerprint: String {
        Insecure.SHA1.hash(data: Data(canonicalJSON().utf8)).map { String(format: "%02x", $0) }.joined()
    }
}

/// Text helpers with CPython semantics (`str.strip`, `str.casefold`, slicing).
public enum PimText {
    /// Characters `str.strip()` removes: Unicode whitespace plus the ASCII separators 0x1C–0x1F.
    static let whitespace = CharacterSet.whitespacesAndNewlines.union(CharacterSet(charactersIn: "\u{1C}\u{1D}\u{1E}\u{1F}"))

    public static func clean(_ text: String?) -> String {
        (text ?? "").trimmingCharacters(in: whitespace)
    }

    /// Stripped, empties dropped, duplicates removed (first occurrence wins).
    public static func cleanList(_ values: [String]) -> [String] {
        values.map(clean).reduce(into: [String]()) { seen, value in
            if !value.isEmpty, !seen.contains(value) { seen.append(value) }
        }
    }

    public static func casefold(_ text: String) -> String {
        text.folding(options: .caseInsensitive, locale: nil)
    }

    /// CRLF → LF then stripped, as every record normalizes its notes.
    public static func normalizedNotes(_ notes: String) -> String {
        clean(notes.replacingOccurrences(of: "\r\n", with: "\n"))
    }

    /// The first `count` code points (Python slicing).
    public static func prefix(_ text: String, _ count: Int) -> String {
        String(String.UnicodeScalarView(text.unicodeScalars.prefix(count)))
    }

    /// `_label` of jornada/sync/engine.py for records with a summary.
    static func label(summary: String) -> String {
        let cleaned = clean(summary)
        return cleaned.isEmpty ? "(record)" : prefix(cleaned, 60)
    }
}

/// Typed reads from a JSON dictionary (JSONSerialization or `toDict` output).
enum DictField {
    static func string(_ dict: [String: Any], _ key: String) -> String {
        dict[key] as? String ?? ""
    }

    static func bool(_ dict: [String: Any], _ key: String, default fallback: Bool = false) -> Bool {
        guard let number = dict[key] as? NSNumber else { return fallback }
        return number.boolValue
    }

    static func int(_ dict: [String: Any], _ key: String) -> Int? {
        guard let number = dict[key] as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID() else { return nil }
        return number.intValue
    }

    static func strings(_ dict: [String: Any], _ key: String) -> [String] {
        (dict[key] as? [Any])?.compactMap { $0 as? String } ?? []
    }

    /// An ISO datetime, or a date (midnight); nil when absent or unparsable.
    static func dateTime(_ dict: [String: Any], _ key: String) -> NaiveDateTime? {
        (dict[key] as? String).flatMap { NaiveDateTime(iso: $0) }
    }

    /// An ISO date, or the date part of an ISO datetime.
    static func date(_ dict: [String: Any], _ key: String) -> NaiveDate? {
        (dict[key] as? String).flatMap { NaiveDateTime(iso: $0)?.date }
    }

    static func optional(_ value: Any?) -> Any { value ?? NSNull() }
}

// MARK: - Appointment

public struct Appointment: PimRecord {
    public let summary: String
    public let start: NaiveDateTime
    public let end: NaiveDateTime
    public let allDay: Bool
    public let location: String
    public let notes: String
    public let categories: [String]
    public let busyStatus: String
    public let isPrivate: Bool
    public let reminderMinutes: Int?
    public let recurring: Bool
    public let uid: String

    public init(summary: String, start: NaiveDateTime, end: NaiveDateTime, allDay: Bool = false,
                location: String = "", notes: String = "", categories: [String] = [], busyStatus: String = "busy",
                isPrivate: Bool = false, reminderMinutes: Int? = nil, recurring: Bool = false, uid: String = "") {
        self.summary = summary
        self.start = start
        self.end = end
        self.allDay = allDay
        self.location = location
        self.notes = notes
        self.categories = categories
        self.busyStatus = busyStatus
        self.isPrivate = isPrivate
        self.reminderMinutes = reminderMinutes
        self.recurring = recurring
        self.uid = uid
    }

    public init?(dict: [String: Any]) {
        guard dict["summary"] is String, let start = DictField.dateTime(dict, "start"),
              let end = DictField.dateTime(dict, "end") else { return nil }
        self.init(summary: DictField.string(dict, "summary"), start: start, end: end,
                  allDay: DictField.bool(dict, "all_day"), location: DictField.string(dict, "location"),
                  notes: DictField.string(dict, "notes"), categories: DictField.strings(dict, "categories"),
                  busyStatus: dict["busy_status"] as? String ?? "busy", isPrivate: DictField.bool(dict, "private"),
                  reminderMinutes: DictField.int(dict, "reminder_minutes"), recurring: DictField.bool(dict, "recurring"),
                  uid: DictField.string(dict, "uid"))
    }

    public func toDict() -> [String: Any] {
        ["summary": summary, "start": start.iso, "end": end.iso, "all_day": allDay, "location": location,
         "notes": notes, "categories": categories, "busy_status": busyStatus, "private": isPrivate,
         "reminder_minutes": DictField.optional(reminderMinutes), "recurring": recurring, "uid": uid]
    }

    public func normalized() -> Appointment {
        Appointment(summary: PimText.clean(summary), start: start, end: end, allDay: allDay,
                    location: PimText.clean(location), notes: PimText.normalizedNotes(notes),
                    categories: PimText.cleanList(categories),
                    busyStatus: PimVocabulary.busyStates.contains(busyStatus) ? busyStatus : "busy",
                    isPrivate: isPrivate, reminderMinutes: reminderMinutes, recurring: recurring, uid: uid)
    }

    public var matchKey: String {
        let me = normalized()
        return "appt|\(PimText.casefold(me.summary))|\(me.start.isoMinutes)"
    }

    public var label: String { PimText.label(summary: summary) }
}

// MARK: - Task (PimTask: the plain name would shadow Swift concurrency's Task)

public struct PimTask: PimRecord {
    public let summary: String
    public let due: NaiveDate?
    public let start: NaiveDate?
    public let completed: NaiveDate?
    public let priority: String
    public let notes: String
    public let categories: [String]
    public let isPrivate: Bool
    public let uid: String

    public init(summary: String, due: NaiveDate? = nil, start: NaiveDate? = nil, completed: NaiveDate? = nil,
                priority: String = "normal", notes: String = "", categories: [String] = [], isPrivate: Bool = false,
                uid: String = "") {
        self.summary = summary
        self.due = due
        self.start = start
        self.completed = completed
        self.priority = priority
        self.notes = notes
        self.categories = categories
        self.isPrivate = isPrivate
        self.uid = uid
    }

    public init?(dict: [String: Any]) {
        guard dict["summary"] is String else { return nil }
        self.init(summary: DictField.string(dict, "summary"), due: DictField.date(dict, "due"),
                  start: DictField.date(dict, "start"), completed: DictField.date(dict, "completed"),
                  priority: dict["priority"] as? String ?? "normal", notes: DictField.string(dict, "notes"),
                  categories: DictField.strings(dict, "categories"), isPrivate: DictField.bool(dict, "private"),
                  uid: DictField.string(dict, "uid"))
    }

    public var isCompleted: Bool { completed != nil }

    public func toDict() -> [String: Any] {
        ["summary": summary, "due": DictField.optional(due?.iso), "start": DictField.optional(start?.iso),
         "completed": DictField.optional(completed?.iso), "priority": priority, "notes": notes,
         "categories": categories, "private": isPrivate, "uid": uid]
    }

    public func normalized() -> PimTask {
        PimTask(summary: PimText.clean(summary), due: due, start: start, completed: completed,
             priority: PimVocabulary.priorities.contains(priority) ? priority : "normal",
             notes: PimText.normalizedNotes(notes), categories: PimText.cleanList(categories),
             isPrivate: isPrivate, uid: uid)
    }

    public var matchKey: String {
        let me = normalized()
        return "task|\(PimText.casefold(me.summary))|\(me.due?.iso ?? "")"
    }

    public var label: String { PimText.label(summary: summary) }
}
