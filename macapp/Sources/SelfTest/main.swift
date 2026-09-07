import Foundation
import JornadaCore

/// Protocol self-test: exercises the Swift RAPI client against the Python
/// fake device (tests/fake_device.py), and the Swift dccm listener against the
/// Python fake ActiveSync client. Usage:
///   SelfTest rapi <port>
///   SelfTest dccm <port>     (listens; exits 0 once a device handshakes + 2 pings)
let arguments = CommandLine.arguments
guard arguments.count >= 3 else {
    FileHandle.standardError.write(
        Data("usage: SelfTest rapi <port> | dccm <port> | gpib <port> | mirror <dir> | runner <n> | usb-profiles <file> | usb <n>\n".utf8))
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
