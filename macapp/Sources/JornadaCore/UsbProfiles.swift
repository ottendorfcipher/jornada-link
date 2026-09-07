import Foundation

/// The USB link table — Swift twin of `jornada/usb_profiles.py`.
///
/// Driver profiles for the USB devices that can carry a Jornada link, and the
/// capability matrix of the handhelds that fit the HP F1822A dock. The
/// Python module is the reference; `SelfTest usb-profiles` dumps this table and
/// `tests/check_usb_parity.py` verifies the two agree row for row.
public enum UsbRole: String, Codable, Sendable {
    /// A USB-to-RS-232 chip: gives us a /dev/cu.* node for the serial PPP path.
    case serialBridge = "serial-bridge"
    /// A serial bridge built into the dock, recognised by its USB strings.
    case dockBridge = "dock-bridge"
    /// A Windows CE handheld's own USB function ("Windows CE USB Devices").
    case winceUsbSync = "wince-usb-sync"
}

public enum UsbDriverAvailability: String, Codable, Sendable {
    case builtIn = "built-in"
    case vendorExtension = "vendor-extension"
    case none = "none"
}

public enum HandheldArchitecture: String, Codable, Sendable {
    case sh3 = "SH3"
    case arm = "ARM"
}

public struct UsbDriverProfile: Codable, Sendable, Equatable {
    public let key: String
    public let vendorId: Int?
    public let productId: Int?
    public let name: String
    public let chip: String
    public let role: UsbRole
    public let macosDriver: UsbDriverAvailability
    public let driverName: String
    public let notes: String

    enum CodingKeys: String, CodingKey {
        case key, name, chip, role, notes
        case vendorId = "vendor_id"
        case productId = "product_id"
        case macosDriver = "macos_driver"
        case driverName = "driver_name"
    }

    /// Explicit encoding so a missing vendor/product is written as `null`
    /// (the synthesized encoder would drop the key, breaking parity).
    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(key, forKey: .key)
        try container.encode(vendorId, forKey: .vendorId)
        try container.encode(productId, forKey: .productId)
        try container.encode(name, forKey: .name)
        try container.encode(chip, forKey: .chip)
        try container.encode(role, forKey: .role)
        try container.encode(macosDriver, forKey: .macosDriver)
        try container.encode(driverName, forKey: .driverName)
        try container.encode(notes, forKey: .notes)
    }

    public var vidPid: String {
        let vid = vendorId.map { String(format: "%04x", $0) } ?? "----"
        let pid = productId.map { String(format: "%04x", $0) } ?? "----"
        return "\(vid):\(pid)"
    }
}

public struct HandheldModel: Codable, Sendable, Equatable, Identifiable {
    public let key: String
    public let name: String
    public let cpu: String
    public let architecture: HandheldArchitecture
    public let os: String
    public let usbClient: String
    public let dockUsb: Bool
    public let notes: String

    public var id: String { key }

    enum CodingKeys: String, CodingKey {
        case key, name, cpu, architecture, os, notes
        case usbClient = "usb_client"
        case dockUsb = "dock_usb"
    }
}

public struct UsbClassification: Sendable, Equatable {
    public let profile: UsbDriverProfile
    public let role: UsbRole
}

public enum UsbProfiles {
    public static let usbClientNone = "none"
    public static let usbClientSA1110 = "sa1110-udc"
    public static let dockMarkers = ["JORNADA", "JDOCK"]
    static let usbClassCDC = 0x02
    static let usbSubclassACM = 0x02
    public static let usbClassHub = 0x09

    private static let sh3Note = "Hitachi SH7709A + HD64461 companion chip: no USB device controller on the board, "
        + "so the dock's USB-B jack carries nothing for this model. Serial (RS-232) only."
    private static let armNote = "StrongARM SA-1110 with an integrated USB device controller (UDC); enumerates on the "
        + "dock's USB-B jack as a Windows CE USB Sync device."
    private static let syncDriver = "wceusbsh.sys (Windows) / ipaq (Linux)"

