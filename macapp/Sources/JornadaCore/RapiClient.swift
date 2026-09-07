import Foundation

/// RAPI client for Windows CE 2.x devices ("old" librapi2 protocol, TCP 990).
/// Synchronous; call from a background queue/actor.
public final class RapiClient {
    public struct Command {
        public static let findAllFiles: UInt32 = 0x09
        public static let createFile: UInt32 = 0x05
        public static let readFile: UInt32 = 0x06
        public static let writeFile: UInt32 = 0x07
        public static let closeHandle: UInt32 = 0x08
        public static let createDirectory: UInt32 = 0x17
        public static let removeDirectory: UInt32 = 0x18
        public static let createProcess: UInt32 = 0x19
        public static let moveFile: UInt32 = 0x1A
        public static let deleteFile: UInt32 = 0x1C
        public static let getVersion: UInt32 = 0x3B
        public static let syncTimeToPc: UInt32 = 0x37
        public static let getStoreInformation: UInt32 = 0x29
        public static let getPowerStatus: UInt32 = 0x41
    }

    public struct Faf {
        public static let attributes: UInt32 = 0x01
        public static let lastWriteTime: UInt32 = 0x08
        public static let sizeHigh: UInt32 = 0x10
        public static let sizeLow: UInt32 = 0x20
        public static let name: UInt32 = 0x80
        public static let listing: UInt32 = attributes | lastWriteTime | sizeHigh | sizeLow | name
    }

    public static let attributeDirectory: UInt32 = 0x10
    public static let attributeInRom: UInt32 = 0x40
    public static let invalidHandle: UInt32 = 0xFFFF_FFFF
    public static let genericRead: UInt32 = 0x8000_0000
    public static let genericWrite: UInt32 = 0x4000_0000
    public static let openExisting: UInt32 = 3
    public static let createAlways: UInt32 = 2
    public static let defaultChunk = 28 * 1024

    public enum RapiError: Error, CustomStringConvertible {
        case notConnected
        case remote(String, winError: UInt32)
        case hresult(UInt32, command: UInt32)
        case protocolError(String)

        public var description: String {
            switch self {
            case .notConnected: return "not connected to the device"
            case .remote(let what, let code): return "\(what) failed (Windows error \(code))"
            case .hresult(let hr, let command):
                return String(format: "command 0x%02x failed, HRESULT 0x%08x", command, hr)
            case .protocolError(let reason): return "protocol error: \(reason)"
            }
        }
    }

    public struct FileEntry: Identifiable, Hashable {
        public let name: String
        public let attributes: UInt32
        public let size: UInt64
        public let modified: Date?
        public var id: String { name }
        public var isDirectory: Bool { attributes & RapiClient.attributeDirectory != 0 }
        public var isInRom: Bool { attributes & RapiClient.attributeInRom != 0 }
    }

    public struct VersionInfo {
        public let major: UInt32
        public let minor: UInt32
        public let build: UInt32
        public let platformId: UInt32
    }

    public struct StoreInfo {
        public let storeSize: UInt32
        public let freeSize: UInt32
    }

    public struct PowerStatus {
        public let acLine: UInt8
        public let batteryFlag: UInt8
        public let batteryPercent: UInt8
        public let backupPercent: UInt8
    }

    private let socket = TcpSocket()
    private let host: String
    private let port: UInt16
    private let timeout: Double

    public init(host: String, port: UInt16 = 990, timeout: Double = 45) {
        self.host = host
        self.port = port
        self.timeout = timeout
    }

    public var isConnected: Bool { socket.isOpen }

    public func connect(password: String? = nil, key: UInt8 = 0) throws {
        try socket.connect(host: host, port: port, timeoutSeconds: timeout)
        if let password, !password.isEmpty {
            var writer = WireWriter()
            let encoded = Data(WireWriter.wstr(password).map { $0 ^ key })
            writer.u16(UInt16(encoded.count))
            writer.bytes(encoded)
            try socket.sendAll(writer.data)
            let reply = try socket.recvExact(1)
            guard reply.first != 0 else {
                socket.close()
                throw RapiError.remote("password check", winError: 5)
            }
        }
    }

    public func close() { socket.close() }

    // MARK: - Core call (internal so extensions in other files can build on them)

    func call(_ command: UInt32, _ payload: Data = Data()) throws -> WireReader {
        guard socket.isOpen else { throw RapiError.notConnected }
        var writer = WireWriter()
        writer.u32(command)
        writer.bytes(payload)
        try socket.sendFrame(writer.data)
        var reader = WireReader(try socket.recvFrame())
        let result1 = try reader.u32()
        if result1 == 1 {
            let hresult = try reader.u32()
            throw RapiError.hresult(hresult, command: command)
        }
        return reader
    }

    struct Reply {
        let lastError: UInt32
        let returnValue: UInt32
        var reader: WireReader
    }

