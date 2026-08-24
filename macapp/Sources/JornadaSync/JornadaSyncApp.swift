import SwiftUI

@main
struct JornadaSyncApp: App {
    @StateObject private var model = AppModel()

    var body: some Scene {
        Window("Jornada Sync", id: "main") {
            MainWindow(model: model)
        }
        .defaultSize(width: 900, height: 620)
    }
}
