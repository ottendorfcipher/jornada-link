import Foundation
import JornadaCore

/// Protocol self-test: exercises the Swift RAPI client against the Python
/// fake device (tests/fake_device.py), and the Swift dccm listener against the
/// Python fake ActiveSync client. Usage:
///   SelfTest rapi <port>
///   SelfTest dccm <port>     (listens; exits 0 once a device handshakes + 2 pings)
///   SelfTest cedb x          (CEDB record codec against tests/test_cedb.py; no network)
let arguments = CommandLine.arguments
guard arguments.count >= 3 else {
    FileHandle.standardError.write(
        Data("usage: SelfTest rapi <port> | dccm <port> | gpib <port> | cedb x | mirror <dir> | runner <n> | usb-profiles <file> | usb <n>\n".utf8))
    exit(2)
}
// rapi/dccm need a TCP port; mirror/runner take a path or placeholder instead.
let port = UInt16(arguments[2]) ?? 0
if ["rapi", "dccm", "gpib"].contains(arguments[1]) && port == 0 {
    FileHandle.standardError.write(Data("usage: SelfTest rapi|dccm <port>\n".utf8))
    exit(2)
}

var failures = 0
func check(_ name: String, _ condition: Bool) {
    print("\(condition ? "PASS" : "FAIL")  \(name)")
    if !condition { failures += 1 }
}

/// True when `body` throws (for the negative-path checks).
func fails(_ body: () throws -> Void) -> Bool {
    do { try body() } catch { return true }
    return false
}

// MARK: - Object-store databases (records seeded by tests/serve_fake.py)

// Pocket Outlook property ids (jornada/pim/ids.py).
let subjectId: UInt16 = 0x0037, notesId: UInt16 = 0x0017, apptStartId: UInt16 = 0x420D
let apptDurationId: UInt16 = 0x4213, apptLocationId: UInt16 = 0x4208
let firstNameId: UInt16 = 0x3A06, lastNameId: UInt16 = 0x3A11, fullNameId: UInt16 = 0x4013
let sensitivityId: UInt16 = 0x0004, birthdayId: UInt16 = 0x4001

func databaseChecks(_ client: RapiClient) throws {
    let databases = try client.findAllDatabases()
    let counts = Dictionary(databases.map { ($0.name, $0.numRecords) }, uniquingKeysWith: { first, _ in first })
    check("findAllDatabases lists the three seeded databases",
          counts == ["Appointments Database": 2, "Contacts Database": 1, "Tasks Database": 1])
    check("listing carries oids and modification dates", databases.allSatisfy { $0.oid != 0 && $0.lastModified != nil })
    let appointments = databases.first { $0.name == "Appointments Database" }
    check("Appointments Database is listed", appointments != nil)
    guard let appointments else { return }

    let handle = try client.openDatabase(oid: appointments.oid)
    let dentist = try client.readRecord(handle: handle)
    let standup = try client.readRecord(handle: handle)
    check("Dentist: subject, start, duration, notes",
          dentist?.string(subjectId) == "Dentist" && dentist?.filetime(apptStartId) == 0x01D9_E0F0_9B2C_3D4E
          && dentist?.int(apptDurationId) == 30 && dentist?.blob(notesId) == Data("bring card\r\n".utf8))
    check("Standup: subject, location, start, duration",
          standup?.string(subjectId) == "Standup" && standup?.string(apptLocationId) == "Room 4"
          && standup?.filetime(apptStartId) == 0x01D9_E0F1_0000_0000 && standup?.int(apptDurationId) == 15)
    check("readRecord is nil at the end of the database", try client.readRecord(handle: handle) == nil)
    try seekChecks(client, handle: handle, first: dentist?.oid, last: standup?.oid)
    try client.closeHandle(handle)
    check("reading after close throws", fails { _ = try client.readRecord(handle: handle) })
    check("opening an unknown oid throws", fails { _ = try client.openDatabase(oid: 0xDEAD) })
    try contactChecks(client)
    try scratchDatabaseChecks(client)
}

