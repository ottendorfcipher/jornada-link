import Foundation
import IOKit

/// USB devices on this Mac, read from the I/O Registry — Swift twin of
/// `jornada/usb_registry.py` (which shells out to `ioreg`; here we use IOKit
/// directly, Apple frameworks only).
///
/// For every `IOUSBHostDevice` we walk the IOService plane below it, stopping
/// at nested devices (those behind a hub are reported on their own), to find
/// the interfaces and — for a USB-serial bridge — every `IOSerialBSDClient`
/// whose `IOCalloutDevice` is a `/dev/cu.*` node, together with the driver
/// that owns it and whether this user can open it.
public struct UsbSerialNode: Sendable, Equatable, Hashable {
    public let path: String
    public let driver: String
    public let writable: Bool

    public init(path: String, driver: String, writable: Bool) {
        self.path = path
        self.driver = driver
        self.writable = writable
    }
}

public struct UsbInterface: Sendable, Equatable {
    public let number: Int
    public let classCode: Int
    public let subclass: Int
    public let protocolCode: Int
    public let driver: String?

    public init(number: Int, classCode: Int, subclass: Int, protocolCode: Int, driver: String?) {
        self.number = number
        self.classCode = classCode
        self.subclass = subclass
        self.protocolCode = protocolCode
        self.driver = driver
    }
}

public struct UsbDevice: Sendable, Equatable, Identifiable {
    public let vendorId: Int
    public let productId: Int
    public let vendor: String
    public let product: String
    public let serial: String
    public let locationId: Int
    public let deviceClass: Int
    public let interfaces: [UsbInterface]
    public let serialNodes: [UsbSerialNode]

    public init(vendorId: Int, productId: Int, vendor: String, product: String, serial: String, locationId: Int,
                deviceClass: Int, interfaces: [UsbInterface], serialNodes: [UsbSerialNode]) {
        self.vendorId = vendorId
        self.productId = productId
        self.vendor = vendor
        self.product = product
        self.serial = serial
        self.locationId = locationId
        self.deviceClass = deviceClass
        self.interfaces = interfaces.sorted { $0.number < $1.number }
        self.serialNodes = serialNodes.sorted { $0.path < $1.path }
    }

    public var id: String { "\(locationId):\(vidPid):\(serial)" }
    public var vidPid: String { String(format: "%04x:%04x", vendorId, productId) }
    public var isHub: Bool {
        deviceClass == UsbProfiles.usbClassHub || interfaces.contains { $0.classCode == UsbProfiles.usbClassHub }
    }
    public var interfaceClasses: [(Int, Int)] { interfaces.map { ($0.classCode, $0.subclass) } }
    public var label: String {
        let parts = [vendor, product].map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
        return parts.isEmpty ? vidPid : parts.joined(separator: " ")
    }
}

public enum UsbRegistry {
    static let deviceClass = "IOUSBHostDevice"
    static let interfaceClass = "IOUSBHostInterface"
    static let serialClientClass = "IOSerialBSDClient"

    /// Every USB device (hubs included), in bus order.
    public static func devices() -> [UsbDevice] {
        var iterator: io_iterator_t = 0
        guard IOServiceGetMatchingServices(kIOMainPortDefault, IOServiceMatching(deviceClass), &iterator)
                == KERN_SUCCESS else { return [] }
        defer { IOObjectRelease(iterator) }
        var found: [UsbDevice] = []
        while true {
            let entry = IOIteratorNext(iterator)
            guard entry != 0 else { break }
            defer { IOObjectRelease(entry) }
            if let device = device(from: entry) { found.append(device) }
        }
        return found.sorted { ($0.locationId, $0.vidPid) < ($1.locationId, $1.vidPid) }
    }

    /// The devices that are not hubs (hubs never carry a link).
    public static func linkCandidates(_ devices: [UsbDevice]) -> [UsbDevice] {
        devices.filter { !$0.isHub }
    }

