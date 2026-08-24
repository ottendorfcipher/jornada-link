import JornadaCore
import SwiftUI

struct LogsView: View {
    @ObservedObject var model: AppModel

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 2) {
                    ForEach(Array(model.logLines.enumerated()), id: \.offset) { index, line in
                        Text(line)
                            .font(.system(.caption, design: .monospaced))
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .id(index)
                    }
                }
                .padding(10)
            }
            .background(Color(nsColor: .textBackgroundColor))
            .onChange(of: model.logLines.count) {
                proxy.scrollTo(model.logLines.indices.last, anchor: .bottom)
            }
        }
        .toolbar {
            ToolbarItemGroup {
                Button("PPP Log") {
                    NSWorkspace.shared.open(PppController.stateDirectory()
                        .appendingPathComponent("ppp.log"))
                }
                Button("Copy") {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(model.logLines.joined(separator: "\n"),
                                                   forType: .string)
                }
            }
        }
    }
}