func seekChecks(_ client: RapiClient, handle: UInt32, first: UInt32?, last: UInt32?) throws {
    let beginning = try client.seekDatabase(handle: handle, type: .beginning)
    check("seek beginning is index 0 of the first record", beginning.index == 0 && beginning.oid == first)
    let end = try client.seekDatabase(handle: handle, type: .end)
    check("seek end is index 1 of the last record", end.index == 1 && end.oid == last)
    check("read after seek end gives Standup", try client.readRecord(handle: handle)?.string(subjectId) == "Standup")
    check("seek current -2 returns to index 0", try client.seekDatabase(handle: handle, type: .current, value: -2).index == 0)
    check("seek by oid finds the record", try client.seekDatabase(handle: handle, type: .oid, value: Int(end.oid)).oid == end.oid)
    check("seek past the end reports oid 0", try client.seekDatabase(handle: handle, type: .beginning, value: 99).oid == 0)
}

func contactChecks(_ client: RapiClient) throws {
    guard let contacts = try client.findDatabase(named: "contacts database") else {
        check("findDatabase(named:) is case-insensitive", false)
        return
    }
    check("findDatabase(named:) is case-insensitive", contacts.name == "Contacts Database" && contacts.numRecords == 1)
    let handle = try client.openDatabase(oid: contacts.oid)
    defer { try? client.closeHandle(handle) }
    let grace: [PropVal] = [
        .string(firstNameId, "Grace"), .string(lastNameId, "Hopper"), .string(fullNameId, "Grace Hopper"),
        .i2(sensitivityId, 1), .filetime(birthdayId, 0x01A4_5C6E_9A8B_0000), .blob(notesId, Data("COBOL\r\n".utf8)),
    ]
    let oid = try client.writeRecord(handle: handle, props: grace)
    check("writeRecord returns a new oid", oid != 0)
    let (info, records) = try client.readAllRecords(named: "Contacts Database")
    check("readAllRecords(named:) sees both contacts", info.numRecords == 2 && records.count == 2)
    check("seeded contact is intact", records.first { $0.oid != oid }?.string(fullNameId) == "Ada Lovelace")
    let written = records.first { $0.oid == oid }
    check("string values round-trip", written?.string(firstNameId) == "Grace" && written?.string(fullNameId) == "Grace Hopper")
    check("i2 value round-trips", written?.int(sensitivityId) == 1)
    check("filetime value round-trips", written?.filetime(birthdayId) == 0x01A4_5C6E_9A8B_0000)
    check("blob value round-trips", written?.blob(notesId) == Data("COBOL\r\n".utf8))

    let update: [PropVal] = [.string(lastNameId, "Murray"), .deleted(propId: notesId, kind: .blob)]
    check("update keeps the oid", try client.writeRecord(handle: handle, props: update, oid: oid) == oid)
    let updated = try client.readAllRecords(named: "Contacts Database").1.first { $0.oid == oid }
    check("PROPDELETE removed the note and kept the rest",
          updated?.get(notesId) == nil && updated?.string(lastNameId) == "Murray" && updated?.string(firstNameId) == "Grace")
    check("updating an unknown oid throws", fails { _ = try client.writeRecord(handle: handle, props: update, oid: 0x7777) })
    try client.deleteRecord(handle: handle, oid: oid)
    check("deleteRecord leaves the seeded contact", try client.findDatabase(named: "Contacts Database")?.numRecords == 1)
    check("deleting the record again throws", fails { try client.deleteRecord(handle: handle, oid: oid) })
}

