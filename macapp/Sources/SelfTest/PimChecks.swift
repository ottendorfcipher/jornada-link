import Foundation
import JornadaCore

// PIM self-test (case `pim <port>`): the codecs and DeviceStore against the
// records tests/serve_fake.py seeds, plus offline checks that mirror
// tests/test_pim_codecs.py. Fingerprints are the values CPython produces for
// the same records (computed with `python3 -c` and hard-coded here).

/// Expected values from the Python implementation; the expressions name the CPython code that produced them.
enum PythonParity {
    // Appointment("Dentist", datetime(2026,9,7,9,30), datetime(2026,9,7,10,0), location="Clinic").fingerprint()
    static let appointmentFingerprint = "ab5da8366c7304a439fdc87b0e8604247b3d90a5"
    static let appointmentCanonical = #"{"all_day": false, "busy_status": "busy", "categories": [], "end": "2026-09-07T10:00:00", "location": "Clinic", "notes": "", "private": false, "recurring": false, "reminder_minutes": null, "start": "2026-09-07T09:30:00", "summary": "Dentist"}"#
    // Task("Buy batteries", due=date(2026,9,8), priority="high").fingerprint()
    static let taskFingerprint = "ffded114e7c9b8422adbc745a77bf70b43fdc531"
    // Contact(first_name="Ada", last_name="Lovelace", emails=("ada@example.org",), phones=(("mobile", "+44 20 7946 0001"),),
    //         addresses=(Address("home", "1 St", "London", "", "N1", "UK"),), birthday=date(1815,12,10)).fingerprint()
    static let contactFingerprint = "0d6b5b781b21035a24aa948db7321e2535b09356"
    // Appointment(" Lunch ", datetime(2026,9,7,12,0,5), datetime(2026,9,7,13), busy_status="odd",
    //             categories=(" b","a","b"), notes="x\r\ny").fingerprint()  (tests/test_pim_codecs.py::test_models_normalize_and_match)
    static let normalizedFingerprint = "e1a4766447aac1934748fc483475839980853c16"
    // Contact(full_name="", first_name="Ada", last_name="Lovelace", emails=("A@x.org",)).fingerprint()
    static let adaFingerprint = "f0dd02020ef1f56e2e75caa01c5bcdca7c4a5418"
    // json.dumps({"a": [1, 2], "b": {}, "c": [], "d": None, "e": True}, indent=1)
    static let indentedSample = "{\n \"a\": [\n  1,\n  2\n ],\n \"b\": {},\n \"c\": [],\n \"d\": null,\n \"e\": true\n}"
    // json.dumps({"z": 1, "a": {"y": [1, {"k": "v"}]}}, indent=2, sort_keys=True)
    static let sortedSample = "{\n  \"a\": {\n    \"y\": [\n      1,\n      {\n        \"k\": \"v\"\n      }\n    ]\n  },\n  \"z\": 1\n}"
}

func pimChecks(_ client: RapiClient) throws {
    pimOfflineChecks()
    let snapshots = FileManager.default.temporaryDirectory
        .appendingPathComponent("jornada-pim-selftest-\(ProcessInfo.processInfo.processIdentifier)")
    defer { try? FileManager.default.removeItem(at: snapshots) }
    try seededAppointmentChecks(client, snapshots: snapshots)
    try appointmentStoreChecks(client, snapshots: snapshots)
    try taskStoreChecks(client, snapshots: snapshots)
    try contactStoreChecks(client, snapshots: snapshots)
}

// MARK: - Device store against the fake device

func seededAppointmentChecks(_ client: RapiClient, snapshots: URL) throws {
    let store = DeviceStore(client: client, codec: .appointments, snapshotDirectory: snapshots)
    let items = try store.list()
    let dentist = items.first { ($0.record as? Appointment)?.summary == "Dentist" }?.record as? Appointment
    check("seeded Dentist decodes: start 2023-09-06 18:33:18, 30 minutes, notes, busy",
          dentist?.start == NaiveDateTime(2023, 9, 6, 18, 33, 18) && dentist.map { $0.end.seconds(since: $0.start) } == 1800
          && dentist?.notes == "bring card\n" && dentist?.busyStatus == "busy" && dentist?.allDay == false
          && dentist?.recurring == false)
    let standup = items.first { ($0.record as? Appointment)?.summary == "Standup" }?.record as? Appointment
    check("seeded Standup decodes: location, start, 15 minutes",
          standup?.location == "Room 4" && standup?.start == NaiveDateTime(2023, 9, 6, 18, 36, 7)
          && standup.map { $0.end.seconds(since: $0.start) } == 900)
    check("device item ids are the decimal oids", items.allSatisfy { UInt32($0.id) != nil })
}

