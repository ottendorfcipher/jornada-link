import JornadaCore
import SwiftUI

/// Instrument terminal through the GPIB gateway on the device: address, command line with
/// Send / Query / Read, presets, bus operations, and a log. Restrained on purpose: system
/// colours, the default button is the only emphasis.
struct GpibView: View {
    @ObservedObject var model: AppModel
    @StateObject private var gpib = GpibController()
    @State private var command = "*IDN?"
    @FocusState private var commandFocused: Bool

    private static let presets: [(String, String)] = [
        ("Identify", "*IDN?"), ("Reset", "*RST"), ("Clear status", "*CLS"), ("Event status", "*ESR?"),
        ("All events", "ALLEV?"), ("Autoset", "AUTOSET EXECUTE"), ("Run", "ACQUIRE:STATE RUN"),
        ("Stop", "ACQUIRE:STATE STOP"), ("Single sequence", "ACQUIRE:STOPAFTER SEQUENCE;STATE RUN"),
        ("CH1 scale?", "CH1:SCALE?"), ("Timebase?", "HORIZONTAL:MAIN:SCALE?"),
        ("Measure frequency", "MEASUREMENT:IMMED:TYPE FREQUENCY;:MEASUREMENT:IMMED:VALUE?"),
        ("Measure Vpk-pk", "MEASUREMENT:IMMED:TYPE PK2PK;:MEASUREMENT:IMMED:VALUE?"),
        ("Waveform preamble", "DATA:SOURCE CH1;:DATA:ENCDG ASCII;:WFMPRE?"),
        ("Waveform data", "DATA:SOURCE CH1;:DATA:ENCDG ASCII;:CURVE?"),
    ]

    var body: some View {
        VStack(spacing: 0) {
            gatewayBar
            Divider()
            commandBar
            Divider()
            busBar
            Divider()
            logView
        }
        .onAppear { gpib.host = model.device?.ip ?? PppController.deviceIp }
        .onChange(of: model.device?.ip) { _, ip in gpib.host = ip ?? PppController.deviceIp }
    }

    private var gatewayBar: some View {
        HStack(spacing: 10) {
            Circle()
                .fill(gpib.connected ? Color.green : Color.secondary.opacity(0.4))
                .frame(width: 9, height: 9)
            Text(gpib.connected ? "gateway connected (\(gpib.host))" : "gateway not connected")
                .font(.callout)
            Spacer()
            Button("Start gateway on device") {
                Task {
                    if await model.launchOnDevice(path: "\\gpibsrv.exe") {
                        try? await Task.sleep(for: .seconds(4))
                        gpib.connect()
                    }
                }
            }
            .disabled(model.phase != .connected || gpib.busy)
            .help("Launches \\gpibsrv.exe on the Jornada, then connects to it")
            Button(gpib.connected ? "Disconnect" : "Connect") {
                if gpib.connected { gpib.disconnect() } else { gpib.connect() }
            }
            .disabled(gpib.busy)
            Button("Stop gateway") { gpib.stopGateway() }
                .disabled(!gpib.connected || gpib.busy)
                .help("Sends ++quit; the gateway window on the device closes")
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 7)
        .background(.bar)
    }

    private var commandBar: some View {
        HStack(spacing: 8) {
            Text("Address")
            TextField("1", value: $gpib.address, format: .number)
                .frame(width: 44)
                .textFieldStyle(.roundedBorder)
            TextField("Command", text: $command)
                .textFieldStyle(.roundedBorder)
                .focused($commandFocused)
                .onSubmit { gpib.send(command) }
            Menu("Presets") {
                ForEach(Self.presets, id: \.1) { label, text in
                    Button(label) { command = text; commandFocused = true }
                }
            }
            .frame(width: 100)
            Button("Send") { gpib.send(command) }
                .keyboardShortcut(.defaultAction)
                .disabled(!gpib.connected || gpib.busy)
            Button("Query") { gpib.query(command) }
                .disabled(!gpib.connected || gpib.busy)
            Button("Read") { gpib.read() }
                .disabled(!gpib.connected || gpib.busy)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
    }

    private var busBar: some View {
        HStack(spacing: 8) {
            Button("IFC") { gpib.interfaceClear() }
            Button("Clear") { gpib.deviceClear() }
            Button("Trigger") { gpib.trigger() }
            Button("Poll") { gpib.serialPoll() }
            Button("Local") { gpib.local() }
            Spacer()
            if gpib.busy { ProgressView().controlSize(.small) }
            Button("Clear log") { gpib.lines.removeAll() }
        }
        .disabled(!gpib.connected || gpib.busy)
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
    }

    private var logView: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 2) {
                    ForEach(Array(gpib.lines.enumerated()), id: \.offset) { index, line in
                        Text(line)
                            .font(.system(.body, design: .monospaced))
                            .textSelection(.enabled)
                            .id(index)
                    }
                }
                .padding(12)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .onChange(of: gpib.lines.count) { _, count in
                if count > 0 { proxy.scrollTo(count - 1, anchor: .bottom) }
            }
        }
    }
}