func scratchDatabaseChecks(_ client: RapiClient) throws {
    let bySubject = DatabaseInfo.SortSpec(propid: Cedb.propid(subjectId, .string), flags: 0)
    let oid = try client.createDatabase(named: "Swift Scratch", type: 0x300, sortSpecs: [bySubject])
    let listed = try client.findAllDatabases(type: 0x300, flags: RapiClient.Fad.listing | RapiClient.Fad.sortSpecs)
    check("createDatabase shows up with its type and sort spec",
          listed.map(\.oid) == [oid] && listed.first?.numSortOrder == 1
          && listed.first?.sortSpecs.count == 4 && listed.first?.sortSpecs.first == bySubject)
    check("creating a duplicate database throws", fails { _ = try client.createDatabase(named: "swift scratch") })
    try client.deleteDatabase(oid: oid)
    check("deleteDatabase removes it", try client.findDatabase(named: "Swift Scratch") == nil)
    check("deleting a missing database throws", fails { try client.deleteDatabase(oid: oid) })
}

// MARK: - CEDB record codec (tests/test_cedb.py, no network)

/// The 60-byte layout tests/test_cedb.py::test_pack_layout_matches_librapi2 expects.
func canonicalRecordBytes() -> Data {
    var expected = WireWriter()
    expected.u32(0x4223_0002); expected.u16(0); expected.u16(0); expected.u32(0); expected.u32(0)    // i2 0x4223 = 0
    expected.u32(0x0037_001F); expected.u16(0); expected.u16(0); expected.u32(48); expected.u32(0)   // string at 48
    expected.u32(0x0017_0041); expected.u16(0); expected.u16(0); expected.u32(3); expected.u32(56)   // 3-byte blob at 56
    expected.bytes(WireWriter.wstr("Hi")); expected.bytes(Data([0, 0]))                               // "Hi\0" + pad
    expected.bytes(Data("abc".utf8)); expected.bytes(Data([0]))                                       // "abc" + pad
    return expected.data
}

/// One hand-built CEPROPVAL entry.
func entryBytes(_ cepropid: UInt32, flags: UInt16, low: UInt32, high: UInt32) -> Data {
    var writer = WireWriter()
    writer.u32(cepropid); writer.u16(0); writer.u16(flags); writer.u32(low); writer.u32(high)
    return writer.data
}

func unpackRejects(_ data: Data, count: Int = 1) -> Bool {
    do { _ = try Cedb.unpack(data, count: count) } catch is CedbError { return true } catch { return false }
    return false
}

func packRejects(_ prop: PropVal) -> Bool {
    do { _ = try Cedb.pack([prop]) } catch is CedbError { return true } catch { return false }
    return false
}

let everyKind: [PropVal] = [
    .i2(1, -5), .i2(1, 0x7FFF), .ui2(2, 0xFFFF), .i4(3, -2_000_000_000), .ui4(4, 0xFFFF_FFFF),
    .bool(5, true), .bool(5, false), .r8(6, 2.5), .filetime(7, 0x01D9_E0F0_9B2C_3D4E),
    .string(8, ""), .string(8, "héllo wörld — ünïcode"), .blob(9, Data()), .blob(9, Data([0, 1, 2])),
    .blob(9, Data((0..<768).map { UInt8($0 % 256) } + [0x78])),
]

