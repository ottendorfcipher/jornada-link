import Foundation

/// The "desktop" half of a Windows CE ActiveSync connection (SynCE dccm):
/// listens on TCP 5679; the device announces itself and expects the ping word
/// 0x12345678 every few seconds or it hangs up the whole PPP link.
public final class DccmListener: @unchecked Sendable {
    public static let port: UInt16 = 5679
    private static let ping: UInt32 = 0x1234_5678
    private static let maxPacket = 512
    private static let minPacket = 0x24
    private static let pingInterval: Double = 5.0
    private static let maxMissedPings = 5

    public struct DeviceInfo: Equatable {
        public let name: String
        public let deviceClass: String
        public let hardware: String
        public let osMajor: Int
        public let osMinor: Int
        public let buildNumber: Int
        public let ip: String
        public let passwordKey: UInt8?

        public var osText: String { "Windows CE \(osMajor).\(osMinor < 10 ? "0" : "")\(osMinor)" }

        public init(name: String, deviceClass: String, hardware: String,
                    osMajor: Int, osMinor: Int, buildNumber: Int,
                    ip: String, passwordKey: UInt8?) {
            self.name = name
            self.deviceClass = deviceClass
            self.hardware = hardware
            self.osMajor = osMajor
            self.osMinor = osMinor
            self.buildNumber = buildNumber
            self.ip = ip
            self.passwordKey = passwordKey
        }
    }

    public enum Event {
        case listening(UInt16)
        case portBusy
        case deviceConnected(DeviceInfo)
        case deviceDisconnected(String)
        case passwordRequired
        case passwordRejected
        case log(String)
    }

    private let queue = DispatchQueue(label: "dccm.listener")
    private let port: UInt16
    private let stateLock = NSLock()
    private var listenerFd: Int32 = -1
    private var clientFd: Int32 = -1
    private var stopped = false

    private var isStopped: Bool {
        stateLock.lock()
        defer { stateLock.unlock() }
        return stopped
    }
    private let eventSink: @Sendable (Event) -> Void

    /// Password supplier: called on a background queue when the device
    /// presents a challenge; return nil to fail the connection.
    public var passwordProvider: (@Sendable () -> String?)?

    public init(port: UInt16 = DccmListener.port, events: @escaping @Sendable (Event) -> Void) {
        self.port = port
        self.eventSink = events
    }

    public func start() {
        queue.async { [self] in runListener() }
    }

    /// Safe to call from any thread; unblocks the accept/serve loops by
    /// closing their descriptors (never dispatches onto the busy queue).
    public func stop() {
        stateLock.lock()
        defer { stateLock.unlock() }
        stopped = true
        if clientFd >= 0 { Darwin.close(clientFd); clientFd = -1 }
        if listenerFd >= 0 { Darwin.close(listenerFd); listenerFd = -1 }
    }

    // MARK: - Listener loop (runs on `queue`)