    private static func ftdi(_ pid: Int, _ key: String, _ name: String, _ chip: String, _ notes: String = "") -> UsbDriverProfile {
        UsbDriverProfile(key: key, vendorId: 0x0403, productId: pid, name: name, chip: chip, role: .serialBridge,
                         macosDriver: .builtIn, driverName: "AppleUSBFTDI", notes: notes)
    }

    private static func prolific(_ pid: Int, _ key: String, _ name: String, _ chip: String, _ notes: String = "") -> UsbDriverProfile {
        UsbDriverProfile(key: key, vendorId: 0x067B, productId: pid, name: name, chip: chip, role: .serialBridge,
                         macosDriver: .vendorExtension, driverName: "Prolific PL2303 DriverKit extension", notes: notes)
    }

    private static func silabs(_ pid: Int, _ key: String, _ name: String, _ chip: String) -> UsbDriverProfile {
        UsbDriverProfile(key: key, vendorId: 0x10C4, productId: pid, name: name, chip: chip, role: .serialBridge,
                         macosDriver: .builtIn, driverName: "AppleUSBSLCOM",
                         notes: "Older macOS releases need Silicon Labs' VCP driver instead.")
    }

    private static func wch(_ pid: Int, _ key: String, _ name: String, _ chip: String) -> UsbDriverProfile {
        UsbDriverProfile(key: key, vendorId: 0x1A86, productId: pid, name: name, chip: chip, role: .serialBridge,
                         macosDriver: .vendorExtension, driverName: "WCH CH34x VCP driver",
                         notes: "Recent macOS releases may bind a built-in driver; otherwise install WCH's.")
    }

    private static func hpSync(_ pid: Int) -> UsbDriverProfile {
        UsbDriverProfile(key: String(format: "hp-usb-sync-%04x", pid), vendorId: 0x03F0, productId: pid,
                         name: String(format: "HP USB Sync (product %04x)", pid),
                         chip: "SA-1110 UDC or Pocket PC USB function", role: .winceUsbSync, macosDriver: .none,
                         driverName: syncDriver,
                         notes: "Listed in Microsoft's wceusbsh.inf and the Linux ipaq driver; the Jornada 720/728 "
                            + "cradle and the 540/560 Pocket PCs enumerate as one of these.")
    }

    private static func winceSync(_ vid: Int, _ pid: Int, _ key: String, _ name: String) -> UsbDriverProfile {
        UsbDriverProfile(key: key, vendorId: vid, productId: pid, name: name, chip: "Windows CE USB function",
                         role: .winceUsbSync, macosDriver: .none, driverName: syncDriver,
                         notes: "Listed in Microsoft's wceusbsh.inf and the Linux ipaq driver.")
    }

    public static let cdcAcmProfile = UsbDriverProfile(
        key: "usb-cdc-acm", vendorId: nil, productId: nil, name: "USB CDC-ACM serial function", chip: "CDC-ACM",
        role: .serialBridge, macosDriver: .builtIn, driverName: "AppleUSBACM",
        notes: "Class-compliant; matched by interface class, not vendor/product.")