func cedbChecks() throws {
    let props: [PropVal] = [.i2(0x4223, 0), .string(0x0037, "Hi"), .blob(0x0017, Data("abc".utf8))]
    let packed = try Cedb.pack(props)
    check("canonical record packs to 60 bytes", packed.count == 60)
    check("canonical layout matches librapi2 byte for byte", packed == canonicalRecordBytes())
    check("canonical record unpacks to the same props", try Cedb.unpack(packed, count: 3) == props)
    check("align rounds up to four", [0, 1, 3, 4, 5, 8, 13].map(Cedb.align) == [0, 4, 4, 4, 8, 8, 16])
    check("propid composition", Cedb.propid(0x4223, .i2) == 0x4223_0002 && PropVal.string(0x37, "x").cepropid == 0x0037_001F)
    for prop in everyKind {
        check("round trip \(prop.kind.name) \(prop.value)", try Cedb.unpack(Cedb.pack([prop]), count: 1) == [prop])
    }
    let many = (1...11).map { PropVal.string(UInt16($0), String(repeating: "s", count: $0)) }
        + [PropVal.blob(99, Data(repeating: 0x7A, count: 7))]
    check("many properties keep order and alignment", try Cedb.unpack(Cedb.pack(many), count: many.count) == many)
    let gone = PropVal.deleted(propId: 0x0017, kind: .blob)
    let goneBack = try Cedb.unpack(Cedb.pack([gone]), count: 1)
    check("deleted property carries its flag through packing", gone.value == .blob(Data()) && goneBack == [gone])
    let missing = try Cedb.unpack(entryBytes(0x0001_0002, flags: Cedb.propNotFound, low: 0, high: 0), count: 1)
    check("PROPNOTFOUND unpacks as missing", missing == [PropVal(propId: 1, kind: .i2, value: .missing, flags: Cedb.propNotFound)])
    let unknown = try Cedb.unpack(entryBytes(0x0001_007F, flags: 0, low: 0x0302_0100, high: 0x0706_0504), count: 1)
    check("unknown type keeps its raw union bytes",
          unknown.first?.kind == .unknown(0x7F) && unknown.first?.value == .raw(Data([0, 1, 2, 3, 4, 5, 6, 7]))
          && unknown.first?.kind.name == "0x007f")
    check("bad string offset is rejected", unpackRejects(entryBytes(0x0037_001F, flags: 0, low: 999, high: 0)))
    check("bad blob range is rejected", unpackRejects(entryBytes(0x0017_0041, flags: 0, low: 100, high: 16)))
    check("short buffer is rejected", unpackRejects(Data(count: 15)))
    check("ui2 with a signed value is rejected at pack", packRejects(PropVal(propId: 1, kind: .ui2, value: .int(-1))))
    check("unknown type is rejected at pack", packRejects(PropVal(propId: 1, kind: .unknown(0x7F), value: .uint(1))))
    let stamp = Date(timeIntervalSince1970: 1_700_000_000.5)
    check("filetime helpers round-trip",
          FileTime.toDate(ticks: 0) == nil && FileTime.toDate(ticks: FileTime.fromDate(stamp)) == stamp)
}

