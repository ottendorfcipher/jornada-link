import Foundation
import JornadaCore
import SwiftUI

@MainActor
final class AppModel: ObservableObject {
    enum Pane: String, CaseIterable, Identifiable {
        case overview = "Overview"
        case files = "Files"
        case transfers = "Transfers"
        case logs = "Log"
        var id: String { rawValue }
        var symbol: String {
            switch self {
            case .overview: return "arrow.triangle.2.circlepath"
            case .files: return "folder"
            case .transfers: return "arrow.up.arrow.down.circle"
            case .logs: return "text.alignleft"
            }
        }
    }

    enum LinkPhase: Equatable {
        case down
        case waitingForDevice     // ppp wrapper running or ppp0 absent but listener up
        case pppUp                // interface exists, no ActiveSync session yet
        case connected            // device handshaked on 5679
    }

    // Published state ---------------------------------------------------------
    @Published var pane: Pane = .overview
    @Published var phase: LinkPhase = .down
    @Published var device: DccmListener.DeviceInfo?
    @Published var listenerNote = "starting…"
    @Published var companionMode = false

    @Published var osVersionText = "—"
    @Published var storageText = "—"
    @Published var storageFraction: Double?
    @Published var batteryText = "—"
    @Published var batteryPercent: Int?
    @Published var onACPower = false

    @Published var currentPath = "\\"
    @Published var entries: [RapiClient.FileEntry] = []
    @Published var listingBusy = false
    @Published var lastError: String?

    @Published var transfers: [Transfer] = []
    @Published var logLines: [String] = []
    @Published var devicePassword = ""

    let rapi = RapiService()
    private var listener: DccmListener?
    private var pollTimer: Timer?
    private var pollCount = 0