    private func runListener() {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { eventSink(.log("dccm: socket() failed")); return }
        var one: Int32 = 1
        setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, socklen_t(MemoryLayout<Int32>.size))
        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = port.bigEndian
        address.sin_addr.s_addr = INADDR_ANY
        let bound = withUnsafePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard bound == 0, listen(fd, 2) == 0 else {
            Darwin.close(fd)
            eventSink(.portBusy)
            return
        }
        stateLock.lock()
        if stopped { stateLock.unlock(); Darwin.close(fd); return }
        listenerFd = fd
        stateLock.unlock()
        eventSink(.listening(port))
        while !isStopped {
            var peer = sockaddr_in()
            var length = socklen_t(MemoryLayout<sockaddr_in>.size)
            let client = withUnsafeMutablePointer(to: &peer) { pointer in
                pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                    accept(fd, $0, &length)
                }
            }
            guard client >= 0 else { break }
            var ipBuffer = [CChar](repeating: 0, count: Int(INET_ADDRSTRLEN))
            inet_ntop(AF_INET, &peer.sin_addr, &ipBuffer, socklen_t(INET_ADDRSTRLEN))
            let ip = String(cString: ipBuffer)
            stateLock.lock()
            clientFd = client
            stateLock.unlock()
            serveClient(client, ip: ip)
            stateLock.lock()
            clientFd = -1
            stateLock.unlock()
        }
    }

    private func serveClient(_ fd: Int32, ip: String) {
        let socket = TcpSocket()
        socket.adopt(descriptor: fd, timeoutSeconds: Self.pingInterval)
        var info: DeviceInfo?
        var announced = false
        var awaitingPasswordReply = false
        var passwordKey: UInt8?
        var missedPings = 0
        defer {
            socket.close()
            if announced { eventSink(.deviceDisconnected(ip)) }
        }
        while !isStopped && missedPings < Self.maxMissedPings {
            do {
                if awaitingPasswordReply {
                    var reader = WireReader(try socket.recvExact(2))
                    let ok = try reader.u16() != 0
                    guard ok else { eventSink(.passwordRejected); return }
                    try sendPing(socket)
                    awaitingPasswordReply = false
                    if let ready = info {
                        announced = true
                        eventSink(.deviceConnected(ready))
                    }
                    continue
                }
                var reader = WireReader(try socket.recvExact(4))
                let header = try reader.u32()
                if header == 0 { continue }
                if header == Self.ping { missedPings = 0; continue }
                if header < UInt32(Self.maxPacket) {
                    guard header >= UInt32(Self.minPacket) else {
                        eventSink(.log("dccm: runt info packet (\(header)B) from \(ip)"))
                        return
                    }
                    let body = try socket.recvExact(Int(header))
                    let parsed = try Self.parseInfoPacket(body, ip: ip, key: passwordKey)
                    info = parsed
                    if passwordKey != nil {
                        awaitingPasswordReply = true
                    } else {
                        try sendPing(socket)
                        announced = true
                        eventSink(.deviceConnected(parsed))
                    }
                    continue
                }
                // Password challenge: low byte is the XOR key.
                passwordKey = UInt8(header & 0xFF)
                eventSink(.passwordRequired)
                guard let password = passwordProvider?(), !password.isEmpty else {
                    eventSink(.log("dccm: device is password protected and no password is set"))
                    return
                }
                var writer = WireWriter()
                let encoded = Data(WireWriter.wstr(password).map { $0 ^ passwordKey! })
                writer.u16(UInt16(encoded.count))
                writer.bytes(encoded)
                try socket.sendAll(writer.data)
            } catch TcpSocket.SocketError.timeout {
                do {
                    try sendPing(socket)
                    missedPings += 1
                } catch {
                    return
                }
            } catch {
                if announced || info != nil {
                    eventSink(.log("dccm: session with \(ip) ended: \(error)"))
                }
                return
            }
        }
    }

    private func sendPing(_ socket: TcpSocket) throws {
        var writer = WireWriter()
        writer.u32(Self.ping)
        try socket.sendAll(writer.data)
    }

    // MARK: - Info packet

    public static func parseInfoPacket(_ body: Data, ip: String, key: UInt8?) throws -> DeviceInfo {
        var reader = WireReader(body)
        _ = try reader.take(4)
        let osVersion = try reader.u16()      // low byte major, high byte minor
        let buildNumber = try reader.u16()
        func string(at offset: Int) throws -> String {
            var cursor = WireReader(body)
            _ = try cursor.take(offset)
            let pointer = Int(try cursor.u32())
            guard pointer < body.count else { throw WireError.malformed("string offset \(pointer)") }
            var text = WireReader(body)
            _ = try text.take(pointer)
            return try text.wchars((body.count - pointer) / 2)
        }
        return DeviceInfo(
            name: try string(at: 0x18),
            deviceClass: try string(at: 0x1C),
            hardware: try string(at: 0x20),
            osMajor: Int(osVersion & 0xFF),
            osMinor: Int(osVersion >> 8),
            buildNumber: Int(buildNumber),
            ip: ip,
            passwordKey: key
        )
    }
}