    public static let drivers: [UsbDriverProfile] = [
        ftdi(0x6001, "ftdi-ft232r", "FTDI FT232R / FT232BM / FT245", "FT232R",
             "The adapter jornada-link was developed and tested with."),
        ftdi(0x6015, "ftdi-ft-x", "FTDI FT231X / FT230X / FT234XD", "FT-X"),
        ftdi(0x6010, "ftdi-ft2232", "FTDI FT2232C/D/H (two ports)", "FT2232"),
        ftdi(0x6011, "ftdi-ft4232h", "FTDI FT4232H (four ports)", "FT4232H"),
        ftdi(0x6014, "ftdi-ft232h", "FTDI FT232H", "FT232H"),
        prolific(0x2303, "prolific-pl2303", "Prolific PL2303 (HX/HXD/TA/TB and clones)", "PL2303",
                 "Prolific's current driver refuses counterfeit and end-of-life HXA/XA parts."),
        prolific(0x23A3, "prolific-pl2303gc", "Prolific PL2303GC", "PL2303GC"),
        prolific(0x23B3, "prolific-pl2303gb", "Prolific PL2303GB", "PL2303GB"),
        prolific(0x23C3, "prolific-pl2303gt", "Prolific PL2303GT", "PL2303GT"),
        prolific(0x23D3, "prolific-pl2303gl", "Prolific PL2303GL", "PL2303GL"),
        prolific(0x23E3, "prolific-pl2303ge", "Prolific PL2303GE", "PL2303GE"),
        prolific(0x23F3, "prolific-pl2303gs", "Prolific PL2303GS", "PL2303GS"),
        silabs(0xEA60, "silabs-cp210x", "Silicon Labs CP2102 / CP2102N / CP2104", "CP210x"),
        silabs(0xEA70, "silabs-cp2105", "Silicon Labs CP2105 (two ports)", "CP2105"),
        silabs(0xEA71, "silabs-cp2108", "Silicon Labs CP2108 (four ports)", "CP2108"),
        wch(0x7523, "wch-ch340", "WCH CH340 / CH340G / CH340C", "CH340"),
        wch(0x5523, "wch-ch341", "WCH CH341 (serial mode)", "CH341"),
        wch(0x55D4, "wch-ch9102", "WCH CH9102", "CH9102"),
        hpSync(0x1016), hpSync(0x1116), hpSync(0x1216),
        hpSync(0x2016), hpSync(0x2116), hpSync(0x2216),
        hpSync(0x3016), hpSync(0x3116), hpSync(0x3216),
        hpSync(0x4016), hpSync(0x4116), hpSync(0x4216),
        hpSync(0x5016), hpSync(0x5116), hpSync(0x5216),
        winceSync(0x049F, 0x0003, "compaq-ipaq-sync", "Compaq iPAQ USB Sync"),
        winceSync(0x045E, 0x00CE, "microsoft-wince-sync", "Microsoft Windows CE USB Sync"),
        winceSync(0x0BB4, 0x00CE, "htc-wince-sync", "HTC Windows CE USB Sync"),
    ]

    private static func sh3(_ key: String, _ name: String, _ os: String) -> HandheldModel {
        HandheldModel(key: key, name: name, cpu: "Hitachi SH7709A (SH-3) 133 MHz", architecture: .sh3, os: os,
                      usbClient: usbClientNone, dockUsb: false, notes: sh3Note)
    }

    private static func arm(_ key: String, _ name: String) -> HandheldModel {
        HandheldModel(key: key, name: name, cpu: "Intel StrongARM SA-1110 206 MHz", architecture: .arm,
                      os: "Windows CE 3.0 / H/PC 2000", usbClient: usbClientSA1110, dockUsb: true, notes: armNote)
    }

    public static let handhelds: [HandheldModel] = [
        sh3("jornada-680", "HP Jornada 680", "Windows CE 2.11 / H/PC Pro 3.0"),
        sh3("jornada-680e", "HP Jornada 680e", "Windows CE 2.11 / H/PC Pro 3.0"),
        sh3("jornada-690", "HP Jornada 690", "Windows CE 2.11 / H/PC Pro 3.01"),
        sh3("jornada-690e", "HP Jornada 690e", "Windows CE 2.11 / H/PC Pro 3.01"),
        arm("jornada-710", "HP Jornada 710"),
        arm("jornada-720", "HP Jornada 720"),
        arm("jornada-728", "HP Jornada 728"),
        sh3("sh3-hpc-pro-family", "SH3 Handheld PC Pro (Jornada 680/680e/690/690e)", "Windows CE 2.11 / H/PC Pro 3.x"),
        arm("sa1110-hpc2000-family", "StrongARM Handheld PC 2000 (Jornada 710/720/728)"),
    ]