func appointmentStoreChecks(_ client: RapiClient, snapshots: URL) throws {
    let store = DeviceStore(client: client, codec: .appointments, snapshotDirectory: snapshots)
    let before = try store.list().count
    let timed = Appointment(summary: "Swift dentist", start: NaiveDateTime(2026, 9, 7, 9, 30), end: NaiveDateTime(2026, 9, 7, 10, 15),
                            location: "Clinic", notes: "bring\ncard", categories: ["Health", "Personal"],
                            busyStatus: "tentative", isPrivate: true, reminderMinutes: 15)
    let trip = Appointment(summary: "Trip", start: NaiveDateTime(2026, 9, 7, 8), end: NaiveDateTime(2026, 9, 10), allDay: true)
    let timedId = try store.create(timed)
    let snapshotFiles = try FileManager.default.contentsOfDirectory(atPath: snapshots.path)
    check("a snapshot is written before the first write", snapshotFiles.count == 1
          && snapshotFiles.first?.hasPrefix("Appointments_Database.") == true && snapshotFiles.first?.hasSuffix(".json") == true)
    let payload = try snapshotFiles.first.flatMap {
        try JSONSerialization.jsonObject(with: Data(contentsOf: snapshots.appendingPathComponent($0))) as? [String: Any]
    }
    check("snapshot JSON carries the database name and every record",
          payload?["database"] as? String == PimIds.appointmentsDatabase && (payload?["records"] as? [Any])?.count == before
          && ((payload?["records"] as? [[String: Any]])?.first?["props"] as? [[String: Any]])?.first?["kind"] != nil)
    if let keep = ProcessInfo.processInfo.environment["JORNADA_SELFTEST_SNAPSHOT_COPY"], let name = snapshotFiles.first {
        // Lets tests/check the layout with Python's restore parser (see the CI/verification notes).
        try? FileManager.default.removeItem(atPath: keep)
        try FileManager.default.copyItem(at: snapshots.appendingPathComponent(name), to: URL(fileURLWithPath: keep))
    }
    let tripId = try store.create(trip)
    check("one snapshot per session", try FileManager.default.contentsOfDirectory(atPath: snapshots.path).count == 1)
    let listed = try store.list()
    let timedBack = listed.first { $0.id == timedId }?.record as? Appointment
    let tripBack = listed.first { $0.id == tripId }?.record as? Appointment
    check("timed appointment round-trips through the device", timedBack == timed)
    check("all-day appointment round-trips as midnight + whole days",
          tripBack == Appointment(summary: "Trip", start: NaiveDateTime(2026, 9, 7), end: NaiveDateTime(2026, 9, 10), allDay: true))
    _ = try store.update(id: timedId, record: Appointment(summary: "Swift dentist", start: timed.start, end: timed.end))
    let cleared = try store.list().first { $0.id == timedId }?.record as? Appointment
    check("update clears location, notes, categories and the reminder",
          cleared?.location == "" && cleared?.notes == "" && cleared?.categories == [] && cleared?.reminderMinutes == nil
          && cleared?.isPrivate == false && cleared?.busyStatus == "busy")
    try recurringChecks(client, store: store)
    try store.delete(id: timedId)
    try store.delete(id: tripId)
    check("deleting leaves the seeded appointments", try store.list().count == before)
}

func recurringChecks(_ client: RapiClient, store: DeviceStore) throws {
    let (info, _) = try client.readAllRecords(named: PimIds.appointmentsDatabase)
    let handle = try client.openDatabase(oid: info.oid)
    let weeklyOid = try client.writeRecord(handle: handle, props: [.string(PimIds.subject, "Weekly"), .i2(PimIds.apptOccurrence, 1)])
    try client.closeHandle(handle)
    let weekly = try store.list().first { $0.id == String(weeklyOid) }?.record as? Appointment
    check("recurring appointment decodes as recurring with the fallback start",
          weekly?.recurring == true && weekly?.start == NaiveDateTime(1970, 1, 1))
    check("updating a recurring appointment is refused", fails {
        _ = try store.update(id: String(weeklyOid), record: Appointment(summary: "Weekly", start: NaiveDateTime(2026, 1, 1, 9),
                                                                         end: NaiveDateTime(2026, 1, 1, 10)))
    })
    check("deleting a recurring appointment is refused", fails { try store.delete(id: String(weeklyOid)) })
    let cleanup = try client.openDatabase(oid: info.oid)
    try client.deleteRecord(handle: cleanup, oid: weeklyOid)
    try client.closeHandle(cleanup)
}