    func callSimple(_ command: UInt32, _ payload: Data = Data()) throws -> Reply {
        var reader = try call(command, payload)
        let lastError = try reader.u32()
        let returnValue = try reader.u32()
        return Reply(lastError: lastError, returnValue: returnValue, reader: reader)
    }

    func checkBool(_ what: String, _ reply: Reply) throws {
        guard reply.returnValue != 0 else {
            throw RapiError.remote(what, winError: reply.lastError)
        }
    }

    // MARK: - Directory listing

    public func listDirectory(_ directory: String) throws -> [FileEntry] {
        let base = directory == "\\" || directory.isEmpty ? "\\" : directory
        let pattern = base.hasSuffix("\\") ? base + "*" : base + "\\*"
        return try findAllFiles(pattern: pattern)
    }

    public func findAllFiles(pattern: String, flags: UInt32 = Faf.listing) throws -> [FileEntry] {
        var writer = WireWriter()
        writer.string(pattern)
        writer.u32(flags)
        var reader = try call(Command.findAllFiles, writer.data)
        let count = Int(try reader.u32())
        var entries: [FileEntry] = []
        entries.reserveCapacity(count)
        for _ in 0..<count {
            let nameSize = flags & Faf.name != 0 ? Int(try reader.u32()) : 0
            let attributes = flags & Faf.attributes != 0 ? try reader.u32() : 0
            var modified: Date?
            if flags & Faf.lastWriteTime != 0 {
                let low = try reader.u32()
                let high = try reader.u32()
                modified = FileTime.toDate(low: low, high: high)
            }
            let sizeHigh = flags & Faf.sizeHigh != 0 ? try reader.u32() : 0
            let sizeLow = flags & Faf.sizeLow != 0 ? try reader.u32() : 0
            let name = flags & Faf.name != 0 ? try reader.wchars(nameSize) : ""
            entries.append(FileEntry(
                name: name,
                attributes: attributes,
                size: (UInt64(sizeHigh) << 32) | UInt64(sizeLow),
                modified: modified
            ))
        }
        return entries
    }

    // MARK: - File I/O

    public func createFile(_ path: String, access: UInt32, share: UInt32 = 0,
                           disposition: UInt32, attributes: UInt32 = 0) throws -> UInt32 {
        var writer = WireWriter()
        writer.u32(access)
        writer.u32(share)
        writer.u32(disposition)
        writer.u32(attributes)
        writer.u32(0)
        writer.string(path)
        let reply = try callSimple(Command.createFile, writer.data)
        guard reply.returnValue != Self.invalidHandle else {
            throw RapiError.remote("open \(path)", winError: reply.lastError)
        }
        return reply.returnValue
    }

    public func readFile(handle: UInt32, size: Int) throws -> Data {
        var writer = WireWriter()
        writer.u32(handle)
        writer.optionalOut(UInt32(size))
        writer.optionalIn(nil)
        var reply = try callSimple(Command.readFile, writer.data)
        try checkBool("CeReadFile", reply)
        let bytesRead = Int(try reply.reader.u32())
        return try reply.reader.take(bytesRead)
    }

    public func writeFile(handle: UInt32, data: Data) throws -> Int {
        var writer = WireWriter()
        writer.u32(handle)
        writer.optionalIn(data)
        writer.optionalIn(nil)
        var reply = try callSimple(Command.writeFile, writer.data)
        try checkBool("CeWriteFile", reply)
        return Int(try reply.reader.u32())
    }

    public func closeHandle(_ handle: UInt32) throws {
        var writer = WireWriter()
        writer.u32(handle)
        try checkBool("CeCloseHandle", try callSimple(Command.closeHandle, writer.data))
    }

    /// Download `path`, streaming chunks to `sink`; returns total bytes.
    public func download(_ path: String, chunk: Int = RapiClient.defaultChunk,
                         sink: (Data) throws -> Void) throws -> Int {
        let handle = try createFile(path, access: Self.genericRead, share: 1, disposition: Self.openExisting)
        defer { try? closeHandle(handle) }
        var total = 0
        while true {
            let data = try readFile(handle: handle, size: chunk)
            if data.isEmpty { break }
            total += data.count
            try sink(data)
        }
        return total
    }

    /// Upload `data` to `path` (overwrites); reports progress in bytes written.
    public func upload(_ path: String, data: Data, chunk: Int = RapiClient.defaultChunk,
                       progress: ((Int, Int) -> Void)? = nil) throws {
        let handle = try createFile(path, access: Self.genericWrite, disposition: Self.createAlways)
        defer { try? closeHandle(handle) }
        var offset = 0
        while offset < data.count {
            let end = min(offset + chunk, data.count)
            let piece = data.subdata(in: offset..<end)
            let written = try writeFile(handle: handle, data: piece)
            guard written == piece.count else {
                throw RapiError.protocolError("short write (\(written) of \(piece.count) bytes)")
            }
            offset = end
            progress?(offset, data.count)
        }
    }

