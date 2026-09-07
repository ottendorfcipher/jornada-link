import Foundation

/// Which USB device should carry the link, and why or why not — Swift twin of
/// `jornada/usb_doctor.py`. Pure: takes a snapshot of the bus, the pinned port
/// and the known handheld; produces ranked candidates and human findings.
public enum UsbFindingLevel: String, Sendable, Comparable {
    case ok, info, warn, error

    private var rank: Int { [Self.ok: 0, .info: 1, .warn: 2, .error: 3][self] ?? 0 }
    public static func < (lhs: Self, rhs: Self) -> Bool { lhs.rank < rhs.rank }
}

public struct UsbFinding: Sendable, Equatable, Identifiable {
    public let level: UsbFindingLevel
    public let title: String
    public let detail: String
    public var id: String { "\(level.rawValue):\(title)" }
}

public struct UsbClassifiedDevice: Sendable, Identifiable {
    public let device: UsbDevice
    public let classification: UsbClassification?
    public var id: String { device.id }
    public var role: UsbRole? { classification?.role }
}

public struct UsbCandidate: Sendable, Identifiable {
    public let path: String
    public let driver: String
    public let writable: Bool
    public let device: UsbDevice
    public let classification: UsbClassification
    public let score: Int
    public var id: String { path }
}

public struct UsbDiagnosis: Sendable {
    public let devices: [UsbClassifiedDevice]
    public let candidates: [UsbCandidate]
    public let recommended: String?
    public let pinned: String?
    public let handheld: HandheldModel?
    public let findings: [UsbFinding]

    public var worstLevel: UsbFindingLevel { findings.map(\.level).max() ?? .ok }
}

public enum UsbDoctor {
    public static let docsPath = "docs/usb-link.md"

    public static func classify(_ devices: [UsbDevice]) -> [UsbClassifiedDevice] {
        UsbRegistry.linkCandidates(devices).map { device in
            UsbClassifiedDevice(device: device, classification: UsbProfiles.classify(
                vendorId: device.vendorId, productId: device.productId, product: device.product,
                serial: device.serial, interfaceClasses: device.interfaceClasses))
        }
    }

    static func score(role: UsbRole, driver: String, writable: Bool, pinned: Bool) -> Int {
        var score = role == .dockBridge ? 400 : (role == .serialBridge ? 200 : 0)
        if driver.hasPrefix("com.apple.") { score += 100 }
        score += writable ? 50 : -500
        if pinned { score += 10_000 }
        return score
    }

    /// Every serial node of every recognised bridge, best first.
    public static func rankCandidates(_ devices: [UsbDevice], pinned: String? = nil) -> [UsbCandidate] {
        var found: [UsbCandidate] = []
        for item in classify(devices) {
            guard let classification = item.classification, classification.role != .winceUsbSync else { continue }
            for node in item.device.serialNodes {
                found.append(UsbCandidate(
                    path: node.path, driver: node.driver, writable: node.writable, device: item.device,
                    classification: classification,
                    score: score(role: classification.role, driver: node.driver, writable: node.writable,
                                 pinned: pinned == node.path)))
            }
        }
        return found.sorted { ($0.score, $1.path) > ($1.score, $0.path) }
    }

    public static func recommendedSerialPath(_ devices: [UsbDevice], pinned: String? = nil) -> String? {
        rankCandidates(devices, pinned: pinned).first?.path
    }

    static func dockFindings(_ handheld: HandheldModel?, _ ranked: [UsbCandidate]) -> [UsbFinding] {
        guard let handheld else { return [] }
        let dockBridge = ranked.first { $0.classification.role == .dockBridge }
        switch handheld.architecture {
        case .sh3:
            if let bridge = dockBridge {
                return [UsbFinding(level: .ok, title: "\(handheld.name) is linked through the dock's USB jack",
                                   detail: "The bridge retrofitted into the dock (\(bridge.device.label)) carries the "
                                       + "handheld's RS-232 lines to \(bridge.path); the SH-3 itself never sees USB.")]
            }
            return [UsbFinding(level: .info, title: "\(handheld.name): the dock's USB-B jack is inert for this model",
                               detail: "The SH7709A CPU and HD64461 companion chip have no USB device controller, so the "
                                   + "connector's USB pins carry nothing. Connect the dock's DB-9 (or the sync cable) to a "
                                   + "USB-serial adapter, or retrofit a bridge into the dock — see \(docsPath).")]
        case .arm:
            return [UsbFinding(level: .info, title: "\(handheld.name) can enumerate on the dock's USB jack",
                               detail: "Its SA-1110 has a USB device controller and Windows CE 3.0 presents a USB Sync "
                                   + "function, but macOS ships no driver for it; jornada-link's native USB link is on the "
                                   + "roadmap (\(docsPath)). Use the serial path meanwhile.")]
        }
    }

