import CoreTransferable
import Foundation
import UniformTypeIdentifiers

extension UTType {
    /// Device entries being dragged inside the Files table (an app-private type).
    static let jornadaDeviceItems = UTType(exportedAs: "io.github.jornadalink.device-items")
}

/// Names of device entries being dragged, and the device folder they live in.
struct DeviceItemDrag: Codable, Transferable {
    let directory: String
    let names: [String]

    static var transferRepresentation: some TransferRepresentation {
        CodableRepresentation(contentType: .jornadaDeviceItems)
    }
}

/// Everything a folder row in the Files table can receive: files dragged in
/// from Finder (uploaded into that folder) and device entries dragged from the
/// table itself (moved into that folder). One drop type, one handler.
struct FilesDrop: Transferable {
    enum Kind {
        case file(URL)
        case device(DeviceItemDrag)
    }

    let kind: Kind

    static var transferRepresentation: some TransferRepresentation {
        ProxyRepresentation(importing: { (url: URL) in FilesDrop(kind: .file(url)) })
        ProxyRepresentation(importing: { (drag: DeviceItemDrag) in FilesDrop(kind: .device(drag)) })
    }

    static func split(_ items: [FilesDrop]) -> (files: [URL], drags: [DeviceItemDrag]) {
        var files: [URL] = []
        var drags: [DeviceItemDrag] = []
        for item in items {
            switch item.kind {
            case .file(let url): files.append(url)
            case .device(let drag): drags.append(drag)
            }
        }
        return (files, drags)
    }
}