    /// The concrete models a user can pick by hand (family rows are inferred only).
    public static var selectableHandhelds: [HandheldModel] {
        handhelds.filter { $0.key.hasPrefix("jornada-") }
    }

    private static let byVidPid: [String: UsbDriverProfile] = Dictionary(
        uniqueKeysWithValues: drivers.map { ("\($0.vendorId ?? -1):\($0.productId ?? -1)", $0) })
    private static let byKey: [String: HandheldModel] = Dictionary(uniqueKeysWithValues: handhelds.map { ($0.key, $0) })
    private static let modelNumbers = ["680e", "690e", "680", "690", "710", "720", "728"]

    /// Exact vendor/product match first, then a vendor-wide wildcard, else nil.
    public static func profile(vendorId: Int?, productId: Int?) -> UsbDriverProfile? {
        guard let vendorId else { return nil }
        return byVidPid["\(vendorId):\(productId ?? -1)"] ?? byVidPid["\(vendorId):-1"]
    }

    public static func hasDockMarker(_ strings: String?...) -> Bool {
        strings.contains { text in
            guard let upper = text?.uppercased() else { return false }
            return dockMarkers.contains { upper.contains($0) }
        }
    }

    /// Identify a USB device by vendor/product, falling back to a CDC-ACM interface
    /// (`interfaceClasses` are (bInterfaceClass, bInterfaceSubClass) pairs).
    public static func classify(vendorId: Int?, productId: Int?, product: String? = nil, serial: String? = nil,
                                interfaceClasses: [(Int, Int)] = []) -> UsbClassification? {
        var found = profile(vendorId: vendorId, productId: productId)
        if found == nil, interfaceClasses.contains(where: { $0.0 == usbClassCDC && $0.1 == usbSubclassACM }) {
            found = cdcAcmProfile
        }
        guard let profile = found else { return nil }
        var role = profile.role
        if role == .serialBridge, hasDockMarker(product, serial) { role = .dockBridge }
        return UsbClassification(profile: profile, role: role)
    }

    public static func handheld(_ key: String) -> HandheldModel? { byKey[key] }

    /// Best model for what the dccm handshake told us: a model number in the
    /// device name wins; otherwise the CPU architecture picks the family row.
    public static func identifyHandheld(hardware: String?, name: String?) -> HandheldModel? {
        let text = (name ?? "").lowercased()
        let squashed = text.replacingOccurrences(of: " ", with: "")
        for number in modelNumbers where text.contains(number) && squashed.contains("jornada") {
            return byKey["jornada-\(number)"]
        }
        for number in modelNumbers where text.hasSuffix(number) || text.contains("j\(number)") {
            return byKey["jornada-\(number)"]
        }
        let arch = (hardware ?? "").uppercased().replacingOccurrences(of: "-", with: "").replacingOccurrences(of: " ", with: "")
        if arch.contains("SH3") || arch.contains("SH7709") { return byKey["sh3-hpc-pro-family"] }
        if arch.contains("ARM") || arch.contains("SA1110") { return byKey["sa1110-hpc2000-family"] }
        return nil
    }

    /// The whole table as JSON (sorted keys; row order by key) for the parity check.
    public static func tableJSON() throws -> Data {
        struct Table: Encodable {
            let drivers: [UsbDriverProfile]
            let handhelds: [HandheldModel]
            let dockMarkers: [String]
            enum CodingKeys: String, CodingKey { case drivers, handhelds, dockMarkers = "dock_markers" }
        }
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        return try encoder.encode(Table(drivers: drivers.sorted { $0.key < $1.key },
                                        handhelds: handhelds.sorted { $0.key < $1.key },
                                        dockMarkers: dockMarkers))
    }
}
