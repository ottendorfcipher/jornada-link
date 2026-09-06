import Foundation

/// Client for the jornada-gpib gateway (`gpibsrv.exe` on the device), which speaks the
/// Prologix GPIB-ETHERNET command language over TCP port 1234 of the PPP link.
/// Blocking; call from a background queue.
public final class GpibGateway {
    public enum GpibError: Error, CustomStringConvertible {
        case notConnected
        case unreachable(String)
        case timeout
        case badAnswer(String)

        public var description: String {
            switch self {
            case .notConnected: return "not connected to the gateway"
            case .unreachable(let why): return "cannot reach the GPIB gateway (\(why)); is gpibsrv.exe running on the Jornada?"
            case .timeout: return "no answer from the instrument (timeout)"
            case .badAnswer(let text): return "unexpected answer from the gateway: \(text)"
            }
        }
    }

    public static let defaultPort: UInt16 = 1234
    private let socket = TcpSocket()
    private let timeout: Double
    private var connected = false

    public init(timeout: Double = 5.0) {
        self.timeout = timeout
    }

    public var isConnected: Bool { connected }

    public func connect(host: String, port: UInt16 = GpibGateway.defaultPort) throws {
        do {
            try socket.connect(host: host, port: port, timeoutSeconds: timeout)
        } catch {
            throw GpibError.unreachable("\(error)")
        }
        connected = true
    }

    public func close() {
        socket.close()
        connected = false
    }

    /// Escape the bytes the Prologix line protocol treats specially (CR, LF, ESC, '+').
    public static func escape(_ data: Data) -> Data {
        var out = Data(capacity: data.count + 8)
        for byte in data {
            if byte == 0x0A || byte == 0x0D || byte == 0x1B || byte == 0x2B { out.append(0x1B) }
            out.append(byte)
        }
        return out
    }

    public func sendLine(_ line: String) throws {
        guard connected else { throw GpibError.notConnected }
        var data = Data(line.utf8)
        if !line.hasPrefix("++") { data = Self.escape(data) }
        data.append(0x0A)
        try socket.sendAll(data)
    }

    /// Read until a line feed, or until nothing more arrives within the timeout.
    public func readLine() throws -> String {
        guard connected else { throw GpibError.notConnected }
        var data = Data()
        while true {
            do {
                let chunk = try socket.recvUpTo(4096)
                data.append(chunk)
                if data.last == 0x0A { break }
            } catch TcpSocket.SocketError.timeout {
                if data.isEmpty { throw GpibError.timeout }
                break
            }
        }
        var text = String(decoding: data, as: UTF8.self)
        while text.hasSuffix("\n") || text.hasSuffix("\r") { text.removeLast() }
        return text
    }

    public func command(_ text: String) throws -> String {
        try sendLine(text)
        return try readLine()
    }

    public func setAddress(_ pad: Int) throws {
        guard (0...30).contains(pad) else { throw GpibError.badAnswer("address \(pad) outside 0..30") }
        try sendLine("++addr \(pad)")
    }

    public func write(address: Int, _ text: String) throws {
        try setAddress(address)
        try sendLine("++auto 0")
        try sendLine(text)
    }

    public func read(address: Int) throws -> String {
        try setAddress(address)
        try sendLine("++read eoi")
        return try readLine()
    }

    public func query(address: Int, _ text: String) throws -> String {
        try write(address: address, text)
        try sendLine("++read eoi")
        return try readLine()
    }

    public func serialPoll(address: Int) throws -> Int {
        let answer = try command("++spoll \(address)")
        guard let value = Int(answer.trimmingCharacters(in: .whitespaces)) else { throw GpibError.badAnswer(answer) }
        return value
    }

    public func interfaceClear() throws { try sendLine("++ifc") }
    public func deviceClear(address: Int) throws { try setAddress(address); try sendLine("++clr") }
    public func trigger(address: Int) throws { try setAddress(address); try sendLine("++trg") }
    public func local(address: Int) throws { try setAddress(address); try sendLine("++loc") }
    public func version() throws -> String { try command("++ver") }
    public func quitGateway() throws { try sendLine("++quit") }
}