    // MARK: - File management

    public func createDirectory(_ path: String) throws {
        var writer = WireWriter()
        writer.optionalString(path)
        writer.optionalIn(nil)
        try checkBool("CeCreateDirectory", try callSimple(Command.createDirectory, writer.data))
    }

    public func removeDirectory(_ path: String) throws {
        var writer = WireWriter()
        writer.optionalString(path)
        try checkBool("CeRemoveDirectory", try callSimple(Command.removeDirectory, writer.data))
    }

    public func deleteFile(_ path: String) throws {
        var writer = WireWriter()
        writer.optionalString(path)
        try checkBool("CeDeleteFile", try callSimple(Command.deleteFile, writer.data))
    }

    public func moveFile(from source: String, to destination: String) throws {
        var writer = WireWriter()
        writer.optionalString(source)
        writer.optionalString(destination)
        try checkBool("CeMoveFile", try callSimple(Command.moveFile, writer.data))
    }

    public func createProcess(_ application: String, commandLine: String? = nil) throws -> UInt32 {
        var writer = WireWriter()
        writer.optionalString(application)
        writer.optionalString(commandLine)
        for _ in 0..<7 { writer.u32(0) }
        writer.optionalOut(16)
        var reply = try callSimple(Command.createProcess, writer.data)
        try checkBool("CeCreateProcess", reply)
        guard let info = try reply.reader.optional(), info.count >= 12 else { return 0 }
        var reader = WireReader(info)
        _ = try reader.u32()
        _ = try reader.u32()
        return try reader.u32()  // dwProcessId
    }

    /// CeSyncTimeToPc: set the device clock to this machine's current time.
    /// CeSyncTimeToPc: set the device's date and time from this Mac. The
    /// FILETIME carries both, so both are set. By default the Mac's local
    /// wall-clock is sent so the Jornada reads the same date and time as the
    /// Mac; pass `useLocal: false` for true UTC when the device's own
    /// time-zone is configured. Verify with `readDeviceClock()`.
    public func syncTimeFromMac(useLocal: Bool = true) throws {
        let offset = useLocal ? Double(TimeZone.current.secondsFromGMT()) : 0
        let wall = Date().timeIntervalSince1970 + offset
        let ticks = UInt64(wall * 10_000_000 + 116_444_736_000_000_000)
        var writer = WireWriter()
        writer.u32(UInt32(truncatingIfNeeded: ticks))
        writer.u32(UInt32(truncatingIfNeeded: ticks >> 32))
        writer.u32(0)
        writer.u32(10_000)
        var reader = try call(Command.syncTimeToPc, writer.data)
        _ = try reader.u32()  // last_error; command has no return value
    }

    /// Read the device clock by timestamping a throwaway file. The returned
    /// Date's UTC calendar fields are the device's displayed date and time.
    public func readDeviceClock(probeDir: String = "\\Temp") throws -> Date? {
        let probe = probeDir + "\\.jornada_clock"
        try upload(probe, data: Data())
        defer { try? deleteFile(probe) }
        let entries = try listDirectory(probeDir)
        return entries.first(where: { $0.name == ".jornada_clock" })?.modified
    }

    // MARK: - System information

    public func version() throws -> VersionInfo {
        var reply = try callSimple(Command.getVersion)
        try checkBool("CeGetVersionEx", reply)
        let size = Int(try reply.reader.u32())
        var reader = WireReader(try reply.reader.take(size))
        _ = try reader.u32()
        return VersionInfo(major: try reader.u32(), minor: try reader.u32(),
                           build: try reader.u32(), platformId: try reader.u32())
    }

    public func storeInformation() throws -> StoreInfo {
        var writer = WireWriter()
        writer.optionalOut(8)
        var reply = try callSimple(Command.getStoreInformation, writer.data)
        try checkBool("CeGetStoreInformation", reply)
        guard let data = try reply.reader.optional(), data.count >= 8 else {
            throw RapiError.protocolError("store info missing")
        }
        var reader = WireReader(data)
        return StoreInfo(storeSize: try reader.u32(), freeSize: try reader.u32())
    }

    public func powerStatus() throws -> PowerStatus {
        var writer = WireWriter()
        writer.optionalOut(24)
        writer.u32(1)
        var reply = try callSimple(Command.getPowerStatus, writer.data)
        try checkBool("CeGetSystemPowerStatusEx", reply)
        guard let data = try reply.reader.optional(), data.count >= 24 else {
            throw RapiError.protocolError("power status missing")
        }
        return PowerStatus(acLine: data[data.startIndex],
                           batteryFlag: data[data.startIndex + 1],
                           batteryPercent: data[data.startIndex + 2],
                           backupPercent: data[data.startIndex + 14])
    }
}