    // Lifecycle ---------------------------------------------------------------
    func start() {
        guard listener == nil else { return }
        log("Jornada Sync started")
        startListener()
        pollTimer = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.pollLinkState() }
        }
        pollLinkState()
    }

    private func startListener() {
        let fresh = DccmListener { [weak self] event in
            Task { @MainActor in self?.handle(event) }
        }
        fresh.passwordProvider = { [weak self] in
            DispatchQueue.main.sync { self?.devicePassword.isEmpty == false ? self?.devicePassword : nil }
        }
        listener = fresh
        fresh.start()
    }

    private func handle(_ event: DccmListener.Event) {
        switch event {
        case .listening(let port):
            listenerNote = "listening on port \(port)"
            companionMode = false
        case .portBusy:
            listenerNote = "port 5679 in use — following the CLI listener"
            companionMode = true
            log("dccm port busy; running in companion mode (reading ~/.jornada-link/connection.json)")
        case .deviceConnected(let info):
            device = info
            phase = .connected
            syncGaveUp = false   // fresh session, fresh chance
            log("device connected: \(info.name) (\(info.hardware), \(info.osText)) at \(info.ip)")
            rapi.configure(host: info.ip, password: devicePassword.isEmpty ? nil : devicePassword,
                           passwordKey: info.passwordKey ?? 0)
            Task { await initialSync() }
        case .deviceDisconnected(let ip):
            log("device at \(ip) disconnected")
            device = nil
            syncGaveUp = false
            entries = []
            osVersionText = "—"
            rapi.disconnect()
            pollLinkState()
        case .passwordRequired:
            log("the device requires a password — set it in the banner field and reconnect")
        case .passwordRejected:
            log("the device rejected the password")
        case .log(let line):
            log(line)
        }
    }

    private func pollLinkState() {
        pollCount += 1
        let up = PppController.linkIsUp()
        let engine = PppController.engineRunning()
        if device != nil {
            phase = .connected
        } else if up {
            phase = .pppUp
        } else if engine {
            phase = .waitingForDevice
        } else {
            phase = .down
        }
        if companionMode {
            readCompanionState()
            // The CLI listener may have gone away; try to take over port 5679.
            if pollCount % 5 == 0 && device == nil {
                listener?.stop()
                listener = nil
                startListener()
            }
        }
        // Safety net: connected to a device but the listing is empty (a missed or
        // failed initial fetch) — resync, at most every 16s, and never once the
        // session has given up (initialSync's own latch enforces that too).
        if phase == .connected, device != nil, entries.isEmpty, !listingBusy,
           !isSyncing, !syncGaveUp, pollCount % 8 == 0 {
            Task { await initialSync() }
        }
    }

    private func readCompanionState() {
        let url = PppController.stateDirectory().appendingPathComponent("connection.json")
        guard let data = try? Data(contentsOf: url),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            if device != nil, PppController.linkIsUp() == false {
                device = nil
                phase = .down
            }
            return
        }
        guard device == nil else { return }
        let deviceDict = json["device"] as? [String: Any]
        let info = DccmListener.DeviceInfo(
            name: deviceDict?["name"] as? String ?? "Windows CE device",
            deviceClass: deviceDict?["device_class"] as? String ?? "?",
            hardware: deviceDict?["hardware"] as? String ?? "?",
            osMajor: (deviceDict?["os_version"] as? Int ?? 0) & 0xFF,
            osMinor: (deviceDict?["os_version"] as? Int ?? 0) >> 8,
            buildNumber: deviceDict?["build_number"] as? Int ?? 0,
            ip: json["ip"] as? String ?? PppController.deviceIp,
            passwordKey: nil
        )
        handle(.deviceConnected(info))
    }

    // Link control ------------------------------------------------------------
    func connectLink() {
        Task {
            do {
                try await Task.detached { try PppController.startLink() }.value
                log("PPP engine started (device \(PppController.serialDevice() ?? "?") @ \(PppController.baud())) — now tap PC Link on the Jornada")
                pollLinkState()
            } catch {
                lastError = "\(error)"
                log("could not start the PPP link: \(error)")
            }
        }
    }

    func disconnectLink() {
        Task {
            do {
                try await Task.detached { try PppController.stopLink() }.value
                log("PPP link stopped")
                device = nil
                rapi.disconnect()
                pollLinkState()
            } catch {
                lastError = "\(error)"
            }
        }
    }

    private func explainRapiFailure(_ error: Error, context: String) {
        let text = "\(error)"
        if text.contains("timed out") || text.contains("timeout") {
            lastError = "The Jornada's file service (RAPI, port 990) is not answering. " +
                "Disconnect PC Link on the device and tap it again; if it still fails, " +
                "soft-reset the Jornada (recessed Reset button) and re-tap PC Link."
        } else {
            lastError = text
        }
        log("\(context) failed: \(text)")
    }

    // Device info -------------------------------------------------------------
    /// Fetch device info + the current directory right after a connection, with
    /// retries: the Jornada's RAPI service (port 990) is often not ready the
    /// instant the dccm handshake completes, and the link may still be settling
    /// after an auto-reconnect. Without this, a transient first-call failure
    /// left the UI connected-but-blank.
    private var isSyncing = false
    /// Set when a session's sync attempts are exhausted; cleared by the next
    /// connect/disconnect. Prevents the poll loop from retrying forever —
    /// sustained retry pressure is what wedges the device's rapisrv.
    private var syncGaveUp = false

    func initialSync() async {
        guard !isSyncing, !syncGaveUp else { return }
        isSyncing = true
        defer { isSyncing = false }
        let backoffSeconds: [UInt64] = [2, 5, 10]
        for (index, delay) in backoffSeconds.enumerated() {
            let infoOK = await refreshDeviceInfo()
            // Only probe the listing once the info calls prove RAPI is alive:
            // a dead port 990 then costs one connection per attempt, not two.
            let filesOK = infoOK ? await loadDirectory(currentPath) : false
            if infoOK && filesOK {
                lastError = nil
                return
            }
            if device == nil { return }   // device went away; stop retrying
            if index < backoffSeconds.count - 1 {
                log("device file service not ready — next attempt in \(delay)s…")
                try? await Task.sleep(nanoseconds: delay * 1_000_000_000)
            }
        }
        syncGaveUp = true
        log("pausing automatic sync for this session — the device's file service isn't answering")
        lastError = "The Jornada's file service (port 990) is not answering. " +
            "Give it a quiet minute, then reconnect PC Link on the device; " +
            "if it still fails, soft-reset the Jornada and reconnect."
    }

    @discardableResult
    func refreshDeviceInfo() async -> Bool {
        do {
            let version = try await rapi.run("version") { try $0.version() }
            osVersionText = "Windows CE \(version.major).\(String(format: "%02d", version.minor)) (build \(version.build))"
            let store = try await rapi.run("store") { try $0.storeInformation() }
            let free = Double(store.freeSize), total = Double(store.storeSize)
            storageText = "\(Self.bytes(store.freeSize)) free of \(Self.bytes(store.storeSize))"
            storageFraction = total > 0 ? (total - free) / total : nil
            let power = try await rapi.run("power") { try $0.powerStatus() }
            onACPower = power.acLine == 1
            if power.batteryPercent <= 100 {
                batteryPercent = Int(power.batteryPercent)
                batteryText = "\(power.batteryPercent)%\(onACPower ? " (on AC power)" : "")"
            } else {
                batteryPercent = nil
                batteryText = onACPower ? "on AC power" : "unknown"
            }
            return true
        } catch {
            explainRapiFailure(error, context: "device info")
            return false
        }
    }

    func setDeviceClock() {
        Task {
            do {
                try await rapi.run("settime") { try $0.syncTimeFromMac() }
                log("device clock set from this Mac")
            } catch {
                explainRapiFailure(error, context: "set clock")
            }
        }
    }

    // Files -------------------------------------------------------------------
    @discardableResult
    func loadDirectory(_ path: String) async -> Bool {
        listingBusy = true
        defer { listingBusy = false }
        do {
            let listing = try await rapi.run("ls") { try $0.listDirectory(path) }
            currentPath = path
            entries = listing.sorted {
                if $0.isDirectory != $1.isDirectory { return $0.isDirectory }
                return $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending
            }
            lastError = nil
            return true
        } catch {
            explainRapiFailure(error, context: "list \(path)")
            return false
        }
    }

    func enter(_ entry: RapiClient.FileEntry) {
        guard entry.isDirectory else { return }
        Task { await loadDirectory(Self.join(currentPath, entry.name)) }
    }

    func goUp() {
        guard currentPath != "\\" else { return }
        let trimmed = currentPath.hasSuffix("\\") ? String(currentPath.dropLast()) : currentPath
        let parent = trimmed.contains("\\") ? String(trimmed[..<trimmed.lastIndex(of: "\\")!]) : ""
        Task { await loadDirectory(parent.isEmpty ? "\\" : parent) }
    }

    // Transfers ---------------------------------------------------------------
    func upload(urls: [URL]) {
        for url in urls { uploadOne(url) }
    }

    private func uploadOne(_ url: URL) {
        let destination = Self.join(currentPath, url.lastPathComponent)
        guard let data = try? Data(contentsOf: url) else {
            log("cannot read \(url.path)")
            return
        }
        let transfer = Transfer(name: url.lastPathComponent, direction: .toDevice, totalBytes: data.count)
        transfers.insert(transfer, at: 0)
        let transferId = transfer.id
        Task {
            do {
                try await rapi.run("put") { client in
                    try client.upload(destination, data: data) { moved, _ in
                        Task { @MainActor in self.updateTransfer(transferId, moved: moved) }
                    }
                }
                finishTransfer(transferId, state: .done)
                log("uploaded \(url.lastPathComponent) → \(destination) (\(Self.bytes(UInt32(clamping: data.count))))")
                await loadDirectory(currentPath)
            } catch {
                finishTransfer(transferId, state: .failed("\(error)"))
                log("upload \(url.lastPathComponent) failed: \(error)")
            }
        }
    }

    func download(_ entry: RapiClient.FileEntry, to localUrl: URL) {
        let source = Self.join(currentPath, entry.name)
        let transfer = Transfer(name: entry.name, direction: .fromDevice, totalBytes: Int(entry.size))
        transfers.insert(transfer, at: 0)
        let transferId = transfer.id
        Task {
            do {
                let collected = try await rapi.run("get") { client in
                    var buffer = Data()
                    _ = try client.download(source) { chunk in
                        buffer.append(chunk)
                        let moved = buffer.count
                        Task { @MainActor in self.updateTransfer(transferId, moved: moved) }
                    }
                    return buffer
                }
                try collected.write(to: localUrl)
                finishTransfer(transferId, state: .done)
                log("downloaded \(source) → \(localUrl.path)")
            } catch {
                finishTransfer(transferId, state: .failed("\(error)"))
                log("download \(entry.name) failed: \(error)")
            }
        }
    }

    private func updateTransfer(_ id: UUID, moved: Int) {
        guard let index = transfers.firstIndex(where: { $0.id == id }) else { return }
        transfers[index].movedBytes = moved
    }

    private func finishTransfer(_ id: UUID, state: Transfer.State) {
        guard let index = transfers.firstIndex(where: { $0.id == id }) else { return }
        transfers[index].state = state
        if state == .done { transfers[index].movedBytes = transfers[index].totalBytes }
    }

    // File management ---------------------------------------------------------
    func delete(_ entry: RapiClient.FileEntry) {
        let path = Self.join(currentPath, entry.name)
        Task {
            do {
                if entry.isDirectory {
                    try await rapi.run("rmdir") { try $0.removeDirectory(path) }
                } else {
                    try await rapi.run("rm") { try $0.deleteFile(path) }
                }
                log("deleted \(path)")
                await loadDirectory(currentPath)
            } catch {
                lastError = "\(error)"
                log("delete \(path) failed: \(error)")
            }
        }
    }

    func createFolder(named name: String) {
        let path = Self.join(currentPath, name)
        Task {
            do {
                try await rapi.run("mkdir") { try $0.createDirectory(path) }
                await loadDirectory(currentPath)
            } catch {
                lastError = "\(error)"
                log("mkdir \(path) failed: \(error)")
            }
        }
    }

    func rename(_ entry: RapiClient.FileEntry, to newName: String) {
        let from = Self.join(currentPath, entry.name)
        let to = Self.join(currentPath, newName)
        Task {
            do {
                try await rapi.run("mv") { try $0.moveFile(from: from, to: to) }
                await loadDirectory(currentPath)
            } catch {
                lastError = "\(error)"
                log("rename failed: \(error)")
            }
        }
    }

    func runOnDevice(_ entry: RapiClient.FileEntry) {
        let path = Self.join(currentPath, entry.name)
        Task {
            do {
                let pid = try await rapi.run("run") { try $0.createProcess(path) }
                log("started \(path) on the device (pid \(pid))")
            } catch {
                lastError = "\(error)"
                log("run \(path) failed: \(error)")
            }
        }
    }

    // Helpers -----------------------------------------------------------------
    func log(_ line: String) {
        let stamp = DateFormatter.localizedString(from: Date(), dateStyle: .none, timeStyle: .medium)
        logLines.append("\(stamp)  \(line)")
        if logLines.count > 500 { logLines.removeFirst(logLines.count - 500) }
    }

    static func join(_ directory: String, _ name: String) -> String {
        directory.hasSuffix("\\") ? directory + name : directory + "\\" + name
    }

    static func bytes(_ value: UInt32) -> String {
        ByteCountFormatter.string(fromByteCount: Int64(value), countStyle: .file)
    }
}
