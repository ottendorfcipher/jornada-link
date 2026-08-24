import SwiftUI

struct TransfersView: View {
    @ObservedObject var model: AppModel

    var body: some View {
        Group {
            if model.transfers.isEmpty {
                ContentUnavailableView("No Transfers", systemImage: "arrow.up.arrow.down.circle",
                                       description: Text("Uploads and downloads appear here."))
            } else {
                List(model.transfers) { transfer in
                    HStack(spacing: 12) {
                        Image(systemName: transfer.direction == .toDevice
                              ? "arrow.up.circle.fill" : "arrow.down.circle.fill")
                            .font(.title2)
                            .foregroundStyle(color(for: transfer))
                        VStack(alignment: .leading, spacing: 3) {
                            Text(transfer.name).font(.headline)
                            switch transfer.state {
                            case .running:
                                ProgressView(value: transfer.fractionDone)
                                Text("\(transfer.movedBytes.formatted()) of \(transfer.totalBytes.formatted()) bytes — \(transfer.throughputText)")
                                    .font(.caption).foregroundStyle(.secondary)
                            case .done:
                                Text("Done — \(transfer.totalBytes.formatted()) bytes")
                                    .font(.caption).foregroundStyle(.secondary)
                            case .failed(let reason):
                                Text(reason).font(.caption).foregroundStyle(.red)
                            }
                        }
                        Spacer()
                        Text(transfer.direction == .toDevice ? "to device" : "to Mac")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    .padding(.vertical, 4)
                }
            }
        }
        .toolbar {
            ToolbarItem {
                Button("Clear") { model.transfers.removeAll { $0.state != .running } }
                    .disabled(model.transfers.allSatisfy { $0.state == .running })
            }
        }
    }

    private func color(for transfer: Transfer) -> Color {
        switch transfer.state {
        case .running: return .accentColor
        case .done: return .green
        case .failed: return .red
        }
    }
}
