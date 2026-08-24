import Foundation

/// Minimal blocking TCP socket (BSD sockets). All calls are synchronous;
/// run them off the main thread.
public final class TcpSocket {
    public enum SocketError: Error, CustomStringConvertible {
        case system(String, Int32)
        case closed(String)
        case timeout(String)

        public var description: String {
            switch self {
            case .system(let what, let errnoValue):
                return "\(what): \(String(cString: strerror(errnoValue)))"
            case .closed(let what): return "\(what): connection closed"
            case .timeout(let what): return "\(what): timed out"
            }
        }
    }

    private var fd: Int32 = -1

    public init() {}
    deinit { close() }

    public var isOpen: Bool { fd >= 0 }

    public func connect(host: String, port: UInt16, timeoutSeconds: Double) throws {
        close()
        let socketFd = socket(AF_INET, SOCK_STREAM, 0)
        guard socketFd >= 0 else { throw SocketError.system("socket", errno) }
        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = port.bigEndian
        guard inet_pton(AF_INET, host, &address.sin_addr) == 1 else {
            Darwin.close(socketFd)
            throw SocketError.system("inet_pton \(host)", EINVAL)
        }
        setTimeout(socketFd, timeoutSeconds)
        // Non-blocking connect with an explicit deadline: SO_SNDTIMEO does not
        // bound connect() on Darwin (the OS default is ~75s, which made a dead
        // RAPI port feel like a frozen app).
        let connectDeadline = min(timeoutSeconds, 10.0)
        let originalFlags = fcntl(socketFd, F_GETFL, 0)
        _ = fcntl(socketFd, F_SETFL, originalFlags | O_NONBLOCK)
        let result = withUnsafePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.connect(socketFd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        if result != 0 {
            guard errno == EINPROGRESS else {
                let savedErrno = errno
                Darwin.close(socketFd)
                throw SocketError.system("connect \(host):\(port)", savedErrno)
            }
            var descriptor = pollfd(fd: socketFd, events: Int16(POLLOUT), revents: 0)
            let ready = poll(&descriptor, 1, Int32(connectDeadline * 1000))
            guard ready == 1 else {
                Darwin.close(socketFd)
                throw SocketError.timeout("connect \(host):\(port)")
            }
            var soError: Int32 = 0
            var soLength = socklen_t(MemoryLayout<Int32>.size)
            getsockopt(socketFd, SOL_SOCKET, SO_ERROR, &soError, &soLength)
            guard soError == 0 else {
                Darwin.close(socketFd)
                throw SocketError.system("connect \(host):\(port)", soError)
            }
        }
        _ = fcntl(socketFd, F_SETFL, originalFlags)
        var one: Int32 = 1
        setsockopt(socketFd, IPPROTO_TCP, TCP_NODELAY, &one, socklen_t(MemoryLayout<Int32>.size))
        fd = socketFd
    }

    public func adopt(descriptor: Int32, timeoutSeconds: Double) {
        close()
        setTimeout(descriptor, timeoutSeconds)
        fd = descriptor
    }

    private func setTimeout(_ descriptor: Int32, _ seconds: Double) {
        var tv = timeval(tv_sec: Int(seconds), tv_usec: Int32((seconds - Double(Int(seconds))) * 1e6))
        setsockopt(descriptor, SOL_SOCKET, SO_RCVTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
        setsockopt(descriptor, SOL_SOCKET, SO_SNDTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
    }

    public func close() {
        if fd >= 0 {
            Darwin.close(fd)
            fd = -1
        }
    }

    public func sendAll(_ data: Data) throws {
        guard fd >= 0 else { throw SocketError.closed("send") }
        var sent = 0
        try data.withUnsafeBytes { (raw: UnsafeRawBufferPointer) in
            while sent < raw.count {
                let n = Darwin.send(fd, raw.baseAddress!.advanced(by: sent), raw.count - sent, 0)
                if n <= 0 {
                    if n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK) { throw SocketError.timeout("send") }
                    throw n == 0 ? SocketError.closed("send") : SocketError.system("send", errno)
                }
                sent += n
            }
        }
    }

    public func recvExact(_ size: Int) throws -> Data {
        guard fd >= 0 else { throw SocketError.closed("recv") }
        var out = Data(capacity: size)
        var buffer = [UInt8](repeating: 0, count: 64 * 1024)
        while out.count < size {
            let want = min(buffer.count, size - out.count)
            let n = Darwin.recv(fd, &buffer, want, 0)
            if n <= 0 {
                if n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK) { throw SocketError.timeout("recv") }
                throw n == 0 ? SocketError.closed("recv") : SocketError.system("recv", errno)
            }
            out.append(contentsOf: buffer[0..<n])
        }
        return out
    }

    /// Length-prefixed frame I/O (u32 LE length + payload).
    public func sendFrame(_ payload: Data) throws {
        var writer = WireWriter()
        writer.u32(UInt32(payload.count))
        writer.bytes(payload)
        try sendAll(writer.data)
    }

    public func recvFrame(maxSize: Int = 8 * 1024 * 1024) throws -> Data {
        var reader = WireReader(try recvExact(4))
        let size = Int(try reader.u32())
        guard size <= maxSize else { throw SocketError.system("frame too large (\(size))", EMSGSIZE) }
        return try recvExact(size)
    }
}
