import AppKit
import Foundation

// 屏保画面：1072x1448（面板原始像素），灰阶，按 viewer 里 /idle 的同一套几何重画。
let W = 1072.0, H = 1448.0, S = 2.1   // S: CSS -> 面板像素的缩放，和网页设计一一对应

let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(W), pixelsHigh: Int(H),
                           bitsPerSample: 8, samplesPerPixel: 1, hasAlpha: false, isPlanar: false,
                           colorSpaceName: .calibratedWhite, bytesPerRow: Int(W), bitsPerPixel: 8)!
let ctx = NSGraphicsContext(bitmapImageRep: rep)!
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = ctx
let cg = ctx.cgContext

func px(_ css: Double) -> CGFloat { CGFloat(css * S) }
func rectCss(_ x: Double, _ y: Double, _ w: Double, _ h: Double) -> NSRect {
    NSRect(x: px(x), y: H - px(y + h), width: px(w), height: px(h))   // y 从顶部算
}
func fill(_ r: NSRect, white: CGFloat) {
    NSColor(calibratedWhite: white, alpha: 1).setFill()
    r.fill()
}
func stroke(_ r: NSRect, width: Double) {
    let p = NSBezierPath(rect: r)
    p.lineWidth = px(width)
    NSColor.black.setStroke()
    p.stroke()
}
func mono(_ s: String, size: Double, at x: Double, top: Double, tracking: Double,
          alignRight: Bool = false, upper: Bool = true) {
    let attrs: [NSAttributedString.Key: Any] = [
        .font: NSFont(name: "CourierNewPSMT", size: px(size)) ?? NSFont.monospacedSystemFont(ofSize: px(size), weight: .regular),
        .foregroundColor: NSColor.black,
        .kern: px(tracking),
    ]
    let str = NSAttributedString(string: upper ? s.uppercased() : s, attributes: attrs)
    let w = str.size().width
    str.draw(at: NSPoint(x: alignRight ? px(x) - w : px(x), y: H - px(top) - str.size().height))
}

// background
fill(NSRect(x: 0, y: 0, width: W, height: H), white: 1.0)

// frame + corner ticks
stroke(rectCss(16, 16, 511 - 32, 690 - 32), width: 2)
// four L marks on the frame corners, arms pointing along the frame
fill(rectCss(16, 16, 26, 2), white: 0.0);      fill(rectCss(16, 16, 2, 26), white: 0.0)
fill(rectCss(469, 16, 26, 2), white: 0.0);     fill(rectCss(493, 16, 2, 26), white: 0.0)
fill(rectCss(16, 672, 26, 2), white: 0.0);     fill(rectCss(16, 648, 2, 26), white: 0.0)
fill(rectCss(469, 672, 26, 2), white: 0.0);    fill(rectCss(493, 648, 2, 26), white: 0.0)

// header
mono("pi / eink", size: 14, at: 38, top: 40, tracking: 3)
mono("terminal", size: 14, at: 473, top: 40, tracking: 3, alignRight: true)
fill(rectCss(38, 66, 435, 2), white: 0.0)
fill(rectCss(38, 72, 435, 1), white: 0.45)

// dot matrix (top right)
for row in 0..<6 {
    for col in 0..<6 {
        fill(rectCss(430 + Double(col) * 11, 84 + Double(row) * 11, 2.2, 2.2), white: 0.0)
    }
}

// the mark: 260 CSS wide, centred, y = 205
let mx = 125.0, my = 205.0, ms = 260.0
func block(_ x: Double, _ y: Double, _ w: Double, _ h: Double) -> NSRect {
    rectCss(mx + ms * x / 470.0, my + ms * y / 470.0, ms * w / 470.0, ms * h / 470.0)
}
// a) solid, b) solid
fill(block(0, 0, 352.07, 117.36), white: 0.0)
fill(block(234.71, 117.36, 117.36, 117.35), white: 0.0)
// c) d) e) hatch: clip to the block, then draw 45° stripes
let hatchParts = [block(0, 117.36, 117.36, 117.35),
                  block(117.36, 234.71, 117.35, 117.36),
                  block(0, 352.07, 117.36, 117.35)]
for part in hatchParts {
    NSGraphicsContext.saveGraphicsState()
    NSBezierPath(rect: part).addClip()
    var x = part.minX - part.height
    while x < part.maxX {
        let p = NSBezierPath()
        p.move(to: NSPoint(x: x, y: part.minY))
        p.line(to: NSPoint(x: x + part.height, y: part.maxY))
        p.lineWidth = px(2)
        NSColor.black.setStroke()
        p.stroke()
        x += px(7)
    }
    NSGraphicsContext.restoreGraphicsState()
}
// f) outline
stroke(block(352.07, 234.71, 117.36, 234.72), width: 2)

// word
mono("pi", size: 19, at: 236, top: 488, tracking: 0.6, upper: false)

// measurement ticks beside the mark
fill(rectCss(38, 335, 2, 14), white: 0.0)
fill(rectCss(471, 335, 2, 14), white: 0.0)

// footer
fill(rectCss(38, 618, 435, 1), white: 0.45)
fill(rectCss(38, 624, 435, 2), white: 0.0)
mono("511x690", size: 12, at: 38, top: 634, tracking: 1)
mono("e-ink", size: 12, at: 255, top: 634, tracking: 1)
mono("idle", size: 12, at: 473, top: 634, tracking: 1, alignRight: true)

NSGraphicsContext.restoreGraphicsState()

try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: "/tmp/sleep.png"))
try! rep.representation(using: .jpeg, properties: [.compressionFactor: 0.92])!.write(to: URL(fileURLWithPath: "/tmp/sleep.jpg"))
print("wrote /tmp/sleep.png and /tmp/sleep.jpg")
