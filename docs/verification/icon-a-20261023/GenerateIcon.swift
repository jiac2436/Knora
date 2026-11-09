import AppKit
import ImageIO
import UniformTypeIdentifiers

// Package the selected artwork without redrawing it. Use the same 5% transparent
// margin as the previous macOS icon; round only the tile's exterior corners.
let input = URL(fileURLWithPath: CommandLine.arguments[1])
let output = URL(fileURLWithPath: CommandLine.arguments[2], isDirectory: true)
guard let source = CGImageSourceCreateWithURL(input as CFURL, nil),
      let artwork = CGImageSourceCreateImageAtIndex(source, 0, nil),
      artwork.width == artwork.height else { fatalError("Invalid square artwork") }
let colorSpace = CGColorSpace(name: CGColorSpace.sRGB)!
func context(_ size: Int) -> CGContext {
    guard let context = CGContext(data: nil, width: size, height: size,
        bitsPerComponent: 8, bytesPerRow: size * 4, space: colorSpace,
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else {
        fatalError("Cannot allocate bitmap")
    }
    context.interpolationQuality = .high
    return context
}
func save(_ image: CGImage, _ url: URL) {
    guard let destination = CGImageDestinationCreateWithURL(url as CFURL,
        UTType.png.identifier as CFString, 1, nil) else { fatalError("Cannot write PNG") }
    CGImageDestinationAddImage(destination, image, nil)
    precondition(CGImageDestinationFinalize(destination))
}
try FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)
let masterContext = context(1024)
let tile = CGRect(x: 51.2, y: 51.2, width: 921.6, height: 921.6)
masterContext.addPath(CGPath(roundedRect: tile, cornerWidth: tile.width * 0.235,
    cornerHeight: tile.height * 0.235, transform: nil))
masterContext.clip()
masterContext.draw(artwork, in: tile)
let master = masterContext.makeImage()!
save(master, output.appendingPathComponent("AppIcon.png"))
let iconset = output.appendingPathComponent("AppIcon.iconset", isDirectory: true)
try FileManager.default.createDirectory(at: iconset, withIntermediateDirectories: true)
for size in [16, 32, 128, 256, 512] {
    for scale in [1, 2] {
        let pixels = size * scale
        let bitmap = context(pixels)
        bitmap.draw(master, in: CGRect(x: 0, y: 0, width: pixels, height: pixels))
        let suffix = scale == 2 ? "@2x" : ""
        save(bitmap.makeImage()!, iconset.appendingPathComponent("icon_\(size)x\(size)\(suffix).png"))
    }
}
print("PASS: selected artwork packaged as 1024px PNG and 10 icon representations")
