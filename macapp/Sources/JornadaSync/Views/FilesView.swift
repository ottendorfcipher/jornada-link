import JornadaCore
import SwiftUI
import UniformTypeIdentifiers

/// Device file browser: breadcrumbs, sortable table, upload/download,
/// drag-and-drop from Finder, context menu file management.
struct FilesView: View {
    @ObservedObject var model: AppModel
    @State private var selection = Set<RapiClient.FileEntry.ID>()
    @State private var showingImporter = false
    @State private var newFolderName = ""
    @State private var showingNewFolder = false
    @State private var renameTarget: RapiClient.FileEntry?
    @State private var renameText = ""

    var body: some View {
        VStack(spacing: 0) {
            pathBar
            Divider()
            table
        }
        .toolbar { toolbarContent }
        .fileImporter(isPresented: $showingImporter,
                      allowedContentTypes: [.item], allowsMultipleSelection: true) { result in
            if case .success(let urls) = result { model.upload(urls: urls) }
        }
        .alert("New Folder", isPresented: $showingNewFolder) {
            TextField("Name", text: $newFolderName)
            Button("Create") {
                if !newFolderName.isEmpty { model.createFolder(named: newFolderName) }
                newFolderName = ""
            }
            Button("Cancel", role: .cancel) { newFolderName = "" }
        }
        .alert("Rename", isPresented: Binding(get: { renameTarget != nil },
                                              set: { if !$0 { renameTarget = nil } })) {
            TextField("Name", text: $renameText)
            Button("Rename") {
                if let target = renameTarget, !renameText.isEmpty {
                    model.rename(target, to: renameText)
                }
                renameTarget = nil
            }
            Button("Cancel", role: .cancel) { renameTarget = nil }
        }
        .dropDestination(for: URL.self) { urls, _ in
            guard model.phase == .connected else { return false }
            model.upload(urls: urls)
            return true
        }
    }

    private var pathBar: some View {
        HStack(spacing: 6) {
            Button { model.goUp() } label: { Image(systemName: "chevron.up") }
                .disabled(model.currentPath == "\\")
                .help("Enclosing folder")
            let parts = model.currentPath.split(separator: "\\").map(String.init)
            Button("My Handheld") { Task { await model.loadDirectory("\\") } }
                .buttonStyle(.link)
            ForEach(Array(parts.enumerated()), id: \.offset) { index, part in
                Image(systemName: "chevron.right")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                Button(part) {
                    let path = "\\" + parts[0...index].joined(separator: "\\")
                    Task { await model.loadDirectory(path) }
                }
                .buttonStyle(.link)
            }
            Spacer()
            if model.listingBusy { ProgressView().controlSize(.small) }
            Text("\(model.entries.count) items")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 7)
        .background(.bar)
    }

    private var table: some View {
        Table(model.entries, selection: $selection) {
            TableColumn("Name") { entry in
                HStack(spacing: 7) {
                    Image(systemName: entry.isDirectory ? "folder.fill"
                          : entry.name.lowercased().hasSuffix(".exe") ? "gearshape.fill"
                          : "doc")
                        .foregroundStyle(entry.isDirectory
                                         ? Color(red: 0.20, green: 0.58, blue: 0.38) : .secondary)
                    Text(entry.name)
                    if entry.isInRom {
                        Text("ROM").font(.system(size: 9, weight: .bold))
                            .padding(.horizontal, 4).padding(.vertical, 1)
                            .background(.quaternary, in: Capsule())
                    }
                }
            }
            .width(min: 220)
            TableColumn("Size") { entry in
                Text(entry.isDirectory ? "—" : AppModel.bytes(UInt32(clamping: entry.size)))
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .trailing)
            }
            .width(90)
            TableColumn("Modified") { entry in
                Text(entry.modified.map {
                    DateFormatter.localizedString(from: $0, dateStyle: .medium, timeStyle: .short)
                } ?? "—")
                .foregroundStyle(.secondary)
            }
            .width(160)
        }
        .contextMenu(forSelectionType: RapiClient.FileEntry.ID.self) { ids in
            contextMenu(for: ids)
        } primaryAction: { ids in
            if let id = ids.first, let entry = model.entries.first(where: { $0.id == id }) {
                if entry.isDirectory { model.enter(entry) } else { saveAs(entry) }
            }
        }
        .onDeleteCommand {
            let selected = model.entries.filter { selection.contains($0.id) }
            guard !selected.isEmpty else { return }
            selected.forEach { model.delete($0) }
            selection.removeAll()
        }
        .overlay {
            if model.entries.isEmpty && !model.listingBusy {
                ContentUnavailableView(
                    model.phase == .connected ? "Empty Folder" : "Not Connected",
                    systemImage: model.phase == .connected ? "folder" : "cable.connector.slash",
                    description: Text(model.phase == .connected
                                      ? "Drop files here to copy them to the Jornada."
                                      : "Connect the device to browse its files.")
                )
            }
        }
    }

    @ViewBuilder
    private func contextMenu(for ids: Set<RapiClient.FileEntry.ID>) -> some View {
        let selected = model.entries.filter { ids.contains($0.id) }
        if let single = selected.first, selected.count == 1 {
            if single.isDirectory {
                Button("Open") { model.enter(single) }
            } else {
                Button("Download…") { saveAs(single) }
                if single.name.lowercased().hasSuffix(".exe") {
                    Button("Run on Device") { model.runOnDevice(single) }
                }
            }
            Button("Rename…") {
                renameText = single.name
                renameTarget = single
            }
            Divider()
            Button("Delete", role: .destructive) { model.delete(single) }
        } else if !selected.isEmpty {
            Button("Delete \(selected.count) Items", role: .destructive) {
                selected.forEach { model.delete($0) }
            }
        }
    }

    private func saveAs(_ entry: RapiClient.FileEntry) {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = entry.name
        panel.canCreateDirectories = true
        if panel.runModal() == .OK, let url = panel.url {
            model.download(entry, to: url)
        }
    }

    @ToolbarContentBuilder
    private var toolbarContent: some ToolbarContent {
        ToolbarItemGroup {
            Button {
                showingImporter = true
            } label: {
                Label("Send to Device", systemImage: "square.and.arrow.up.on.square")
            }
            .disabled(model.phase != .connected)
            .help("Copy Mac files to this folder on the Jornada")
            Button {
                showingNewFolder = true
            } label: {
                Label("New Folder", systemImage: "folder.badge.plus")
            }
            .disabled(model.phase != .connected)
            Button {
                Task { await model.loadDirectory(model.currentPath) }
            } label: {
                Label("Refresh", systemImage: "arrow.clockwise")
            }
            .disabled(model.phase != .connected)
        }
    }
}
