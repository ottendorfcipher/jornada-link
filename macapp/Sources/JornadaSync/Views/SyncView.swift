import JornadaCore
import SwiftUI

/// The Sync pane: one card per built-in account in ActiveSync's "sync items"
/// style — target, direction, conflict preference, deletes, Preview / Sync Now,
/// and the plan's actions. System colours; the only emphasis is the green tint
/// the other panes use.
struct SyncView: View {
    @ObservedObject var model: AppModel
    @ObservedObject var sync: SyncModel

    var body: some View {
        ScrollView {
            VStack(spacing: 16) {
                noteCard
                ForEach(SyncModel.Account.allCases) { account in
                    SyncAccountCard(account: account, model: model, sync: sync)
                }
            }
            .padding(20)
            .frame(maxWidth: 680)
            .frame(maxWidth: .infinity)
        }
        .toolbar {
            ToolbarItem {
                Button {
                    Task { await sync.refresh(connected: model.phase == .connected) }
                } label: {
                    Label("Refresh", systemImage: "arrow.clockwise")
                }
                .help("Re-read the calendars, lists and groups, and the device record counts")
            }
        }
        .task { await sync.refresh(connected: model.phase == .connected) }
        .onChange(of: model.phase) { _, phase in
            if phase == .connected { Task { await sync.refreshDeviceCounts() } }
        }
    }

    private var noteCard: some View {
        VStack(alignment: .leading, spacing: 6) {
            Label("Sync with this Mac's Calendar, Reminders and Contacts", systemImage: "arrow.2.squarepath")
                .font(.headline)
            Text("The Jornada is the other side of a trust boundary: every record it sends is decoded and " +
                 "validated before it is used, and only the calendar, list or group chosen below is ever touched " +
                 "on this Mac. Before the first write of a session a JSON snapshot of the device database is saved " +
                 "under Documents ▸ Jornada Backup ▸ PIM Snapshots, so a sync is never the only copy. Records are " +
                 "paired by content the first time and by a link file afterwards (~/.jornada-link/sync/state), " +
                 "the same file the jornada command-line tool uses.")
                .font(.callout)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.quaternary.opacity(0.35), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
    }
}

/// One account: header (name, device count, last sync), target and options, buttons, plan.
private struct SyncAccountCard: View {
    let account: SyncModel.Account
    @ObservedObject var model: AppModel
    @ObservedObject var sync: SyncModel
    @State private var showActions = false

    private static let tint = Color(red: 0.16, green: 0.55, blue: 0.36)

    private var status: SyncModel.Status { sync.status(for: account) }
    private var settings: SyncModel.Settings { sync.settings(for: account) }
    private var connected: Bool { model.phase == .connected }
    private var canRun: Bool { connected && !status.busy && status.access != false && settings.target != nil }