func taskStoreChecks(_ client: RapiClient, snapshots: URL) throws {
    let store = DeviceStore(client: client, codec: .tasks, snapshotDirectory: snapshots)
    let seeded = try store.list().first?.record as? PimTask
    check("seeded task decodes: due date, no completion, normal priority",
          seeded?.summary == "Buy batteries" && seeded?.due == NaiveDate(2023, 9, 6) && seeded?.completed == nil
          && seeded?.priority == "normal")
    let task = PimTask(summary: "Swift task", due: NaiveDate(2026, 1, 2), start: NaiveDate(2026, 1, 1),
                       completed: NaiveDate(2026, 1, 3), priority: "low", notes: "n", categories: ["x"], isPrivate: true)
    let id = try store.create(task)
    let back = try store.list().first { $0.id == id }?.record as? PimTask
    check("task round-trips through the device", back == task)
    _ = try store.update(id: id, record: PimTask(summary: "Swift task"))
    let cleared = try store.list().first { $0.id == id }?.record as? PimTask
    check("task update clears the dates", cleared?.due == nil && cleared?.start == nil && cleared?.completed == nil)
    try store.delete(id: id)
    check("task store is back to the seeded record", try store.list().count == 1)
}

func contactStoreChecks(_ client: RapiClient, snapshots: URL) throws {
    let store = DeviceStore(client: client, codec: .contacts, snapshotDirectory: snapshots)
    let seeded = try store.list().first?.record as? Contact
    check("seeded contact decodes: names, email, mobile, categories",
          seeded?.fullName == "Ada Lovelace" && seeded?.emails == ["ada@example.org"]
          && seeded?.phones == [PhoneNumber("mobile", "+44 20 7946 0001")] && seeded?.categories == ["Friends"])
    let grace = Contact(firstName: "Grace", lastName: "Hopper", middleName: "Brewster", title: "Rear Admiral",
                        fullName: "Grace Hopper", company: "US Navy", jobTitle: "Computer scientist",
                        emails: ["grace@example.org", "b@x.org", "c@x.org", "d@x.org"],
                        phones: [PhoneNumber("work", "1"), PhoneNumber("work", "2"), PhoneNumber("work", "3"),
                                 PhoneNumber("home", "4"), PhoneNumber("mobile", "5"), PhoneNumber("weird", "6")],
                        addresses: [Address(kind: "home", street: "1 Rd", city: "Town", state: "ST", postalCode: "123", country: "UK"),
                                    Address(kind: "work", street: "HQ")],
                        birthday: NaiveDate(1906, 12, 9), anniversary: NaiveDate(1930, 6, 15), spouse: "Vincent",
                        webPage: "https://example.org", notes: "COBOL", categories: ["Navy"])
    let id = try store.create(grace)
    let back = try store.list().first { $0.id == id }?.record as? Contact
    check("contact emails keep three slots, phones spill into work2 and drop the rest",
          back?.emails == ["grace@example.org", "b@x.org", "c@x.org"]
          && back?.phones == [PhoneNumber("work", "1"), PhoneNumber("work2", "2"), PhoneNumber("home", "4"), PhoneNumber("mobile", "5")])
    let expected = Contact(firstName: grace.firstName, lastName: grace.lastName, middleName: grace.middleName, title: grace.title,
                           fullName: grace.fullName, company: grace.company, jobTitle: grace.jobTitle,
                           emails: Array(grace.emails.prefix(3)), phones: back?.phones ?? [], addresses: grace.addresses,
                           birthday: grace.birthday, anniversary: grace.anniversary, spouse: grace.spouse,
                           webPage: grace.webPage, notes: grace.notes, categories: grace.categories)
    check("contact round-trips through the device (normalized)", back?.normalized() == expected.normalized())
    _ = try store.update(id: id, record: Contact(firstName: "Grace"))
    let cleared = try store.list().first { $0.id == id }?.record as? Contact
    check("contact update deletes cleared fields", cleared?.lastName == "" && cleared?.birthday == nil && cleared?.notes == ""
          && cleared?.emails == [] && cleared?.fullName == "Grace")
    try store.delete(id: id)
    check("contact store is back to the seeded record", try store.list().count == 1)
}

