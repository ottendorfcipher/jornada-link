import Foundation

/// Tasks Database ⇄ `PimTask` (jornada/pim/tasks.py).
public enum TaskCodec {
    /// Devices may flag completion with an i2 instead of a date; this stands in for the date.
    public static let unknownCompletionDate = NaiveDate(1970, 1, 1)
    static let priorityNames: [Int: String] = [PimIds.importanceHigh: "high", PimIds.importanceNormal: "normal",
                                               PimIds.importanceLow: "low"]
    static let priorityCodes: [String: Int] = Dictionary(uniqueKeysWithValues: priorityNames.map { ($1, $0) })

    static func completed(of record: Record) -> NaiveDate? {
        switch record.value(PimIds.taskCompleted) {
        case nil, .missing?, .int(0)?, .uint(0)?, .bool(false)?, .filetime(0)?:
            return nil
        case .filetime(let ticks)?:
            return DeviceTime.date(fromFiletime: ticks) ?? unknownCompletionDate
        default:
            return unknownCompletionDate
        }
    }

    public static func decode(_ record: Record) -> PimTask {
        PimTask(
            summary: PimCodecSupport.string(record, PimIds.subject),
            due: PimCodecSupport.date(record, PimIds.taskDue),
            start: PimCodecSupport.date(record, PimIds.taskStart),
            completed: completed(of: record),
            priority: priorityNames[PimCodecSupport.int(record, PimIds.importance, default: PimIds.importanceNormal)] ?? "normal",
            notes: PimCodecSupport.notes(record),
            categories: PimCodecSupport.splitCategories(record.value(PimIds.categories)),
            isPrivate: PimCodecSupport.int(record, PimIds.sensitivity) == PimIds.sensitivityPrivate)
    }

    /// Properties for CeWriteRecordProps; `existing` lets cleared fields be deleted.
    public static func encode(_ task: PimTask, existing: Record? = nil, today: NaiveDate = DeviceTime.today()) -> [PropVal] {
        let item = task.normalized()
        let completed = item.completed.map { $0 == unknownCompletionDate ? today : $0 }
        let fixed: [PropVal] = [
            .string(PimIds.subject, item.summary.isEmpty ? "(no subject)" : item.summary),
            .i4(PimIds.importance, Int32(priorityCodes[item.priority] ?? PimIds.importanceNormal)),
            .i2(PimIds.sensitivity, Int16(item.isPrivate ? PimIds.sensitivityPrivate : PimIds.sensitivityPublic)),
        ]
        return fixed
            + PimCodecSupport.dateProps(PimIds.taskStart, item.start, existing: existing)
            + PimCodecSupport.dateProps(PimIds.taskDue, item.due, existing: existing)
            + PimCodecSupport.dateProps(PimIds.taskCompleted, completed, existing: existing)
            + PimCodecSupport.notesProps(PimIds.notes, item.notes, existing: existing)
            + PimCodecSupport.categoriesProps(item.categories, existing: existing)
    }

    public static func isReadOnly(_ task: PimTask) -> Bool { false }
}