switch arguments[1] {
case "rapi":
    let client = RapiClient(host: "127.0.0.1", port: port, timeout: 10)
    do {
        try client.connect()
        let version = try client.version()
        check("version 2.11.\(version.build)", version.major == 2 && version.minor == 11)

        let root = try client.listDirectory("\\")
        check("root has My Documents dir", root.contains { $0.name == "My Documents" && $0.isDirectory })

        try client.createDirectory("\\My Documents\\SwiftTest")
        var payload = Data()
        for index in 0..<20_000 { payload.append(UInt8(index % 251)) }
        var progressCalls = 0
        try client.upload("\\My Documents\\SwiftTest\\blob.bin", data: payload,
                          chunk: 4096, progress: { _, _ in progressCalls += 1 })
        check("upload progress calls == 5", progressCalls == 5)

        var downloaded = Data()
        let total = try client.download("\\My Documents\\SwiftTest\\blob.bin", chunk: 3000) { downloaded.append($0) }
        check("download roundtrip 20000B", total == 20_000 && downloaded == payload)

        try client.moveFile(from: "\\My Documents\\SwiftTest\\blob.bin",
                            to: "\\My Documents\\SwiftTest\\blob2.bin")
        let listing = try client.listDirectory("\\My Documents\\SwiftTest")
        check("move + list shows blob2.bin", listing.map(\.name) == ["blob2.bin"])
        check("entry size + date parsed", listing[0].size == 20_000 && listing[0].modified != nil)

        let pid = try client.createProcess("\\Windows\\pword.exe", commandLine: nil)
        check("createProcess pid", pid == 0x1001)

        let store = try client.storeInformation()
        check("store info", store.freeSize == 9 * 1024 * 1024)
        let power = try client.powerStatus()
        check("power status", power.batteryPercent == 77 && power.acLine == 1)

        try client.deleteFile("\\My Documents\\SwiftTest\\blob2.bin")
        try client.removeDirectory("\\My Documents\\SwiftTest")
        let after = try client.listDirectory("\\My Documents")
        check("cleanup removed dir", !after.contains { $0.name == "SwiftTest" })

        var missingFailed = false
        do { _ = try client.download("\\nope.txt") { _ in } } catch { missingFailed = true }
        check("missing file raises", missingFailed)

        try databaseChecks(client)
    } catch {
        check("unexpected error: \(error)", false)
    }
    client.close()

case "dccm":
    let semaphore = DispatchSemaphore(value: 0)
    final class Box: @unchecked Sendable { var info: DccmListener.DeviceInfo? }
    let box = Box()
    let listener = DccmListener(port: port, allowedPeer: "127.0.0.1") { event in
        switch event {
        case .deviceConnected(let info):
            box.info = info
            semaphore.signal()
        case .portBusy:
            print("FAIL  port busy")
            exit(1)
        default: break
        }
    }
    listener.start()
    guard semaphore.wait(timeout: .now() + 15) == .success else {
        check("device connected", false)
        exit(1)
    }
    let info = box.info
    check("device name", info?.name == "Jornada680")
    check("device os", info?.osMajor == 2 && info?.osMinor == 11)
    check("device hardware", info?.hardware == "SH3")
    Thread.sleep(forTimeInterval: 0.3)
    listener.stop()

case "gpib":
    // Against tests/serve_fake_gateway.py (Prologix dialect, one instrument at address 1).
    let gateway = GpibGateway(timeout: 3)
    do {
        try gateway.connect(host: "127.0.0.1", port: port)
        check("++ver answered", try gateway.version() == "fake gateway 1.0")
        check("*IDN? at address 1", try gateway.query(address: 1, "*IDN?") == "FAKE,TDS 340,0,FV:v1.02")
        check("serial poll 64", try gateway.serialPoll(address: 1) == 64)
        try gateway.write(address: 1, "ACQUIRE:STATE RUN")
        try gateway.interfaceClear()
        try gateway.deviceClear(address: 1)
        var timedOut = false
        do { _ = try gateway.read(address: 2) } catch GpibGateway.GpibError.timeout { timedOut = true }
        check("read at an idle address times out", timedOut)
        check("escape marks CR LF ESC and +", GpibGateway.escape(Data("a+b\r\n\u{1b}".utf8)) == Data("a\u{1b}+b\u{1b}\r\u{1b}\n\u{1b}\u{1b}".utf8))
        gateway.close()
    } catch {
        check("gpib self-test threw \(error)", false)
    }

case "cedb":
    // Record codec against the canonical layout in tests/test_cedb.py (no network).
    do {
        try cedbChecks()
    } catch {
        check("unexpected error: \(error)", false)
    }

case "mirror":
    // Cross-language parity: write a mirror tree that the Python
    // implementation's tests then validate (tests/check_mirror_parity.py).
    let rootDir = URL(fileURLWithPath: CommandLine.arguments[2])
    let base = Date(timeIntervalSince1970: 1_700_000_000)
    _ = try SendMirror.archive(Data("one".utf8), devicePath: "\\My Documents\\parity.txt",
                               source: "/tmp/src.txt", rootOverride: rootDir, date: base)
    _ = try SendMirror.archive(Data("one".utf8), devicePath: "\\My Documents\\parity.txt",
                               source: "/tmp/src.txt", rootOverride: rootDir, date: base + 30)
    _ = try SendMirror.archive(Data("two".utf8), devicePath: "\\My Documents\\parity.txt",
                               source: "/tmp/src.txt", rootOverride: rootDir, date: base + 60)
    _ = try SendMirror.archive(Data("x".utf8), devicePath: "\\..\\evil.txt",
                               source: nil, rootOverride: rootDir, date: base + 90)
    print("mirror parity tree written to \(rootDir.path)")

case "usb-profiles":
    // Cross-language parity: dump the USB link table for tests/check_usb_parity.py.
    let output = URL(fileURLWithPath: CommandLine.arguments[2])
    try UsbProfiles.tableJSON().write(to: output)
    print("usb table written to \(output.path) (\(UsbProfiles.drivers.count) drivers, \(UsbProfiles.handhelds.count) handhelds)")
    check("ftdi classified as serial bridge",
          UsbProfiles.classify(vendorId: 0x0403, productId: 0x6001)?.role == .serialBridge)
    check("dock marker upgrades a bridge",
          UsbProfiles.classify(vendorId: 0x0403, productId: 0x6001, product: "Jornada Dock Bridge")?.role == .dockBridge)
    check("hp usb sync is a wince device",
          UsbProfiles.classify(vendorId: 0x03F0, productId: 0x2016)?.role == .winceUsbSync)
    check("cdc-acm by interface", UsbProfiles.classify(vendorId: 0x2341, productId: 1, interfaceClasses: [(2, 2)])?.profile.key == "usb-cdc-acm")
    check("sh3 family from dccm hardware", UsbProfiles.identifyHandheld(hardware: "SH3", name: "Pocket")?.key == "sh3-hpc-pro-family")
    check("model number from name", UsbProfiles.identifyHandheld(hardware: "SH3", name: "Jornada680")?.key == "jornada-680")
    let twoNodes = UsbDevice(vendorId: 0x0403, productId: 0x6001, vendor: "FTDI", product: "FT232R USB UART", serial: "AB",
                             locationId: 1, deviceClass: 0, interfaces: [], serialNodes: [
                                UsbSerialNode(path: "/dev/cu.usbserial-AB", driver: "com.ftdi.vcp.dext", writable: false),
                                UsbSerialNode(path: "/dev/cu.usbserial-3", driver: "com.apple.DriverKit-AppleUSBFTDI", writable: true)])
    let diagnosis = UsbDoctor.diagnose([twoNodes], pinned: nil, handheld: UsbProfiles.handheld("jornada-680e"))
    check("doctor prefers the apple node", diagnosis.recommended == "/dev/cu.usbserial-3")
    check("doctor explains the inert dock jack", diagnosis.findings.contains { $0.title.contains("inert") })
    check("doctor worst level is warn", diagnosis.worstLevel == .warn)
    check("pinned port wins", UsbDoctor.recommendedSerialPath([twoNodes], pinned: "/dev/cu.usbserial-AB") == "/dev/cu.usbserial-AB")
    check("no adapter is an error", UsbDoctor.diagnose([]).worstLevel == .error)

case "usb":
    // Live listing of this Mac's USB bus (manual check; needs no hardware to run).
    let diagnosis = UsbDoctor.diagnose(UsbRegistry.devices(), pinned: UsbDoctor.readPin(), handheld: nil)
    for item in diagnosis.devices {
        print("\(item.device.vidPid)  \(item.device.label)  \(item.classification?.profile.name ?? "-")  \(item.role?.rawValue ?? "")")
        for node in item.device.serialNodes {
            print("    \(node.path)  \(node.driver)  \(node.writable ? "openable" : "not openable")")
        }
    }
    for finding in diagnosis.findings { print("\(finding.level.rawValue.uppercased())  \(finding.title)") }
    print("recommended: \(diagnosis.recommended ?? "none")")

case "runner":
    let scratch = FileManager.default.temporaryDirectory
        .appendingPathComponent("jornada-selftest-\(ProcessInfo.processInfo.processIdentifier)")
    let path = try PppController.writeRunnerScript(device: "/dev/cu.usbserial-TEST", baud: 115200,
                                                   directory: scratch)
    print(path)

default:
    exit(2)
}

exit(failures == 0 ? 0 : 1)