// MARK: - Offline codec and model checks (tests/test_pim_codecs.py)

func pimOfflineChecks() {
    fingerprintParityChecks()
    notesAndTimeChecks()
    appointmentCodecChecks()
    taskAndContactCodecChecks()
    jsonAndStateChecks()
}

func fingerprintParityChecks() {
    let dentist = Appointment(summary: "Dentist", start: NaiveDateTime(2026, 9, 7, 9, 30), end: NaiveDateTime(2026, 9, 7, 10, 0), location: "Clinic")
    check("appointment canonical JSON matches CPython", dentist.canonicalJSON() == PythonParity.appointmentCanonical)
    check("appointment fingerprint matches CPython", dentist.fingerprint == PythonParity.appointmentFingerprint)
    check("appointment match key", dentist.matchKey == "appt|dentist|2026-09-07T09:30")
    let task = PimTask(summary: "Buy batteries", due: NaiveDate(2026, 9, 8), priority: "high")
    check("task fingerprint matches CPython", task.fingerprint == PythonParity.taskFingerprint && task.matchKey == "task|buy batteries|2026-09-08")
    let ada = Contact(firstName: "Ada", lastName: "Lovelace", emails: ["ada@example.org"], phones: [PhoneNumber("mobile", "+44 20 7946 0001")],
                      addresses: [Address(kind: "home", street: "1 St", city: "London", postalCode: "N1", country: "UK")],
                      birthday: NaiveDate(1815, 12, 10))
    check("contact fingerprint matches CPython", ada.fingerprint == PythonParity.contactFingerprint
          && ada.matchKey == "contact|ada lovelace|ada@example.org")
    let lunch = Appointment(summary: " Lunch ", start: NaiveDateTime(2026, 9, 7, 12, 0, 5), end: NaiveDateTime(2026, 9, 7, 13),
                            notes: "x\r\ny", categories: [" b", "a", "b"], busyStatus: "odd")
    let normalized = lunch.normalized()
    check("normalization trims, validates busy status, dedupes categories, fixes line ends",
          normalized.summary == "Lunch" && normalized.busyStatus == "busy" && normalized.categories == ["b", "a"] && normalized.notes == "x\ny")
    check("normalized fingerprint matches CPython", lunch.fingerprint == PythonParity.normalizedFingerprint
          && normalized.fingerprint == lunch.fingerprint && lunch.matchKey == "appt|lunch|2026-09-07T12:00")
    check("appointment dict round trip", Appointment(dict: lunch.toDict()) == lunch)
    let short = Contact(firstName: "Ada", lastName: "Lovelace", emails: ["A@x.org"])
    check("contact display name, match key and fingerprint", short.displayName() == "Ada Lovelace"
          && short.matchKey == "contact|ada lovelace|a@x.org" && short.fingerprint == PythonParity.adaFingerprint)
    check("display name falls back to company then email",
          Contact(company: "ACME").displayName() == "ACME" && Contact(emails: ["e@x"]).displayName() == "e@x")
    check("bogus priority normalizes", PimTask(summary: "x", priority: "bogus").normalized().priority == "normal")
    let revived = PimTask(dict: ["summary": "s", "due": "2026-01-01", "categories": ["a"], "bogus": 1])
    check("task from dict ignores unknown keys", revived == PimTask(summary: "s", due: NaiveDate(2026, 1, 1), categories: ["a"]))
    check("contact dict round trip", Contact(dict: ada.toDict()) == ada)
}

