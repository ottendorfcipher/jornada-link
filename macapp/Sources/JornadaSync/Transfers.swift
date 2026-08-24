import Foundation

/// One file transfer shown in the Transfers pane.
struct Transfer: Identifiable {
    enum Direction { case toDevice, fromDevice }
    enum State: Equatable {
        case running
        case done
        case failed(String)
    }

    let id = UUID()
    let name: String
    let direction: Direction
    let totalBytes: Int
    var movedBytes: Int = 0
    var state: State = .running
    let startedAt = Date()

    var fractionDone: Double {
        guard totalBytes > 0 else { return state == .done ? 1 : 0 }
        return Double(movedBytes) / Double(totalBytes)
    }

    var throughputText: String {
        let elapsed = max(Date().timeIntervalSince(startedAt), 0.001)
        let rate = Double(movedBytes) / elapsed
        return String(format: "%.1f KB/s", rate / 1024)
    }
}