    static func syncDeviceFindings(_ items: [UsbClassifiedDevice], _ handheld: HandheldModel?) -> [UsbFinding] {
        var findings: [UsbFinding] = []
        for item in items where item.role == .winceUsbSync {
            let name = item.classification?.profile.name ?? item.device.label
            findings.append(UsbFinding(
                level: .info, title: "Windows CE USB Sync device present (\(item.device.vidPid), \(name))",
                detail: "A handheld speaking USB natively. macOS has no driver for the CE USB Sync function; "
                    + "jornada-link's userspace USB link for it is on the roadmap (\(docsPath))."))
            if let handheld, handheld.architecture == .sh3 {
                findings.append(UsbFinding(level: .warn, title: "That USB Sync device is not the SH-3 Jornada",
                                           detail: "\(handheld.name) has no USB controller, so another Windows CE device is attached."))
            }
        }
        return findings
    }

    static func adapterFindings(_ items: [UsbClassifiedDevice], recommended: String?) -> [UsbFinding] {
        var findings: [UsbFinding] = []
        for item in items {
            guard let classification = item.classification, classification.role != .winceUsbSync else { continue }
            let profile = classification.profile
            let nodes = item.device.serialNodes
            if nodes.isEmpty {
                let hint = profile.macosDriver == .builtIn
                    ? "macOS should have created one by itself; try another port or cable."
                    : "install the vendor driver (\(profile.driverName)) and reconnect the adapter."
                findings.append(UsbFinding(level: .error, title: "\(profile.name) has no serial node",
                                           detail: "\(item.device.label) (\(item.device.vidPid)) is attached but no /dev/cu.* "
                                               + "node exists for it — \(hint)"))
                continue
            }
            if nodes.count > 1 {
                let drivers = nodes.map { "\($0.path) (\($0.driver))" }.joined(separator: ", ")
                findings.append(UsbFinding(level: .info, title: "\(profile.name) exposes \(nodes.count) serial nodes",
                                           detail: "Two drivers are bound to the same adapter: \(drivers). "
                                               + "The link uses \(recommended ?? nodes[0].path)."))
            }
            for node in nodes where !node.writable {
                findings.append(UsbFinding(level: .warn, title: "\(node.path) is root-only",
                                           detail: "The node created by \(node.driver) is not openable by this user. The root "
                                               + "pppd can still use it, but `jornada probe` and the pre-connect flush cannot, "
                                               + "so the link prefers another node of the same adapter when there is one."))
            }
        }
        return findings
    }

    /// The complete picture for a snapshot of the USB bus.
    public static func diagnose(_ devices: [UsbDevice], pinned: String? = nil,
                                handheld: HandheldModel? = nil) -> UsbDiagnosis {
        let items = classify(devices)
        let ranked = rankCandidates(devices, pinned: pinned)
        let recommended = ranked.first?.path
        var findings: [UsbFinding] = []

        if let pinned, !ranked.contains(where: { $0.path == pinned }) {
            findings.append(UsbFinding(level: .warn, title: "Pinned serial port \(pinned) is not present",
                                       detail: "The pin in ~/.jornada-link/serial does not match any attached adapter; "
                                           + "the best attached adapter is used instead. Re-pin or unpin it."))
        }
        if ranked.isEmpty, !items.contains(where: { $0.role == .winceUsbSync }) {
            findings.append(UsbFinding(level: .error, title: "No USB-serial adapter found",
                                       detail: "Plug the adapter into the Jornada's sync cable or the dock's DB-9 port. FTDI "
                                           + "adapters need no driver on macOS; Prolific and WCH chips usually need the "
                                           + "vendor's DriverKit extension."))
        }
        findings += syncDeviceFindings(items, handheld)
        findings += dockFindings(handheld, ranked)
        findings += adapterFindings(items, recommended: recommended)
        if let best = ranked.first {
            let via = pinned == best.path ? "pinned" : "best available"
            let access = best.writable ? "openable by this user" : "root-only (unpin to let the doctor choose)"
            findings.append(UsbFinding(level: .ok, title: "Serial link port: \(best.path) (\(via))",
                                       detail: "\(best.classification.profile.name), driver \(best.driver), \(access)."))
        }
        return UsbDiagnosis(devices: items, candidates: ranked, recommended: recommended, pinned: pinned,
                            handheld: handheld, findings: findings)
    }

    // MARK: - The pin file (~/.jornada-link/serial), shared with the CLI and bin/jornada-ppp

    public static func pinFile(directory: URL = PppController.stateDirectory()) -> URL {
        directory.appendingPathComponent("serial")
    }

    public static func readPin(directory: URL = PppController.stateDirectory()) -> String? {
        guard let text = try? String(contentsOf: pinFile(directory: directory), encoding: .utf8) else { return nil }
        let first = text.split(whereSeparator: \.isNewline).first.map { $0.trimmingCharacters(in: .whitespaces) } ?? ""
        return PppController.isValidSerialPath(first) ? first : nil
    }

    public static func writePin(_ path: String, directory: URL = PppController.stateDirectory()) throws {
        guard PppController.isValidSerialPath(path) else {
            throw PppController.ControlError.invalidSerialPath(path)
        }
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        try (path + "\n").write(to: pinFile(directory: directory), atomically: true, encoding: .utf8)
    }

    public static func clearPin(directory: URL = PppController.stateDirectory()) {
        try? FileManager.default.removeItem(at: pinFile(directory: directory))
    }
}