func notesAndTimeChecks() {
    check("notes encode adds CRLF and the odd-length pad", NotesBlob.encode("a\nbc") == Data("a\r\nbc\u{03}".utf8) && NotesBlob.encode("ab") == Data("ab".utf8))
    check("notes decode strips the pad and CRLF", NotesBlob.decode(Data("a\r\nbc\u{03}".utf8)) == "a\nbc"
          && NotesBlob.decode(Data()) == "" && NotesBlob.decode(nil) == "")
    check("notes cp1252 round trip", NotesBlob.decode(NotesBlob.encode("héllo €")) == "héllo €")
    let ink = Data("{\\pwi".utf8) + Data(count: 10)
    check("ink notes decode to empty", NotesBlob.isInk(ink) && NotesBlob.decode(ink) == "" && !NotesBlob.isInk(Data("x".utf8)))
    check("undefined cp1252 bytes decode to U+FFFD, unencodable characters to ?",
          CodePage1252.decode(Data([0x81])) == "\u{FFFD}" && CodePage1252.encode("ǝ") == Data("?".utf8))
    let moment = NaiveDateTime(2026, 9, 7, 12, 30, 15)
    check("filetime round trip", DeviceTime.naive(fromFiletime: DeviceTime.filetime(from: moment)) == moment
          && DeviceTime.naive(fromFiletime: 0) == nil)
    check("date to filetime is midnight", DeviceTime.naive(fromFiletime: DeviceTime.filetime(from: NaiveDate(2026, 9, 7))) == NaiveDateTime(2026, 9, 7))
    check("filetime ticks match CPython", DeviceTime.filetime(from: NaiveDateTime(2026, 9, 7, 9, 30)) == 134_332_470_000_000_000
          && DeviceTime.filetime(from: NaiveDate(1815, 12, 10)) == 67_827_456_000_000_000
          && DeviceTime.filetime(from: NaiveDateTime(1970, 1, 1)) == 116_444_736_000_000_000)
    check("seeded FILETIME decodes as CPython does", DeviceTime.naive(fromFiletime: 0x01D9_E0F0_9B2C_3D4E) == NaiveDateTime(2023, 9, 6, 18, 33, 18))
    check("ISO parsing", NaiveDateTime(iso: "2026-09-07") == NaiveDateTime(2026, 9, 7) && NaiveDateTime(iso: "2026-09-07T10:00:00Z") == NaiveDateTime(2026, 9, 7, 10)
          && NaiveDate(iso: "2026-13-01") == nil && NaiveDateTime(iso: "2026-09-07T10:00:00.123456+02:00") == NaiveDateTime(2026, 9, 7, 10))
    check("naive calendar arithmetic", NaiveDate(2024, 2, 28).adding(days: 2) == NaiveDate(2024, 3, 1)
          && NaiveDateTime(2026, 1, 1).adding(minutes: -1) == NaiveDateTime(2025, 12, 31, 23, 59)
          && NaiveDate(2026, 9, 10).days(since: NaiveDate(2026, 9, 7)) == 3)
    let zone = TimeZone(identifier: "Europe/Berlin")!
    let instant = DeviceTime.date(NaiveDateTime(2026, 9, 7, 10), zone: zone)
    check("wall-clock conversion in an explicit zone", DeviceTime.wallClock(instant, zone: TimeZone(identifier: "UTC")!) == NaiveDateTime(2026, 9, 7, 8)
          && DeviceTime.wallClock(instant, zone: zone) == NaiveDateTime(2026, 9, 7, 10))
}

