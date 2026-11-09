import Foundation
import ImageIO
import Vision

struct MacImageTextRecognizer: ImageTextRecognizing {
    func recognize(file: URL) throws -> String {
        guard let source = CGImageSourceCreateWithURL(file as CFURL, nil),
              let type = CGImageSourceGetType(source) as String?,
              ["public.png", "public.jpeg", "com.compuserve.gif", "org.webmproject.webp"].contains(type),
              let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
              let width = properties[kCGImagePropertyPixelWidth] as? Int,
              let height = properties[kCGImagePropertyPixelHeight] as? Int,
              width > 0, height > 0, width * height <= 30_000_000 else {
            throw failure("图片格式无效或超过 3000 万像素。")
        }
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.recognitionLanguages = ["zh-Hans", "en-US"]
        request.usesLanguageCorrection = true
        do {
            try VNImageRequestHandler(url: file).perform([request])
            let text = (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }.joined(separator: "\n")
            return text.unicodeScalars.count > 20000
                ? String(text.prefix(19900)) + "\n[OCR 文字超过上限，仅保留前 19900 字符；完整内容请查看图片原件。]"
                : text
        } catch {
            // Image validity and original preservation must not depend on OCR availability.
            return ""
        }
    }

}
