// Draw the supplied SVG path in CoreGraphics without a browser or extra packages.
import Foundation
import CoreGraphics
import ImageIO
let tokens = try JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))) as! [Any]
let path = CGMutablePath()
var i = 0
while i < tokens.count {
    let command = tokens[i] as! String
    i += 1
    if command == "M" {
        path.move(to: CGPoint(x: tokens[i] as! Double, y: tokens[i+1] as! Double))
        i += 2
    } else if command == "C" {
        path.addCurve(to: CGPoint(x: tokens[i+4] as! Double, y: tokens[i+5] as! Double), control1: CGPoint(x: tokens[i] as! Double, y: tokens[i+1] as! Double), control2: CGPoint(x: tokens[i+2] as! Double, y: tokens[i+3] as! Double))
        i += 6
    } else {
        fatalError("Unsupported SVG path command")
    }
}
let context = CGContext(data: nil, width: 1060, height: 520, bitsPerComponent: 8, bytesPerRow: 1060 * 4, space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
context.translateBy(x: -45, y: 795)
context.scaleBy(x: 1, y: -1)
context.addPath(path)
context.setStrokeColor(CGColor(gray: 0, alpha: 1))
context.setLineWidth(78)
context.setLineCap(.round)
context.strokePath()
let destination = CGImageDestinationCreateWithURL(URL(fileURLWithPath: CommandLine.arguments[2]) as CFURL, "public.png" as CFString, 1, nil)!
CGImageDestinationAddImage(destination, context.makeImage()!, nil)
if !CGImageDestinationFinalize(destination) { fatalError("Failed to write tray PNG") }