func appointmentCodecChecks() {
    let timed = Appointment(summary: "Dentist", start: NaiveDateTime(2026, 9, 7, 9, 30), end: NaiveDateTime(2026, 9, 7, 10, 15), location: "Clinic",
                            notes: "bring\ncard", categories: ["Health", "Personal"], busyStatus: "tentative", isPrivate: true, reminderMinutes: 15)
    let props = AppointmentCodec.encode(timed)
    let byId = Dictionary(props.map { ($0.propId, $0) }, uniquingKeysWith: { first, _ in first })
    check("timed encode: duration, type, reminder, unknown 0002, categories",
          byId[PimIds.apptDuration]?.value == .int(45) && byId[PimIds.apptType]?.value == .int(2)
          && byId[PimIds.reminderEnabled]?.value == .int(1) && byId[PimIds.reminderSound]?.value == .string("Alarm1.wav")
          && byId[PimIds.unknown0002]?.value == .int(0) && byId[PimIds.categories]?.value == .string("Health,Personal"))
    check("timed decode(encode) is identity", AppointmentCodec.decode(Record(oid: 1, props: props)) == timed)
    let trip = Appointment(summary: "Trip", start: NaiveDateTime(2026, 9, 7, 8), end: NaiveDateTime(2026, 9, 10), allDay: true)
    let tripProps = AppointmentCodec.encode(trip)
    let tripById = Dictionary(tripProps.map { ($0.propId, $0.value) }, uniquingKeysWith: { first, _ in first })
    check("all-day encode uses type 1 and (days-1)*1440+1", tripById[PimIds.apptType] == .int(1) && tripById[PimIds.apptDuration] == .int(2 * 1440 + 1))
    check("all-day decode", AppointmentCodec.decode(Record(oid: 2, props: tripProps))
          == Appointment(summary: "Trip", start: NaiveDateTime(2026, 9, 7), end: NaiveDateTime(2026, 9, 10), allDay: true))
    let table = [(0, 1), (1, 1), (1440, 1), (1441, 2), (2880, 2), (2881, 3), (100, 1), (1500, 2)]
    check("days_from_duration tolerates both conventions", table.allSatisfy { AppointmentCodec.daysFromDuration($0.0) == $0.1 })
    let existing = Record(oid: 1, props: AppointmentCodec.encode(Appointment(summary: "A", start: NaiveDateTime(2026, 1, 1, 9), end: NaiveDateTime(2026, 1, 1, 10),
                                                                              location: "Room", notes: "n", categories: ["c"], reminderMinutes: 5)))
    let cleared = AppointmentCodec.encode(Appointment(summary: "A", start: NaiveDateTime(2026, 1, 1, 9), end: NaiveDateTime(2026, 1, 1, 10)), existing: existing)
    let deleted = Set(cleared.filter(\.isDeleted).map(\.propId))
    check("update deletes cleared fields", deleted == [PimIds.apptLocation, PimIds.notes, PimIds.categories, PimIds.reminderMinutes])
    let recurring = AppointmentCodec.decode(Record(oid: 2, props: [.string(PimIds.subject, "Weekly"), .i2(PimIds.apptOccurrence, 1)]))
    check("recurring flag and fallback start", recurring.recurring && AppointmentCodec.isReadOnly(recurring) && recurring.start == NaiveDateTime(1970, 1, 1))
    let missing = Record(oid: 3, props: [PropVal(propId: PimIds.subject, kind: .string, value: .missing, flags: Cedb.propNotFound)])
    check("missing subject decodes as empty", AppointmentCodec.decode(missing).summary == "")
}

