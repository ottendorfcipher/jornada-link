import CoreGraphics
import Foundation

/// The Jornada Sync mark: an emerald disc with two chasing sync arrows and a
/// small clamshell-handheld silhouette. Drawn with plain CoreGraphics so the
/// SwiftUI views and the .icns generator render the exact same artwork.
public enum LogoArt {

    public struct Palette {
        public let discTop: CGColor
        public let discBottom: CGColor
        public let ring: CGColor
        public let device: CGColor
        public let deviceScreen: CGColor

        public static let standard = Palette(
            discTop: CGColor(red: 0.271, green: 0.769, blue: 0.478, alpha: 1),      // emerald
            discBottom: CGColor(red: 0.043, green: 0.416, blue: 0.263, alpha: 1),   // deep green
            ring: CGColor(red: 1, green: 1, blue: 1, alpha: 1),
            device: CGColor(red: 0.031, green: 0.235, blue: 0.157, alpha: 1),
            deviceScreen: CGColor(red: 0.78, green: 0.98, blue: 0.85, alpha: 1)
        )

        public static let disabled = Palette(
            discTop: CGColor(gray: 0.62, alpha: 1),
            discBottom: CGColor(gray: 0.38, alpha: 1),
            ring: CGColor(gray: 0.95, alpha: 1),
            device: CGColor(gray: 0.25, alpha: 1),
            deviceScreen: CGColor(gray: 0.85, alpha: 1)
        )
    }

    /// One curved sync arrow (arc band + head), sweeping `sweep` radians
    /// counterclockwise from `start`. Head points along the travel direction.
    public static func arrowPath(center: CGPoint, radius: CGFloat, thickness: CGFloat,
                                 start: CGFloat, sweep: CGFloat) -> CGPath {
        let path = CGMutablePath()
        let end = start + sweep
        let outer = radius + thickness / 2
        let inner = radius - thickness / 2

        path.addArc(center: center, radius: outer, startAngle: start, endAngle: end, clockwise: false)
        path.addArc(center: center, radius: inner, startAngle: end, endAngle: start, clockwise: true)
        path.closeSubpath()

        // Arrowhead: triangle at the arc end, apex further along the travel direction.
        let headLength = thickness * 1.9
        let headHalfWidth = thickness * 1.25
        let apexAngle = end + headLength / radius
        let apex = CGPoint(x: center.x + radius * cos(apexAngle), y: center.y + radius * sin(apexAngle))
        let baseOuter = CGPoint(x: center.x + (radius + headHalfWidth) * cos(end),
                                y: center.y + (radius + headHalfWidth) * sin(end))
        let baseInner = CGPoint(x: center.x + (radius - headHalfWidth) * cos(end),
                                y: center.y + (radius - headHalfWidth) * sin(end))
        path.move(to: baseOuter)
        path.addLine(to: apex)
        path.addLine(to: baseInner)
        path.closeSubpath()
        return path
    }

    /// Clamshell handheld silhouette centered in `rect` (drawn open, hinge in
    /// the middle: slim screen half above, keyboard half below).
    public static func handheldPath(in rect: CGRect) -> CGPath {
        let path = CGMutablePath()
        let width = rect.width
        let height = rect.height
        let corner = width * 0.10
        let gap = height * 0.06
        let screenRect = CGRect(x: rect.minX, y: rect.midY + gap / 2,
                                width: width, height: height / 2 - gap / 2)
        let keyboardRect = CGRect(x: rect.minX, y: rect.minY,
                                  width: width, height: height / 2 - gap / 2)
        path.addRoundedRect(in: screenRect, cornerWidth: corner, cornerHeight: corner)
        path.addRoundedRect(in: keyboardRect, cornerWidth: corner, cornerHeight: corner)
        return path
    }

    public static func screenCutout(in rect: CGRect) -> CGRect {
        let gap = rect.height * 0.06
        let screenRect = CGRect(x: rect.minX, y: rect.midY + gap / 2,
                                width: rect.width, height: rect.height / 2 - gap / 2)
        return screenRect.insetBy(dx: rect.width * 0.10, dy: rect.height * 0.055)
    }

    /// Draw the full mark into a CG context. `rect` is the disc's bounding
    /// square (y-up coordinates). `squircle` draws a macOS-icon rounded-rect
    /// backdrop instead of a circle.
    public static func draw(in context: CGContext, rect: CGRect,
                            palette: Palette = .standard, squircle: Bool = false) {
        context.saveGState()

        let backdrop: CGPath
        if squircle {
            let cornerRadius = rect.width * 0.2237
            backdrop = CGPath(roundedRect: rect, cornerWidth: cornerRadius,
                              cornerHeight: cornerRadius, transform: nil)
        } else {
            backdrop = CGPath(ellipseIn: rect, transform: nil)
        }
        context.addPath(backdrop)
        context.clip()

        let colors = [palette.discTop, palette.discBottom] as CFArray
        if let gradient = CGGradient(colorsSpace: CGColorSpace(name: CGColorSpace.sRGB),
                                     colors: colors, locations: [0, 1]) {
            context.drawLinearGradient(gradient,
                                       start: CGPoint(x: rect.midX, y: rect.maxY),
                                       end: CGPoint(x: rect.midX, y: rect.minY),
                                       options: [])
        }
        // Soft top sheen: white fading to clear, no hard edge.
        if let sheen = CGGradient(colorsSpace: CGColorSpace(name: CGColorSpace.sRGB),
                                  colors: [CGColor(red: 1, green: 1, blue: 1, alpha: 0.20),
                                           CGColor(red: 1, green: 1, blue: 1, alpha: 0.0)] as CFArray,
                                  locations: [0, 1]) {
            context.drawLinearGradient(sheen,
                                       start: CGPoint(x: rect.midX, y: rect.maxY),
                                       end: CGPoint(x: rect.midX, y: rect.minY + rect.height * 0.45),
                                       options: [])
        }

        let center = CGPoint(x: rect.midX, y: rect.midY)
        let ringRadius = rect.width * 0.335
        let thickness = rect.width * 0.062

        context.setFillColor(CGColor(gray: 0, alpha: 0.18))  // soft ring shadow
        let shadowOffset = rect.width * 0.012
        for sweepStart in [CGFloat.pi * 0.58, CGFloat.pi * 1.58] {
            let shadow = arrowPath(center: CGPoint(x: center.x, y: center.y - shadowOffset),
                                   radius: ringRadius, thickness: thickness,
                                   start: sweepStart, sweep: .pi * 0.62)
            context.addPath(shadow)
        }
        context.fillPath()

        context.setFillColor(palette.ring)
        for sweepStart in [CGFloat.pi * 0.58, CGFloat.pi * 1.58] {
            context.addPath(arrowPath(center: center, radius: ringRadius, thickness: thickness,
                                      start: sweepStart, sweep: .pi * 0.62))
        }
        context.fillPath()

        let deviceSize = rect.width * 0.27
        let deviceRect = CGRect(x: center.x - deviceSize / 2, y: center.y - deviceSize * 0.46,
                                width: deviceSize, height: deviceSize * 0.92)
        context.setFillColor(palette.device)
        context.addPath(handheldPath(in: deviceRect))
        context.fillPath()
        context.setFillColor(palette.deviceScreen)
        let screen = screenCutout(in: deviceRect)
        context.fill(CGRect(x: screen.minX, y: screen.minY, width: screen.width, height: screen.height))

        context.restoreGState()
    }
}