    var body: some View {
        VStack(spacing: 0) {
            header
                .padding(.horizontal, 14)
                .padding(.vertical, 11)
            Divider().padding(.leading, 44)
            controls
                .padding(.horizontal, 14)
                .padding(.vertical, 11)
            if let plan = status.plan {
                Divider().padding(.leading, 44)
                planSection(plan)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 9)
            }
            if let error = status.error {
                Divider().padding(.leading, 44)
                errorRow(error)
            }
        }
        .background(.background.secondary, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous)
            .strokeBorder(.separator, lineWidth: 0.5))
    }

    // MARK: - Header

    private var header: some View {
        HStack(spacing: 12) {
            Image(systemName: account.symbol)
                .font(.system(size: 17))
                .foregroundStyle(Self.tint)
                .frame(width: 30)
            VStack(alignment: .leading, spacing: 3) {
                Text(account.rawValue).font(.headline)
                Text(detailText).font(.callout).foregroundStyle(.secondary)
            }
            Spacer()
            if status.busy {
                ProgressView().controlSize(.small)
            } else {
                Text(status.summary ?? (status.lastSync.map { "Last sync \($0)" } ?? "Never synced"))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.trailing)
            }
        }
    }

    private var detailText: String {
        let device = status.deviceCount.map { "\($0) \(account.deviceNoun) on the Jornada" }
            ?? (connected ? "Reading the device…" : "Jornada not connected")
        let last = status.lastSync.map { " · last sync \($0)" } ?? ""
        return device + last
    }

    // MARK: - Controls

    private var controls: some View {
        VStack(alignment: .leading, spacing: 10) {
            targetRow
            HStack(spacing: 14) {
                Picker("Direction", selection: directionBinding) {
                    Text("Both ways").tag(SyncDirection.both)
                    Text("To Jornada").tag(SyncDirection.toDevice)
                    Text("From Jornada").tag(SyncDirection.fromDevice)
                }
                .pickerStyle(.segmented)
                .frame(maxWidth: 360)
                Picker("If both changed", selection: preferBinding) {
                    Text("Mac wins").tag(SyncPrefer.remote)
                    Text("Jornada wins").tag(SyncPrefer.local)
                }
                .frame(maxWidth: 220)
            }
            HStack {
                Toggle("Propagate deletions", isOn: deletesBinding)
                    .toggleStyle(.checkbox)
                    .help("Off: a record deleted on one side is unlinked, the other copy is kept")
                Spacer()
                Button("Preview") { sync.preview(account) }
                    .disabled(!canRun)
                    .help("Show what a sync would do without changing anything")
                Button("Sync Now") { sync.sync(account) }
                    .disabled(!canRun)
            }
        }
    }

    @ViewBuilder
    private var targetRow: some View {
        if status.access == false {
            HStack(spacing: 8) {
                Image(systemName: "lock").foregroundStyle(.secondary)
                Text("\(account.rawValue) access is off for Jornada Sync — allow it under System Settings ▸ Privacy & Security.")
                    .font(.callout)
                Spacer()
            }
        } else if status.access == nil {
            HStack(spacing: 8) {
                Text("Jornada Sync needs access to \(account.rawValue) to choose a \(account.targetNoun).")
                    .font(.callout)
                Spacer()
                Button("Allow Access…") { Task { await sync.requestAccess(account) } }
            }
        } else {
            Picker(account.targetNoun.capitalized, selection: targetBinding) {
                ForEach(status.targets) { target in
                    Text(target.detail.isEmpty ? target.title : "\(target.title) — \(target.detail)").tag(Optional(target.id))
                }
            }
            .frame(maxWidth: 420)
        }
    }

    private var targetBinding: Binding<String?> {
        Binding(get: { settings.target }, set: { sync.setTarget($0, for: account) })
    }

    private var directionBinding: Binding<SyncDirection> {
        Binding(get: { settings.direction }, set: { sync.setDirection($0, for: account) })
    }

    private var preferBinding: Binding<SyncPrefer> {
        Binding(get: { settings.prefer }, set: { sync.setPrefer($0, for: account) })
    }

    private var deletesBinding: Binding<Bool> {
        Binding(get: { settings.propagateDeletes }, set: { sync.setPropagateDeletes($0, for: account) })
    }

    // MARK: - Plan and errors

    private func planSection(_ plan: SyncPlan) -> some View {
        DisclosureGroup(isExpanded: $showActions) {
            VStack(alignment: .leading, spacing: 2) {
                if plan.actions.isEmpty {
                    Text("Nothing to do — both sides agree.").font(.callout).foregroundStyle(.secondary)
                }
                ForEach(Array(plan.actions.enumerated()), id: \.offset) { _, action in
                    Text(action.describe())
                        .font(.system(.caption, design: .monospaced))
                        .textSelection(.enabled)
                }
                ForEach(Array(status.errors.enumerated()), id: \.offset) { _, error in
                    Label(error, systemImage: "exclamationmark.triangle.fill")
                        .font(.caption)
                        .foregroundStyle(.orange)
                        .textSelection(.enabled)
                }
            }
            .padding(.top, 4)
        } label: {
            Text("\(status.planIsPreview ? "Preview" : "Last sync"): \(plan.summary())"
                 + (status.errors.isEmpty ? "" : " — \(status.errors.count) error(s)"))
                .font(.callout)
        }
    }

    private func errorRow(_ text: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.system(size: 17))
                .foregroundStyle(.orange)
                .frame(width: 30)
            Text(text).font(.callout).fixedSize(horizontal: false, vertical: true)
            Spacer()
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 11)
    }
}
