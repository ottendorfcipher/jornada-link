import JornadaCore
import SwiftUI

/// The USB/Serial pane: the status of the physical link, auto-detected.
/// Which adapter is attached, which serial port the link will open, and how
/// far the connection has come — nothing to configure. Problems the doctor
/// finds surface as a single guidance line; the port is chosen automatically
/// (`jornada usb pin` exists for the rare manual override).
struct UsbView: View {
    @ObservedObject var model: AppModel
    @StateObject private var usb = UsbController()

    var body: some View {
        ScrollView {
            VStack(spacing: 16) {
                statusCard
            }
            .padding(20)
            .frame(maxWidth: 640)
            .frame(maxWidth: .infinity)
        }
        .toolbar {
            ToolbarItem {
                Button {
                    usb.refresh()
                } label: {
                    Label("Refresh", systemImage: "arrow.clockwise")
                }
                .help("Re-read the USB bus now (it is polled every few seconds while this pane is open)")
            }
        }
        .onAppear {
            usb.connectedHandheld = Self.handheld(for: model.device)
            usb.startPolling()
        }
        .onDisappear { usb.stopPolling() }
        .onChange(of: model.device?.name) { _, _ in
            usb.connectedHandheld = Self.handheld(for: model.device)
            usb.refresh()
        }
    }

    private static func handheld(for device: DccmListener.DeviceInfo?) -> HandheldModel? {
        device.flatMap { UsbProfiles.identifyHandheld(hardware: $0.hardware, name: $0.name) }
    }

    // MARK: - The one card

    private var statusCard: some View {
        VStack(spacing: 0) {
            row(symbol: "cable.connector", title: "Adapter",
                value: adapterText, status: adapterStatus)
            Divider().padding(.leading, 44)
            row(symbol: "terminal", title: "Port",
                value: portText, status: portStatus)
            Divider().padding(.leading, 44)
            row(symbol: linkSymbol, title: "Link",
                value: linkText, status: linkStatus)
            if let guidance = usb.guidance {
                Divider().padding(.leading, 44)
                HStack(alignment: .firstTextBaseline, spacing: 12) {
                    Image(systemName: guidance.level == .error ? "xmark.octagon.fill" : "exclamationmark.triangle.fill")
                        .font(.system(size: 17))
                        .foregroundStyle(guidance.level == .error ? Color.red : Color.orange)
                        .frame(width: 30)
                    VStack(alignment: .leading, spacing: 3) {
                        Text(guidance.title).font(.headline)
                        Text(guidance.detail).font(.callout).foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Spacer()
                }
                .padding(.horizontal, 14).padding(.vertical, 11)
            }
        }
        .background(.background.secondary, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous)
            .strokeBorder(.separator, lineWidth: 0.5))
    }

    private func row(symbol: String, title: String, value: String, status: String) -> some View {
        HStack(spacing: 12) {
            Image(systemName: symbol)
                .font(.system(size: 17))
                .foregroundStyle(Color(red: 0.16, green: 0.55, blue: 0.36))
                .frame(width: 30)
            VStack(alignment: .leading, spacing: 3) {
                Text(title).font(.headline)
                Text(value).font(.callout).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer()
            Text(status).font(.caption).foregroundStyle(.secondary)
        }
        .padding(.horizontal, 14).padding(.vertical, 11)
    }

    // MARK: - Detected values

    private var adapterText: String {
        guard let diagnosis = usb.diagnosis else { return usb.lastError ?? "Reading the USB bus…" }
        guard let bridge = diagnosis.devices.first(where: { $0.classification != nil }) else {
            return "No USB-serial adapter attached"
        }
        let profile = bridge.classification!.profile
        return "\(bridge.device.label) — \(profile.chip)"
    }

    private var adapterStatus: String {
        guard let diagnosis = usb.diagnosis else { return "" }
        return diagnosis.devices.contains { $0.classification != nil } ? "Detected" : "Missing"
    }

    private var portText: String {
        guard let diagnosis = usb.diagnosis else { return "—" }
        guard let path = diagnosis.recommended else { return "No serial port available" }
        let driver = diagnosis.candidates.first { $0.path == path }?.driver ?? ""
        return driver.isEmpty ? path : "\(path)  (\(driver))"
    }

    private var portStatus: String {
        guard let diagnosis = usb.diagnosis, let path = diagnosis.recommended else { return "" }
        if diagnosis.pinned == path { return "Pinned" }
        let openable = diagnosis.candidates.first { $0.path == path }?.writable ?? true
        return openable ? "Auto-selected" : "Root-only"
    }

    private var linkSymbol: String {
        model.phase == .connected ? "bolt.horizontal.circle.fill" : "bolt.horizontal.circle"
    }

    private var linkText: String {
        switch model.phase {
        case .down: return "Not connected — click Connect, then PC Link on the Jornada"
        case .waitingForDevice: return "Waiting for the Jornada to dial in (PC Link)"
        case .pppUp: return "Serial link up — waiting for ActiveSync"
        case .connected: return "Connected to \(model.device?.name ?? "the device")"
        }
    }

    private var linkStatus: String {
        switch model.phase {
        case .down: return "Down"
        case .waitingForDevice: return "Waiting"
        case .pppUp: return "PPP up"
        case .connected: return "Connected"
        }
    }
}

/// Owns the bus snapshot; IOKit reads happen off the main actor.
@MainActor
final class UsbController: ObservableObject {
    @Published var diagnosis: UsbDiagnosis?
    @Published var lastError: String?
    @Published var connectedHandheld: HandheldModel? { didSet { refresh() } }

    private var timer: Timer?
    private var busy = false

    /// The single thing worth telling the user: the doctor's worst finding, and
    /// only when it is an actual problem.
    var guidance: UsbFinding? {
        guard let diagnosis else { return nil }
        return diagnosis.findings
            .filter { $0.level >= .warn }
            .max(by: { $0.level < $1.level })
    }

    func startPolling() {
        refresh()
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: 3.0, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refresh() }
        }
    }

    func stopPolling() {
        timer?.invalidate()
        timer = nil
    }

    func refresh() {
        guard !busy else { return }
        busy = true
        let handheld = connectedHandheld
        Task.detached(priority: .utility) {
            let snapshot = UsbRegistry.devices()
            let pinned = UsbDoctor.readPin()
            let result = UsbDoctor.diagnose(snapshot, pinned: pinned, handheld: handheld)
            await MainActor.run {
                self.diagnosis = result
                self.lastError = nil
                self.busy = false
            }
        }
    }
}
