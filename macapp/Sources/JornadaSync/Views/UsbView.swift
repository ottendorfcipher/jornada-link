import JornadaCore
import SwiftUI

/// The USB pane: what is on the bus, which serial node the link will open, and
/// what the dock's USB jack can do for the handheld in it. Same restrained style
/// as the other panes — system colours, one default button.
struct UsbView: View {
    @ObservedObject var model: AppModel
    @StateObject private var usb = UsbController()

    var body: some View {
        VStack(spacing: 0) {
            statusBar
            Divider()
            ScrollView {
                VStack(spacing: 16) {
                    devicesCard
                    findingsCard
                    dockCard
                }
                .padding(20)
                .frame(maxWidth: 720)
                .frame(maxWidth: .infinity)
            }
        }
        .onAppear {
            usb.connectedHandheld = model.device.flatMap {
                UsbProfiles.identifyHandheld(hardware: $0.hardware, name: $0.name)
            }
            usb.startPolling()
        }
        .onDisappear { usb.stopPolling() }
        .onChange(of: model.device?.name) { _, _ in
            usb.connectedHandheld = model.device.flatMap {
                UsbProfiles.identifyHandheld(hardware: $0.hardware, name: $0.name)
            }
            usb.refresh()
        }
    }

    // MARK: - Status bar

    private var statusBar: some View {
        HStack(spacing: 10) {
            Circle()
                .fill(Self.colour(for: usb.diagnosis?.worstLevel ?? .info))
                .frame(width: 9, height: 9)
            Text(usb.summary)
                .font(.callout)
                .lineLimit(1)
            Spacer()
            Picker("Handheld", selection: $usb.selectedModelKey) {
                Text(usb.connectedHandheld.map { "Connected: \($0.name)" } ?? "Handheld: unknown").tag("")
                ForEach(UsbProfiles.selectableHandhelds) { handheld in
                    Text(handheld.name).tag(handheld.key)
                }
            }
            .frame(width: 250)
            .help("Which Jornada is docked — decides what the dock's USB jack can do")
            Button {
                usb.refresh()
            } label: {
                Label("Refresh", systemImage: "arrow.clockwise")
            }
            .help("Re-read the USB bus (it is also polled every few seconds while this pane is open)")
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 7)
        .background(.bar)
    }

    // MARK: - Devices

