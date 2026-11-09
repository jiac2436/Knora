import Foundation
import CoreGraphics
import ImageIO
import UniformTypeIdentifiers

// Derive a small toolbar glyph from the existing brand artwork. Do not redraw
// the mark or modify the app/general extension icons.
let input = URL(fileURLWithPath: CommandLine.arguments[1])
let output = URL(fileURLWithPath: CommandLine.arguments[2], isDirectory: true)
guard let source = CGImageSourceCreateWithURL(input as CFURL, nil),
      let original = CGImageSourceCreateImageAtIndex(source, 0, nil) else { fatalError("Invalid brand PNG") }
let width = original.width, height = original.height
let space = CGColorSpace(name: CGColorSpace.sRGB)!
let format = CGBitmapInfo.byteOrder32Big.rawValue | CGImageAlphaInfo.premultipliedLast.rawValue
var pixels = [UInt8](repeating: 0, count: width * height * 4)
var lowerX = width, lowerY = height, upperX = 0, upperY = 0
let glyph: CGImage = pixels.withUnsafeMutableBytes { memory in
    guard let context = CGContext(data: memory.baseAddress, width: width, height: height,
        bitsPerComponent: 8, bytesPerRow: width * 4, space: space, bitmapInfo: format) else { fatalError("Cannot decode PNG") }
    context.draw(original, in: CGRect(x: 0, y: 0, width: width, height: height))
    let buffer = memory.bindMemory(to: UInt8.self)
    for y in 0..<height {
        for x in 0..<width {
            let i = (y * width + x) * 4
            let r = Int(buffer[i]), g = Int(buffer[i + 1]), b = Int(buffer[i + 2])
            let high = max(r, g, b), low = min(r, g, b)
            // The two saturated rings and central white dot belong to the mark;
            // the neutral exterior tile/rim does not. Feather at source resolution.
            let color = high > 65 ? min(1, max(0, Double(high - low - 24) / 22)) : 0
            let center = x > width * 34 / 100 && x < width * 66 / 100
                && y > height * 30 / 100 && y < height * 70 / 100
            let white = center && high - low < 32 ? min(1, max(0, Double(low - 45) / 150)) : 0
            let opacity = max(color, white)
            for channel in 0..<4 { buffer[i + channel] = UInt8((Double(buffer[i + channel]) * opacity).rounded()) }
            if buffer[i + 3] >= 26 {
                lowerX = min(lowerX, x); upperX = max(upperX, x)
                lowerY = min(lowerY, y); upperY = max(upperY, y)
            }
        }
    }
    guard let result = context.makeImage() else { fatalError("Cannot create glyph") }
    return result
}
precondition(upperX > lowerX && upperY > lowerY, "Brand mark was not found")
let side = max(upperX - lowerX + 1, upperY - lowerY + 1)
let centerX = Double(lowerX + upperX + 1) / 2, centerY = Double(lowerY + upperY + 1) / 2
let bounds = CGRect(x: floor(centerX - Double(side) / 2), y: floor(centerY - Double(side) / 2), width: Double(side), height: Double(side))
guard let cropped = glyph.cropping(to: bounds) else { fatalError("Cannot crop glyph") }
try FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)
for size in [16, 24, 32, 48] {
    guard let context = CGContext(data: nil, width: size, height: size, bitsPerComponent: 8,
        bytesPerRow: size * 4, space: space, bitmapInfo: format) else { fatalError("Cannot resize glyph") }
    context.interpolationQuality = .high
    let tile = CGRect(x: 0, y: 0, width: size, height: size)
    context.addPath(CGPath(roundedRect: tile, cornerWidth: Double(size) * 0.18,
                          cornerHeight: Double(size) * 0.18, transform: nil))
    context.clip()
    context.setFillColor(CGColor(red: 0.025, green: 0.025, blue: 0.035, alpha: 1))
    context.fill(tile)
    // Keep the black tile, but let the brand mark fill its width. The original
    // macOS artwork leaves too much space around the mark at toolbar sizes.
    context.draw(cropped, in: tile)
    let file = output.appendingPathComponent("toolbar-filled-\(size).png")
    guard let image = context.makeImage(), let destination = CGImageDestinationCreateWithURL(file as CFURL,
        UTType.png.identifier as CFString, 1, nil) else { fatalError("Cannot write glyph") }
    CGImageDestinationAddImage(destination, image, nil)
    precondition(CGImageDestinationFinalize(destination))
}
print("Toolbar glyph cropped to \(side)px from \(width)px source; black tile with full-size mark; 16/24/32/48px exports")
