import SwiftUI

/// The ActiveSync-style banner: logo, app name, connection status, and the
/// green "connected" orb with the device identity.
struct BannerView: View {
    @ObservedObject var model: AppModel

    private var statusLine: String {
        switch model.phase {
        case .down: return "Not connected"
        case .waitingForDevice: return "Waiting for the device — tap PC Link on the Jornada"
        case .pppUp: return "Serial link up — waiting for ActiveSync…"
        case .connected: return "Connected"
        }
    }

    private var orbColor: Color {
        switch model.phase {
        case .connected: return .green
        case .pppUp, .waitingForDevice: return .yellow
        case .down: return Color(nsColor: .quaternaryLabelColor)
        }
    }

    var body: some View {
        HStack(spacing: 14) {
            LogoView(diameter: 46, dimmed: model.phase != .connected)
                .shadow(color: .black.opacity(0.25), radius: 3, y: 1)
            VStack(alignment: .leading, spacing: 2) {
                Text("Jornada Sync")
                    .font(.system(size: 19, weight: .semibold, design: .rounded))
                Text(statusLine)
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            deviceChip
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
        .background(bannerBackground)
    }

    private var deviceChip: some View {
        HStack(spacing: 10) {
            Circle()
                .fill(orbColor)
                .frame(width: 11, height: 11)
                .shadow(color: orbColor.opacity(0.7), radius: model.phase == .connected ? 4 : 0)
            VStack(alignment: .trailing, spacing: 1) {
                Text(model.device?.name ?? "HP Jornada")
                    .font(.headline)
                Text(model.device.map { "\($0.osText) · \($0.hardware)" } ?? "no partnership")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 7)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 9, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 9, style: .continuous)
            .strokeBorder(.separator, lineWidth: 0.5))
    }

    private var bannerBackground: some View {
        LinearGradient(
            colors: model.phase == .connected
                ? [Color(red: 0.16, green: 0.48, blue: 0.32).opacity(0.28),
                   Color(red: 0.10, green: 0.35, blue: 0.24).opacity(0.10)]
                : [Color.secondary.opacity(0.12), Color.secondary.opacity(0.04)],
            startPoint: .top, endPoint: .bottom
        )
        .background(.ultraThinMaterial)
    }
}
