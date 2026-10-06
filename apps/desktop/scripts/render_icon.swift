// Draw desktop icons in CoreGraphics without a browser or extra packages.
import Foundation
import CoreGraphics
import ImageIO

let configuration = try JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))) as! [String: Any]
func number(_ key: String) -> CGFloat {
    return CGFloat((configuration[key] as! NSNumber).doubleValue)
}

let canvas = Int(number("canvas"))
let windows = configuration["windows"] as! Bool
let small = configuration["small"] as! Bool
let side = number("side")
let margin = number("margin")
let body = CGMutablePath()
if windows {
    body.addRoundedRect(in: CGRect(x: margin, y: margin, width: side, height: side), cornerWidth: number("radius"), cornerHeight: number("radius"))
} else {
    let points = configuration["body_points"] as! [[Double]]
    for (index, point) in points.enumerated() {
        let position = CGPoint(x: point[0], y: point[1])
        if index == 0 {
            body.move(to: position)
        } else {
            body.addLine(to: position)
        }
    }
    body.closeSubpath()
}

let tokens = configuration["logo_tokens"] as! [Any]
let logo = CGMutablePath()
var i = 0
while i < tokens.count {
    let command = tokens[i] as! String
    i += 1
    if command == "M" {
        logo.move(to: CGPoint(x: tokens[i] as! Double, y: tokens[i + 1] as! Double))
        i += 2
    } else if command == "C" {
        logo.addCurve(to: CGPoint(x: tokens[i + 4] as! Double, y: tokens[i + 5] as! Double), control1: CGPoint(x: tokens[i] as! Double, y: tokens[i + 1] as! Double), control2: CGPoint(x: tokens[i + 2] as! Double, y: tokens[i + 3] as! Double))
        i += 6
    } else {
        fatalError("Unsupported SVG path command")
    }
}

let colorSpace = CGColorSpace(name: CGColorSpace.sRGB)!
let context = CGContext(data: nil, width: canvas, height: canvas, bitsPerComponent: 8, bytesPerRow: canvas * 4, space: colorSpace, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
context.clear(CGRect(x: 0, y: 0, width: CGFloat(canvas), height: CGFloat(canvas)))
context.translateBy(x: 0, y: CGFloat(canvas))
context.scaleBy(x: 1, y: -1)

context.saveGState()
if !small {
    // Quartz shadows use the bitmap's unflipped coordinates: negative y is down.
    context.setShadow(offset: CGSize(width: 0, height: -number("shadow_dy")), blur: number("shadow_blur"), color: CGColor(gray: 0, alpha: 0.18))
}
context.addPath(body)
context.setFillColor(CGColor(gray: 1, alpha: 1))
context.fillPath()
context.restoreGState()

let gradient = CGGradient(colorSpace: colorSpace, colorComponents: [1, 1, 1, 1, 243.0 / 255, 246.0 / 255, 251.0 / 255, 1], locations: [0, 1], count: 2)!
context.saveGState()
context.addPath(body)
context.clip()
context.drawLinearGradient(gradient, start: CGPoint(x: CGFloat(canvas) / 2, y: margin), end: CGPoint(x: CGFloat(canvas) / 2, y: margin + side), options: [])
context.restoreGState()

context.saveGState()
context.translateBy(x: number("logo_x"), y: number("logo_y"))
context.scaleBy(x: number("logo_scale"), y: number("logo_scale"))
context.addPath(logo)
context.setStrokeColor(CGColor(colorSpace: colorSpace, components: [37.0 / 255, 99.0 / 255, 235.0 / 255, 1])!)
context.setLineWidth(number("logo_stroke"))
context.setLineCap(.round)
context.setLineJoin(.round)
context.strokePath()
context.restoreGState()

let destination = CGImageDestinationCreateWithURL(URL(fileURLWithPath: CommandLine.arguments[2]) as CFURL, "public.png" as CFString, 1, nil)!
CGImageDestinationAddImage(destination, context.makeImage()!, nil)
if !CGImageDestinationFinalize(destination) { fatalError("Failed to write desktop icon PNG") }
