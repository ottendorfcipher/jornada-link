import Foundation

/// Controls the PPP side: detects the ppp interface and starts/stops the
/// repo's `bin/jornada-ppp` wrapper via the system admin-authorization dialog.
public enum PppController {
    public static let macIp = "192.168.131.102"
    public static let deviceIp = "192.168.131.201"

    /// True when a pppN interface with an address exists.
    public static func linkIsUp() -> Bool {
        var addresses: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&addresses) == 0 else { return false }
        defer { freeifaddrs(addresses) }
        var cursor = addresses
        while let current = cursor {
            let name = String(cString: current.pointee.ifa_name)
            if name.hasPrefix("ppp"),
               let sa = current.pointee.ifa_addr, sa.pointee.sa_family == sa_family_t(AF_INET) {
                return true
            }
            cursor = current.pointee.ifa_next
        }
        return false
    }

    public static func stateDirectory() -> URL {
        FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".jornada-link")
    }

    /// Locate bin/jornada-ppp: env override, then the repo next to the app
    /// bundle, then the default Desktop checkout.
    public static func wrapperPath() -> String? {
        let candidates: [String?] = [
            ProcessInfo.processInfo.environment["JORNADA_PPP_WRAPPER"],
            Bundle.main.bundleURL
                .deletingLastPathComponent()   // dist/
                .deletingLastPathComponent()   // macapp/
                .deletingLastPathComponent()   // repo root
                .appendingPathComponent("bin/jornada-ppp").path,
            (NSHomeDirectory() as NSString).appendingPathComponent("Desktop/jornada-link/bin/jornada-ppp"),
        ]
        for candidate in candidates {
            if let candidate, FileManager.default.isExecutableFile(atPath: candidate) {
                return candidate
            }
        }
        return nil
    }

    public enum ControlError: Error, CustomStringConvertible {
        case wrapperMissing
        case authorizationFailed(String)

        public var description: String {
            switch self {
            case .wrapperMissing:
                return "bin/jornada-ppp not found — set JORNADA_PPP_WRAPPER or keep the app inside the jornada-link repo"
            case .authorizationFailed(let output):
                return output.isEmpty ? "administrator authorization was cancelled" : output
            }
        }
    }

    /// Start the wrapper as root (system authorization dialog). The app's own
    /// dccm listener must already be running so the wrapper reuses it.
    public static func startLink() throws {
        guard let wrapper = wrapperPath() else { throw ControlError.wrapperMissing }
        let user = NSUserName()
        let log = stateDirectory().appendingPathComponent("ppp-wrapper.log").path
        let shell = "nohup env JORNADA_RUN_AS='\(user)' '\(wrapper)' >'\(log)' 2>&1 & echo ok"
        try runPrivileged(shell, prompt: "Jornada Sync needs administrator access to start the serial PPP link (pppd).")
    }

    public static func stopLink() throws {
        let shell = "pkill -f 'bin/jornada[-]ppp' ; pkill -f 'pppd /dev/cu[.]usbserial' ; echo ok"
        try runPrivileged(shell, prompt: "Jornada Sync needs administrator access to stop the serial PPP link.")
    }

    private static func runPrivileged(_ shell: String, prompt: String) throws {
        let escaped = shell
            .replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "\"", with: "\\\"")
        let script = "do shell script \"\(escaped)\" with administrator privileges with prompt \"\(prompt)\""
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        process.arguments = ["-e", script]
        let stderrPipe = Pipe()
        process.standardError = stderrPipe
        try process.run()
        process.waitUntilExit()
        if process.terminationStatus != 0 {
            let data = stderrPipe.fileHandleForReading.readDataToEndOfFile()
            throw ControlError.authorizationFailed(String(decoding: data, as: UTF8.self)
                .trimmingCharacters(in: .whitespacesAndNewlines))
        }
    }
}
