import SwiftUI

/// The classic ActiveSync main pane, modernized: connection state plus the
/// "sync items"-style detail rows (system, storage, power).
struct OverviewView: View {
    @ObservedObject var model: AppModel

    var body: some View {
        ScrollView {
            VStack(spacing: 16) {
                if model.phase != .connected { connectCard }
                detailCard
                if let error = model.lastError { errorCard(error) }
            }
            .padding(20)
            .frame(maxWidth: 640)
            .frame(maxWidth: .infinity)
        }
    }

    private var connectCard: some View {
        VStack(spacing: 12) {
            LogoView(diameter: 84, dimmed: true)
                .padding(.top, 6)
            Text(model.phase == .down ? "Set up a connection" : "Almost there")
                .font(.title3.weight(.semibold))
            VStack(alignment: .leading, spacing: 7) {
                stepRow(number: 1, text: "Plug the Jornada's sync cable into the USB-serial adapter",
                        done: true)
                stepRow(number: 2, text: "Click Connect in the toolbar (administrator prompt — pppd needs root)",
                        done: model.phase != .down)
                stepRow(number: 3, text: "On the Jornada: Start ▸ Programs ▸ Communication ▸ PC Link",
                        done: model.phase == .connected)
            }
            .padding(.horizontal, 8)
            if !model.devicePassword.isEmpty || model.phase != .connected {
                SecureField("Device password (only if the Jornada has one)", text: $model.devicePassword)
                    .textFieldStyle(.roundedBorder)
                    .frame(maxWidth: 380)
                    .padding(.top, 2)
            }
        }
        .padding(18)
        .frame(maxWidth: .infinity)
        .background(.quaternary.opacity(0.35), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
    }

    private func stepRow(number: Int, text: String, done: Bool) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 9) {
            Image(systemName: done ? "checkmark.circle.fill" : "\(number).circle")
                .foregroundStyle(done ? .green : .secondary)
            Text(text)
                .font(.callout)
                .foregroundStyle(done ? .secondary : .primary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var detailCard: some View {
        VStack(spacing: 0) {
            detailRow(symbol: "cpu", title: "System",
                      value: model.device != nil
                        ? "\(model.osVersionText) — \(model.device!.hardware) (\(model.device!.deviceClass))"
                        : "—",
                      status: model.phase == .connected ? "Synchronized" : "Not connected")
            Divider().padding(.leading, 44)
            detailRow(symbol: "internaldrive", title: "Object Store",
                      value: model.storageText,
                      status: model.storageFraction.map { "\(Int($0 * 100))% used" } ?? "",
                      progress: model.storageFraction)
            Divider().padding(.leading, 44)
            detailRow(symbol: model.onACPower ? "battery.100.bolt" : "battery.75",
                      title: "Power",
                      value: model.batteryText,
                      status: model.onACPower ? "AC" : "Battery",
                      progress: model.batteryPercent.map { Double($0) / 100 })
            Divider().padding(.leading, 44)
            HStack(spacing: 12) {
                Image(systemName: "clock.arrow.2.circlepath")
                    .font(.system(size: 17))
                    .foregroundStyle(Color(red: 0.16, green: 0.55, blue: 0.36))
                    .frame(width: 30)
                VStack(alignment: .leading, spacing: 3) {
                    Text("Clock").font(.headline)
                    Text("Sync the Jornada's date and time to this Mac")
                        .font(.callout).foregroundStyle(.secondary)
                }
                Spacer()
                Button("Set Clock") { model.setDeviceClock() }
                    .disabled(model.phase != .connected)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 11)
        }
        .background(.background.secondary, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous)
            .strokeBorder(.separator, lineWidth: 0.5))
    }

    private func detailRow(symbol: String, title: String, value: String,
                           status: String, progress: Double? = nil) -> some View {
        HStack(spacing: 12) {
            Image(systemName: symbol)
                .font(.system(size: 17))
                .foregroundStyle(Color(red: 0.16, green: 0.55, blue: 0.36))
                .frame(width: 30)
            VStack(alignment: .leading, spacing: 3) {
                Text(title).font(.headline)
                Text(value).font(.callout).foregroundStyle(.secondary)
                if let progress {
                    ProgressView(value: progress)
                        .tint(Color(red: 0.16, green: 0.55, blue: 0.36))
                        .frame(maxWidth: 260)
                }
            }
            Spacer()
            Text(status)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 11)
    }

    private func errorCard(_ text: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.yellow)
            Text(text).font(.callout)
            Spacer()
            Button("Dismiss") { model.lastError = nil }
                .buttonStyle(.link)
        }
        .padding(12)
        .background(.yellow.opacity(0.12), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
    }
}
