import SwiftUI

struct MainWindow: View {
    @ObservedObject var model: AppModel

    var body: some View {
        VStack(spacing: 0) {
            BannerView(model: model)
            Divider()
            NavigationSplitView {
                List(AppModel.Pane.allCases, selection: paneSelection) { pane in
                    Label(pane.rawValue, systemImage: pane.symbol)
                        .tag(pane)
                        .badge(pane == .transfers
                               ? model.transfers.filter { $0.state == .running }.count
                               : 0)
                }
                .listStyle(.sidebar)
                .navigationSplitViewColumnWidth(min: 170, ideal: 185, max: 240)
            } detail: {
                switch model.pane {
                case .overview: OverviewView(model: model)
                case .files: FilesView(model: model)
                case .transfers: TransfersView(model: model)
                case .logs: LogsView(model: model)
                }
            }
        }
        .frame(minWidth: 780, minHeight: 520)
        .toolbar { connectionToolbar }
        .onAppear { model.start() }
    }

    private var paneSelection: Binding<AppModel.Pane?> {
        Binding(get: { model.pane }, set: { model.pane = $0 ?? .overview })
    }

    @ToolbarContentBuilder
    private var connectionToolbar: some ToolbarContent {
        ToolbarItemGroup(placement: .navigation) {
            Button {
                model.connectLink()
            } label: {
                Label(model.phase == .down ? "Connect" : "Reconnect",
                      systemImage: model.phase == .connected
                        ? "bolt.horizontal.circle.fill" : "bolt.horizontal.circle")
                    .foregroundStyle(model.phase == .connected ? .green : .primary)
            }
            .help(model.phase == .down
                  ? "Start the serial PPP link (asks for administrator rights)"
                  : "Fully restart the serial PPP link — fixes a stuck connection (asks for administrator rights)")
            if model.phase != .down {
                Button {
                    model.disconnectLink()
                } label: {
                    Label("Disconnect", systemImage: "xmark.circle")
                }
                .help("Stop the serial PPP link")
            }
        }
    }
}
