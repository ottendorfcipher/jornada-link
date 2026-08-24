import Foundation
import JornadaCore

/// Protocol self-test: exercises the Swift RAPI client against the Python
/// fake device (tests/fake_device.py), and the Swift dccm listener against the
/// Python fake ActiveSync client. Usage:
///   SelfTest rapi <port>
///   SelfTest dccm <port>     (listens; exits 0 once a device handshakes + 2 pings)
let arguments = CommandLine.arguments
guard arguments.count >= 3, let port = UInt16(arguments[2]) else {
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
    let listener = DccmListener(port: port) { event in
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

default:
    exit(2)
}

exit(failures == 0 ? 0 : 1)
