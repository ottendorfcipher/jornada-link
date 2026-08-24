import JornadaCore
import SwiftUI

/// The shared LogoArt mark, drawn live in SwiftUI.
struct LogoView: View {
    var diameter: CGFloat
    var dimmed = false

    var body: some View {
        Canvas { context, size in
            context.withCGContext { cg in
                // Canvas gives a y-down context; LogoArt draws y-up.
                cg.translateBy(x: 0, y: size.height)
                cg.scaleBy(x: 1, y: -1)
                LogoArt.draw(in: cg,
                             rect: CGRect(origin: .zero, size: size),
                             palette: dimmed ? .disabled : .standard)
            }
        }
        .frame(width: diameter, height: diameter)
        .accessibilityLabel("Jornada Sync logo")
    }
}
