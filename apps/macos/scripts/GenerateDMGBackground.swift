import AppKit

// A two-times-resolution background; the App and Applications icons remain
// real Finder items, placed over the quiet areas at (176, 224) and (504, 224).
guard (2...3).contains(CommandLine.arguments.count) else { fatalError("请指定背景PNG输出路径和可选倍率") }
let size = NSSize(width: 680, height: 420)
let scale = CommandLine.arguments.count == 3 ? Int(CommandLine.arguments[2])! : 2
precondition([1, 2].contains(scale))
let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: 680 * scale, pixelsHigh: 420 * scale,
    bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
    colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
bitmap.size = size
let context = NSGraphicsContext(bitmapImageRep: bitmap)!
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = context

func color(_ hex: UInt32) -> NSColor {
    NSColor(srgbRed: CGFloat((hex >> 16) & 255) / 255,
            green: CGFloat((hex >> 8) & 255) / 255,
            blue: CGFloat(hex & 255) / 255, alpha: 1)
}

func centered(_ text: String, y: CGFloat, size fontSize: CGFloat,
              weight: NSFont.Weight = .regular, foreground: NSColor) {
    let paragraph = NSMutableParagraphStyle()
    paragraph.alignment = .center
    (text as NSString).draw(in: NSRect(x: 28, y: y, width: 624, height: 38),
        withAttributes: [.font: NSFont.systemFont(ofSize: fontSize, weight: weight),
                         .foregroundColor: foreground, .paragraphStyle: paragraph])
}

NSGradient(starting: color(0xFAFBFF), ending: color(0xF0F2F8))!
    .draw(in: NSRect(origin: .zero, size: size), angle: -90)
centered("知径 · Knora", y: 340, size: 26, weight: .semibold, foreground: color(0x242A38))

// A restrained directional mark connects the two native icons.
let arrow = NSBezierPath()
arrow.move(to: NSPoint(x: 299, y: 196))
arrow.line(to: NSPoint(x: 371, y: 196))
arrow.move(to: NSPoint(x: 359, y: 208))
arrow.line(to: NSPoint(x: 371, y: 196))
arrow.line(to: NSPoint(x: 359, y: 184))
arrow.lineWidth = 3
arrow.lineCapStyle = .round
arrow.lineJoinStyle = .round
color(0xA7B1CD).setStroke()
arrow.stroke()

NSGraphicsContext.restoreGraphicsState()
try bitmap.representation(using: .png, properties: [:])!
    .write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
