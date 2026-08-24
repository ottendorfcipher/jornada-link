import AppKit
import Foundation
import JornadaCore

/// Renders the Jornada Sync mark to PNGs: an .iconset directory (squircle
/// icon) plus a standalone logo PNG. Usage: icongen <output-dir>
let arguments = CommandLine.arguments
guard arguments.count >= 2 else {
    FileHandle.standardError.write(Data("usage: icongen <output-dir>\n".utf8))
    exit(2)
}
let outputRoot = URL(fileURLWithPath: arguments[1])
let iconsetURL = outputRoot.appendingPathComponent("AppIcon.iconset")
try FileManager.default.createDirectory(at: iconsetURL, withIntermediateDirectories: true)

func renderPNG(pixels: Int, squircle: Bool, margin: CGFloat, to url: URL) throws {
    guard let context = CGContext(data: nil, width: pixels, height: pixels,
                                  bitsPerComponent: 8, bytesPerRow: 0,
                                  space: CGColorSpace(name: CGColorSpace.sRGB)!,
                                  bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else {
        throw NSError(domain: "icongen", code: 1, userInfo: [NSLocalizedDescriptionKey: "CGContext failed"])
    }
    let size = CGFloat(pixels)
    let inset = size * margin
    LogoArt.draw(in: context,
                 rect: CGRect(x: inset, y: inset, width: size - 2 * inset, height: size - 2 * inset),
                 squircle: squircle)
    guard let image = context.makeImage() else {
        throw NSError(domain: "icongen", code: 2, userInfo: [NSLocalizedDescriptionKey: "makeImage failed"])
    }
    let rep = NSBitmapImageRep(cgImage: image)
    rep.size = NSSize(width: pixels, height: pixels)
    guard let png = rep.representation(using: .png, properties: [:]) else {
        throw NSError(domain: "icongen", code: 3, userInfo: [NSLocalizedDescriptionKey: "png encode failed"])
    }
    try png.write(to: url)
}

// macOS icon grid: content sits inside ~10% margins of the canvas.
let iconSlots: [(String, Int)] = [
    ("icon_16x16.png", 16), ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32), ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128), ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256), ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512), ("icon_512x512@2x.png", 1024),
]
for (name, pixels) in iconSlots {
    try renderPNG(pixels: pixels, squircle: true, margin: 0.097,
                  to: iconsetURL.appendingPathComponent(name))
}
try renderPNG(pixels: 512, squircle: false, margin: 0.02,
              to: outputRoot.appendingPathComponent("logo-512.png"))
try renderPNG(pixels: 1024, squircle: true, margin: 0.097,
              to: outputRoot.appendingPathComponent("icon-preview-1024.png"))
print("icongen: wrote \(iconsetURL.path)")