func taskAndContactCodecChecks() {
    let task = PimTask(summary: "Buy", due: NaiveDate(2026, 1, 2), start: NaiveDate(2026, 1, 1), completed: NaiveDate(2026, 1, 3), priority: "low",
                       notes: "n", categories: ["x"], isPrivate: true)
    let props = TaskCodec.encode(task)
    check("task decode(encode) is identity", TaskCodec.decode(Record(oid: 1, props: props)) == task)
    let flag = TaskCodec.decode(Record(oid: 2, props: [.string(PimIds.subject, "Done"), .i2(PimIds.taskCompleted, 1)]))
    check("i2 completion flag decodes as the unknown completion date", flag.completed == TaskCodec.unknownCompletionDate && flag.isCompleted)
    let open = TaskCodec.decode(Record(oid: 3, props: [.string(PimIds.subject, "Open"), .i2(PimIds.taskCompleted, 0), .i4(PimIds.importance, 1)]))
    check("zero completion flag and high importance", open.completed == nil && open.priority == "high")
    let unknown = TaskCodec.encode(PimTask(summary: "X", completed: TaskCodec.unknownCompletionDate)).first { $0.propId == PimIds.taskCompleted }
    check("unknown completion date encodes as today", { if case .filetime(let ticks)? = unknown?.value { return ticks > 0 }; return false }())
    let cleared = Set(TaskCodec.encode(PimTask(summary: "Buy"), existing: Record(oid: 1, props: props)).filter(\.isDeleted).map(\.propId))
    check("task update deletes cleared dates", cleared.isSuperset(of: [PimIds.taskDue, PimIds.taskStart, PimIds.taskCompleted]))

    let contact = Contact(firstName: "Ada", lastName: "Lovelace", middleName: "King", title: "Countess", company: "Analytical", jobTitle: "Mathematician",
                          emails: ["a@x.org", "b@x.org", "c@x.org", "d@x.org"],
                          phones: [PhoneNumber("work", "1"), PhoneNumber("work", "2"), PhoneNumber("work", "3"), PhoneNumber("home", "4"),
                                   PhoneNumber("mobile", "5"), PhoneNumber("weird", "6")],
                          addresses: [Address(kind: "home", street: "1 Rd", city: "Town", state: "ST", postalCode: "123", country: "UK"),
                                      Address(kind: "work", street: "HQ")],
                          birthday: NaiveDate(1815, 12, 10), anniversary: NaiveDate(1835, 7, 8), spouse: "William", webPage: "https://x.org",
                          notes: "note", categories: ["Friends"])
    let contactProps = ContactCodec.encode(contact)
    let decoded = ContactCodec.decode(Record(oid: 1, props: contactProps))
    check("contact slots: three emails, phone overflow, full name, addresses, dates",
          decoded.emails == ["a@x.org", "b@x.org", "c@x.org"]
          && decoded.phones == [PhoneNumber("work", "1"), PhoneNumber("work2", "2"), PhoneNumber("home", "4"), PhoneNumber("mobile", "5")]
          && decoded.fullName == "Ada King Lovelace" && decoded.addresses == contact.addresses
          && decoded.birthday == contact.birthday && decoded.anniversary == contact.anniversary)
    let clearedContact = Set(ContactCodec.encode(Contact(firstName: "Ada"), existing: Record(oid: 1, props: contactProps)).filter(\.isDeleted).map(\.propId))
    check("contact update deletes cleared fields", clearedContact.isSuperset(of: [PimIds.contactLastName, PimIds.contactBirthday, PimIds.contactNote]))
    check("empty contact encodes as (unnamed)", ContactCodec.encode(Contact()) == [.string(PimIds.contactFullName, "(unnamed)")])
    check("codec lookup is case-insensitive", PimCodec.codec(for: "contacts database")?.database == PimIds.contactsDatabase && PimCodec.codec(for: "Nope") == nil)
}

func jsonAndStateChecks() {
    let escaped = try? PythonJSON.dumps(["s": "a\"b\\c\n\t\u{01}\u{7F}/é€"], ensureASCII: false)
    check("JSON escapes like CPython (ensure_ascii=False)", escaped == "{\"s\": \"a\\\"b\\\\c\\n\\t\\u0001\u{7F}/é€\"}")
    check("JSON ensure_ascii escapes non-ASCII", (try? PythonJSON.dumps(["s": "é😀"])) == "{\"s\": \"\\u00e9\\ud83d\\ude00\"}")
    let indented: KeyValuePairs<String, Any> = ["a": [1, 2], "b": [String: Any](), "c": [Any](), "d": NSNull(), "e": true]
    check("JSON indent=1 formatting matches CPython", (try? PythonJSON.dumps(indented, indent: 1)) == PythonParity.indentedSample)
    check("JSON indent=2 sorted formatting matches CPython",
          (try? PythonJSON.dumps(["z": 1, "a": ["y": [1, ["k": "v"]]]], sortKeys: true, indent: 2)) == PythonParity.sortedSample)
    let state = SyncState(links: [SyncLink(localId: "1", remoteId: "r1", localHash: "a", remoteHash: "b")], lastSync: "2026-09-07 10:00:00")
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent("jornada-state-selftest-\(ProcessInfo.processInfo.processIdentifier)")
    defer { try? FileManager.default.removeItem(at: directory) }
    let path = try? SyncPaths.statePath(module: "calendar", account: "app", directory: directory)
    check("state path is state/<module>-<account>.json", path?.path.hasSuffix("/state/calendar-app.json") == true)
    check("bad account names are refused", fails { _ = try SyncPaths.statePath(module: "calendar", account: "../x") })
    if let path {
        do {
            try SyncStateFile.save(state, to: path)
            check("state file round trip", SyncStateFile.load(path) == state)
            let text = try String(contentsOf: path, encoding: .utf8)
            check("state file has Python's layout", text.hasPrefix("{\n  \"last_sync\": \"2026-09-07 10:00:00\",\n  \"links\": [\n    {\n      \"local\": \"1\","))
        } catch {
            check("state file write failed: \(error)", false)
        }
    }
    check("missing state file loads empty", SyncStateFile.load(directory.appendingPathComponent("nope.json")) == SyncState())
}
