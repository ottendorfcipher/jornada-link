import Foundation
import JornadaCore

/// Serializes all RAPI calls onto one background queue and one connection.
/// The Jornada's rapisrv is single-threaded; ordering everything avoids
/// interleaved frames.
final class RapiService: @unchecked Sendable {
    private let queue = DispatchQueue(label: "rapi.service")
    private var client: RapiClient?
    private var host = PppController.deviceIp
    private var password: String?
    private var passwordKey: UInt8 = 0
    /// Gentleness contract: rapisrv on a CE 2.x device is a single-threaded
    /// 1998 service, and rapid connect/close cycles wedge it (observed live:
    /// a retry burst left port 990 refusing every connection while dccm kept
    /// working). Fresh connections are therefore rate-limited here, below any
    /// caller, so no retry logic above can turn into a storm.
    private var lastConnectAttempt = Date.distantPast
    private let minSecondsBetweenConnects: TimeInterval = 2.0

    func configure(host: String, password: String?, passwordKey: UInt8) {
        queue.async { [self] in
            self.host = host
            self.password = password
            self.passwordKey = passwordKey
            client?.close()
            client = nil
        }
    }

    func disconnect() {
        queue.async { [self] in
            client?.close()
            client = nil
        }
    }

    /// Run `work` with a connected client on the service queue.
    func run<T: Sendable>(_ label: String,
                          work: @escaping @Sendable (RapiClient) throws -> T) async throws -> T {
        try await withCheckedThrowingContinuation { continuation in
            queue.async { [self] in
                do {
                    let active: RapiClient
                    if let existing = client, existing.isConnected {
                        active = existing
                    } else {
                        let sinceLast = Date().timeIntervalSince(lastConnectAttempt)
                        if sinceLast < minSecondsBetweenConnects {
                            // Blocking this serial background queue briefly is the
                            // point: it spaces out every reconnect attempt.
                            Thread.sleep(forTimeInterval: minSecondsBetweenConnects - sinceLast)
                        }
                        lastConnectAttempt = Date()
                        let fresh = RapiClient(host: host)
                        try fresh.connect(password: password, key: passwordKey)
                        client = fresh
                        active = fresh
                    }
                    do {
                        continuation.resume(returning: try work(active))
                    } catch {
                        // A dead socket poisons the session; reconnect next call.
                        if let socketError = error as? TcpSocket.SocketError {
                            _ = socketError
                            active.close()
                            client = nil
                        }
                        throw error
                    }
                } catch {
                    continuation.resume(throwing: error)
                }
            }
        }
    }
}
