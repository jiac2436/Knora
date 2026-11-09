import AppKit
import Foundation

let app = URL(fileURLWithPath: CommandLine.arguments[1])
guard let bundle = Bundle(url: app),
      bundle.object(forInfoDictionaryKey: "CFBundleIconFile") as? String == "AppIcon.icns",
      let resource = bundle.url(forResource: "AppIcon", withExtension: "icns"),
      let image = NSImage(contentsOf: resource) else { fatalError("Missing bundle icon") }
let sizes = image.representations.map { $0.pixelsWide }.sorted()
precondition(Set(sizes) == Set([16, 32, 64, 128, 256, 512, 1024]), "Incomplete icon representations: \(sizes)")
for rep in image.representations {
    precondition(rep.pixelsWide == rep.pixelsHigh, "Non-square representation")
    guard let bitmap = rep as? NSBitmapImageRep else { fatalError("Undecodable bitmap") }
    precondition(bitmap.hasAlpha, "Lost icon transparency")
}
print("PASS: native Bundle icon lookup and ICNS decode; \(image.representations.count) representations; sizes \(sizes)")