/// Owns the gateway connection on a serial background queue; publishes results on the main actor.
@MainActor
final class GpibController: ObservableObject {
    @Published var host = PppController.deviceIp
    @Published var address = 1
    @Published var connected = false
    @Published var busy = false
    @Published var lines: [String] = []

    private let queue = DispatchQueue(label: "gpib.gateway")
    private let gateway = GpibGateway(timeout: 6.0)
    private static let logLimit = 400

    private func append(_ line: String) {
        lines.append(line)
        if lines.count > Self.logLimit { lines.removeFirst(lines.count - Self.logLimit) }
    }

    /// Run `work` on the gateway queue, log its outcome.
    private func perform(_ label: String, _ work: @escaping (GpibGateway, Int) throws -> String?) {
        guard !busy else { return }
        busy = true
        let pad = address
        let gateway = self.gateway
        queue.async {
            let outcome: Result<String?, Error> = Result { try work(gateway, pad) }
            Task { @MainActor in
                switch outcome {
                case .success(let text):
                    if let text { self.append(text) }
                case .failure(let error):
                    self.append("\(label) failed: \(error)")
                    if case GpibGateway.GpibError.notConnected = error { self.connected = false }
                    if let socketError = error as? TcpSocket.SocketError, case .closed = socketError {
                        self.connected = false
                    }
                }
                self.busy = false
            }
        }
    }

    func connect() {
        let host = self.host
        busy = true
        queue.async { [gateway] in
            let outcome = Result { try gateway.connect(host: host); return try gateway.version() }
            Task { @MainActor in
                switch outcome {
                case .success(let version):
                    self.connected = true
                    self.append("connected to \(host): \(version)")
                case .failure(let error):
                    self.connected = false
                    self.append("connect failed: \(error)")
                }
                self.busy = false
            }
        }
    }

    func disconnect() {
        queue.async { [gateway] in gateway.close() }
        connected = false
        append("disconnected")
    }

    func stopGateway() {
        perform("stop gateway") { gateway, _ in
            try gateway.quitGateway()
            gateway.close()
            return "gateway stopped"
        }
        connected = false
    }

    func send(_ text: String) {
        let trimmed = text.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty else { return }
        if trimmed.hasSuffix("?") { query(trimmed); return }
        perform("send") { gateway, pad in
            try gateway.write(address: pad, trimmed)
            return "> \(trimmed)"
        }
    }

    func query(_ text: String) {
        let trimmed = text.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty else { return }
        perform("query") { gateway, pad in
            let answer = try gateway.query(address: pad, trimmed)
            return "> \(trimmed)\n< \(answer)"
        }
    }

    func read() {
        perform("read") { gateway, pad in "< \(try gateway.read(address: pad))" }
    }

    func serialPoll() {
        perform("serial poll") { gateway, pad in
            let stb = try gateway.serialPoll(address: pad)
            let srq = stb & 0x40 != 0 ? " (requesting service)" : ""
            return String(format: "serial poll %d: status byte 0x%02x%@", pad, stb, srq)
        }
    }

    func interfaceClear() { perform("interface clear") { gateway, _ in try gateway.interfaceClear(); return "interface clear sent" } }
    func deviceClear() { perform("device clear") { gateway, pad in try gateway.deviceClear(address: pad); return "device clear sent to \(pad)" } }
    func trigger() { perform("trigger") { gateway, pad in try gateway.trigger(address: pad); return "trigger sent to \(pad)" } }
    func local() { perform("go to local") { gateway, pad in try gateway.local(address: pad); return "go to local sent to \(pad)" } }
}
