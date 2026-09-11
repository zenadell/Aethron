// Read the words AND their boxes out of an image, using the OCR that is
// already in macOS.
//
// WHY THIS IS SWIFT AND NOT A PIP INSTALL. Every placement failure in
// this project traced to one thing: guessing which element the model
// wrote corresponds to which measured text run, from order and geometry.
// Text settles that exactly — match by the words. Vision has done
// on-device OCR since 10.15, but reaching it from Python needs
// pyobjc-framework-Vision, and this project is stdlib-only on purpose.
// A ~60 line helper compiled once with the swiftc already on the machine
// keeps that promise: no Python dependency, no network, no API key, and
// the binary is cached so it is built once and never again.
//
//   swiftc -O aethron_ocr.swift -o aethron_ocr
//   ./aethron_ocr shot.png        -> one JSON object per line
//
// Falls back honestly: if it cannot be built or run, the caller says so
// and carries on without OCR rather than pretending it read anything.

import Foundation
import Vision
import CoreGraphics
import ImageIO

func die(_ msg: String) -> Never {
    FileHandle.standardError.write(("aethron_ocr: " + msg + "\n").data(using: .utf8)!)
    exit(2)
}

let args = CommandLine.arguments
guard args.count > 1 else { die("usage: aethron_ocr <image>") }
let url = URL(fileURLWithPath: args[1])
guard let src = CGImageSourceCreateWithURL(url as CFURL, nil),
      let img = CGImageSourceCreateImageAtIndex(src, 0, nil) else {
    die("could not read \(args[1])")
}
let W = Double(img.width), H = Double(img.height)

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
// Layout matters more than language modelling here: a marketing page is
// full of product names, and "correcting" them to dictionary words would
// put the wrong string in the right box.
request.usesLanguageCorrection = false
if #available(macOS 13.0, *) {
    request.automaticallyDetectsLanguage = true
}

let handler = VNImageRequestHandler(cgImage: img, options: [:])
do { try handler.perform([request]) } catch { die("OCR failed: \(error)") }

var out: [String] = []
for case let obs as VNRecognizedTextObservation in request.results ?? [] {
    guard let best = obs.topCandidates(1).first else { continue }
    let b = obs.boundingBox            // normalised, origin bottom-left
    let x = b.origin.x * W
    let y = (1 - b.origin.y - b.size.height) * H
    let text = best.string
        .replacingOccurrences(of: "\\", with: "\\\\")
        .replacingOccurrences(of: "\"", with: "\\\"")
    out.append("""
    {"text":"\(text)","x":\(Int(x.rounded())),"y":\(Int(y.rounded())),\
    "w":\(Int((b.size.width * W).rounded())),\
    "h":\(Int((b.size.height * H).rounded())),\
    "confidence":\(String(format: "%.3f", best.confidence))}
    """)
}
print(out.joined(separator: "\n"))