    private var devicesCard: some View {
        VStack(alignment: .leading, spacing: 0) {
            cardTitle("USB devices", symbol: "cable.connector")
            if let devices = usb.diagnosis?.devices, !devices.isEmpty {
                ForEach(devices) { item in
                    Divider().padding(.leading, 44)
                    deviceRow(item)
                }
            } else {
                Divider().padding(.leading, 44)
                Text("No USB devices attached (hubs are not listed).")
                    .font(.callout).foregroundStyle(.secondary)
                    .padding(.horizontal, 14).padding(.vertical, 11)
            }
        }
        .background(.background.secondary, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous).strokeBorder(.separator, lineWidth: 0.5))
    }

    private func deviceRow(_ item: UsbClassifiedDevice) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: Self.symbol(for: item.role))
                .font(.system(size: 17))
                .foregroundStyle(item.classification == nil ? Color.secondary : Color(red: 0.16, green: 0.55, blue: 0.36))
                .frame(width: 30)
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 8) {
                    Text(item.device.label).font(.headline)
                    Text(item.device.vidPid)
                        .font(.system(.caption, design: .monospaced))
                        .foregroundStyle(.secondary)
                    if let role = item.role { roleBadge(role) }
                }
                Text(item.classification.map { "\($0.profile.name) — \($0.profile.chip)" } ?? "Not a link device")
                    .font(.callout).foregroundStyle(.secondary)
                if !item.device.serial.isEmpty {
                    Text("Serial \(item.device.serial)").font(.caption).foregroundStyle(.secondary)
                }
                ForEach(item.device.serialNodes, id: \.path) { node in
                    nodeRow(node)
                }
            }
            Spacer()
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 11)
    }

    private func nodeRow(_ node: UsbSerialNode) -> some View {
        let selected = node.path == usb.diagnosis?.recommended
        let pinned = node.path == usb.diagnosis?.pinned
        return HStack(spacing: 8) {
            Image(systemName: selected ? "checkmark.circle.fill" : "circle")
                .foregroundStyle(selected ? Color.green : Color.secondary)
            Text(node.path).font(.system(.callout, design: .monospaced))
            Text(node.driver).font(.caption).foregroundStyle(.secondary)
            if !node.writable {
                Text("root-only").font(.caption).foregroundStyle(.orange)
                    .help("Not openable by this user: the root pppd can use it, jornada probe cannot")
            }
            Spacer()
            if pinned {
                Button("Unpin") { usb.unpin() }.buttonStyle(.link)
            } else if node.writable {
                Button("Use this port") { usb.pin(node.path) }.buttonStyle(.link)
            }
        }
        .padding(.top, 2)
    }

    private func roleBadge(_ role: UsbRole) -> some View {
        Text(Self.roleLabel(role))
            .font(.caption2.weight(.semibold))
            .padding(.horizontal, 6).padding(.vertical, 2)
            .background(.quaternary, in: Capsule())
    }

    // MARK: - Findings

    private var findingsCard: some View {
        VStack(alignment: .leading, spacing: 0) {
            cardTitle("Diagnosis", symbol: "stethoscope")
            if let findings = usb.diagnosis?.findings, !findings.isEmpty {
                ForEach(findings) { finding in
                    Divider().padding(.leading, 44)
                    HStack(alignment: .firstTextBaseline, spacing: 12) {
                        Image(systemName: Self.symbol(for: finding.level))
                            .foregroundStyle(Self.colour(for: finding.level))
                            .frame(width: 30)
                        VStack(alignment: .leading, spacing: 3) {
                            Text(finding.title).font(.headline)
                            Text(finding.detail).font(.callout).foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        Spacer()
                    }
                    .padding(.horizontal, 14).padding(.vertical, 11)
                }
            } else {
                Divider().padding(.leading, 44)
                Text(usb.lastError ?? "Reading the USB bus…")
                    .font(.callout).foregroundStyle(.secondary)
                    .padding(.horizontal, 14).padding(.vertical, 11)
            }
        }
        .background(.background.secondary, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous).strokeBorder(.separator, lineWidth: 0.5))
    }

    // MARK: - The dock

    private var dockCard: some View {
        VStack(alignment: .leading, spacing: 8) {
            cardTitle("The dock's USB jack", symbol: "dock.rectangle")
            Group {
                if let handheld = usb.effectiveHandheld {
                    Text("\(handheld.name) — \(handheld.cpu), \(handheld.os).").font(.callout)
                    Text(handheld.notes).font(.callout).foregroundStyle(.secondary)
                } else {
                    Text("The HP F1822A dock is passive: its DB-9 carries the handheld's RS-232 lines and its USB-B jack "
                         + "is wired straight to the connector's USB pins. Only a handheld with its own USB device "
                         + "controller (the StrongARM 710/720/728) drives those pins; the SH-3 680/680e/690/690e "
                         + "has none, so for them the jack is inert and the link runs over the DB-9 through a "
                         + "USB-serial adapter.")
                        .font(.callout).foregroundStyle(.secondary)
                }
                Text("Details, pinout and the dock-bridge retrofit: docs/usb-link.md in the repository.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            .fixedSize(horizontal: false, vertical: true)
            .padding(.horizontal, 14).padding(.bottom, 12)
        }
        .background(.background.secondary, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous).strokeBorder(.separator, lineWidth: 0.5))
    }

    // MARK: - Helpers

    private func cardTitle(_ title: String, symbol: String) -> some View {
        HStack(spacing: 12) {
            Image(systemName: symbol)
                .font(.system(size: 17))
                .foregroundStyle(Color(red: 0.16, green: 0.55, blue: 0.36))
                .frame(width: 30)
            Text(title).font(.headline)
            Spacer()
        }
        .padding(.horizontal, 14).padding(.vertical, 11)
    }

    static func colour(for level: UsbFindingLevel) -> Color {
        switch level {
        case .ok: return .green
        case .info: return .secondary
        case .warn: return .orange
        case .error: return .red
        }
    }

    static func symbol(for level: UsbFindingLevel) -> String {
        switch level {
        case .ok: return "checkmark.circle.fill"
        case .info: return "info.circle"
        case .warn: return "exclamationmark.triangle.fill"
        case .error: return "xmark.octagon.fill"
        }
    }

    static func symbol(for role: UsbRole?) -> String {
        switch role {
        case .serialBridge: return "cable.connector.horizontal"
        case .dockBridge: return "dock.rectangle"
        case .winceUsbSync: return "pc"
        case nil: return "questionmark.circle"
        }
    }

    static func roleLabel(_ role: UsbRole) -> String {
        switch role {
        case .serialBridge: return "USB–serial bridge"
        case .dockBridge: return "dock bridge"
        case .winceUsbSync: return "Windows CE USB Sync"
        }
    }
}

/// Owns the bus snapshot; IOKit reads happen off the main actor.
@MainActor
final class UsbController: ObservableObject {
    @Published var diagnosis: UsbDiagnosis?
    @Published var lastError: String?
    @Published var selectedModelKey = "" { didSet { refresh() } }
    @Published var connectedHandheld: HandheldModel?

    private var timer: Timer?
    private var busy = false

    var effectiveHandheld: HandheldModel? {
        selectedModelKey.isEmpty ? connectedHandheld : UsbProfiles.handheld(selectedModelKey)
    }

    var summary: String {
        guard let diagnosis else { return lastError ?? "reading the USB bus…" }
        if let path = diagnosis.recommended { return "serial link port \(path)" }
        return diagnosis.findings.first { $0.level == .error }?.title ?? "no serial link port"
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
        let handheld = effectiveHandheld
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

    func pin(_ path: String) {
        do {
            try UsbDoctor.writePin(path)
            refresh()
        } catch {
            lastError = "\(error)"
        }
    }

    func unpin() {
        UsbDoctor.clearPin()
        refresh()
    }
}