    static func properties(of entry: io_registry_entry_t) -> [String: Any] {
        var unmanaged: Unmanaged<CFMutableDictionary>?
        guard IORegistryEntryCreateCFProperties(entry, &unmanaged, kCFAllocatorDefault, 0) == KERN_SUCCESS,
              let dictionary = unmanaged?.takeRetainedValue() as? [String: Any] else { return [:] }
        return dictionary
    }

    static func text(_ props: [String: Any], _ keys: String...) -> String {
        for key in keys {
            if let value = props[key] as? String, !value.trimmingCharacters(in: .whitespaces).isEmpty {
                return value.trimmingCharacters(in: .whitespaces)
            }
        }
        return ""
    }

    static func int(_ props: [String: Any], _ key: String) -> Int {
        (props[key] as? Int) ?? (props[key] as? NSNumber)?.intValue ?? 0
    }

    static func driver(of props: [String: Any]) -> String? {
        let name = text(props, "CFBundleIdentifier", "IOUserServerName")
        return name.isEmpty ? nil : name
    }

    static func conforms(_ entry: io_object_t, _ className: String) -> Bool {
        IOObjectConformsTo(entry, className) != 0
    }

    static func device(from entry: io_registry_entry_t) -> UsbDevice? {
        let props = properties(of: entry)
        guard props["idVendor"] != nil, props["idProduct"] != nil else { return nil }
        var interfaces: [UsbInterface] = []
        var nodes: [UsbSerialNode] = []
        collect(below: entry, parentDriver: nil, interfaces: &interfaces, nodes: &nodes)
        return UsbDevice(
            vendorId: int(props, "idVendor"), productId: int(props, "idProduct"),
            vendor: text(props, "USB Vendor Name", "kUSBVendorString"),
            product: text(props, "USB Product Name", "kUSBProductString"),
            serial: text(props, "USB Serial Number", "kUSBSerialNumberString"),
            locationId: int(props, "locationID"), deviceClass: int(props, "bDeviceClass"),
            interfaces: interfaces, serialNodes: nodes)
    }

    /// Gather interfaces and tty nodes below a device, stopping at nested USB devices.
    static func collect(below parent: io_registry_entry_t, parentDriver: String?,
                        interfaces: inout [UsbInterface], nodes: inout [UsbSerialNode]) {
        var iterator: io_iterator_t = 0
        guard IORegistryEntryGetChildIterator(parent, kIOServicePlane, &iterator) == KERN_SUCCESS else { return }
        defer { IOObjectRelease(iterator) }
        while true {
            let child = IOIteratorNext(iterator)
            guard child != 0 else { break }
            defer { IOObjectRelease(child) }
            if conforms(child, deviceClass) { continue }
            let props = properties(of: child)
            let driver = self.driver(of: props) ?? parentDriver
            if conforms(child, interfaceClass) {
                interfaces.append(UsbInterface(
                    number: int(props, "bInterfaceNumber"), classCode: int(props, "bInterfaceClass"),
                    subclass: int(props, "bInterfaceSubClass"), protocolCode: int(props, "bInterfaceProtocol"),
                    driver: firstChildDriver(of: child)))
            } else if conforms(child, serialClientClass) {
                let path = text(props, "IOCalloutDevice")
                if !path.isEmpty {
                    nodes.append(UsbSerialNode(path: path, driver: parentDriver ?? "unknown",
                                               writable: access(path, R_OK | W_OK) == 0))
                }
            }
            collect(below: child, parentDriver: driver, interfaces: &interfaces, nodes: &nodes)
        }
    }

    static func firstChildDriver(of entry: io_registry_entry_t) -> String? {
        var iterator: io_iterator_t = 0
        guard IORegistryEntryGetChildIterator(entry, kIOServicePlane, &iterator) == KERN_SUCCESS else { return nil }
        defer { IOObjectRelease(iterator) }
        while true {
            let child = IOIteratorNext(iterator)
            guard child != 0 else { return nil }
            defer { IOObjectRelease(child) }
            if let name = driver(of: properties(of: child)) { return name }
        }
    }
}
