import SwiftUI

@main
struct JornadaSyncApp: App {
    @StateObject private var model = AppModel()

    var body: some Scene {
        // WindowGroup, not Window: a single-Window scene never comes back once
        // the user closes it (Dock clicks / `open` just activate a windowless
        // process). WindowGroup recreates the window on reopen; the model is
        // app-level state and AppModel.start() guards against double-starts.
        WindowGroup("Jornada Sync", id: "main") {
            MainWindow(model: model)
        }
        .defaultSize(width: 900, height: 620)
        // Belt and braces against the zero-window trap: never restore a saved
        // windowless session, and always present the window at launch. Without
        // these, quitting while the window is closed leaves every future launch
        // a windowless process that Dock clicks cannot revive.
        .restorationBehavior(.disabled)
        .defaultLaunchBehavior(.presented)
        .commands {
            // Keyboard navigation independent of sidebar clicks: even if List
            // selection ever misbehaves, ⌘1–⌘4 always switch panes.
            CommandMenu("Go") {
                ForEach(Array(AppModel.Pane.allCases.enumerated()), id: \.element) { index, pane in
                    Button(pane.rawValue) { model.pane = pane }
                        .keyboardShortcut(KeyEquivalent(Character("\(index + 1)")), modifiers: .command)
                }
                Divider()
                Button("Refresh") { model.retrySync() }
                    .keyboardShortcut("r", modifiers: .command)
                    .disabled(model.phase != .connected)
                Button("Enclosing Folder") { model.goUp() }
                    .keyboardShortcut(.upArrow, modifiers: .command)
                    .disabled(model.currentPath == "\\" || model.pane != .files)
            }
        }
    }
}
