import 'package:flutter/material.dart';

/// The supplied wordmark, cropped around its strokes instead of the square artboard.
class LumaLogo extends StatelessWidget {
  const LumaLogo({super.key, this.width = 100, this.color});

  static const blue = Color(0xFF2563EB);
  static const aspectRatio = 1020 / 480;

  final double width;
  final Color? color;

  @override
  Widget build(BuildContext context) {
    final ink =
        color ??
        (Theme.of(context).brightness == Brightness.dark ? Colors.white : blue);
    return Semantics(
      label: 'Luma',
      image: true,
      child: SizedBox(
        width: width,
        height: width / aspectRatio,
        child: CustomPaint(painter: _LumaLogoPainter(ink)),
      ),
    );
  }
}

class _LumaLogoPainter extends CustomPainter {
  const _LumaLogoPainter(this.color);

  final Color color;

  // Exact path from the supplied SVG, with the original round caps and stroke.
  static final _path = Path()
    ..moveTo(1049.5, 648)
    ..cubicTo(1030.5, 676, 987.5, 731, 955, 724)
    ..cubicTo(897.909, 711.704, 980.5, 600.5, 896, 579)
    ..cubicTo(860.193, 569.89, 831.748, 590.946, 807.811, 619)
    ..cubicTo(775.262, 657.15, 751.05, 708.238, 728, 714)
    ..cubicTo(688, 724, 705, 581.5, 669, 579)
    ..cubicTo(633, 576.5, 595.5, 704, 585.5, 704)
    ..cubicTo(575.5, 704, 597, 593, 551, 593)
    ..cubicTo(505, 593, 489.5, 719.5, 455, 724)
    ..cubicTo(427.4, 727.6, 423, 655, 430.5, 600.5)
    ..cubicTo(430.5, 587, 377.5, 745, 343, 740)
    ..cubicTo(308.5, 735, 343, 593, 324, 593)
    ..cubicTo(312.5, 593, 262.5, 760.75, 205, 740)
    ..cubicTo(90, 698.5, 273.5, 233, 360, 342)
    ..cubicTo(405.5, 411.5, 186.333, 599.667, 93, 648)
    ..moveTo(896, 579)
    ..cubicTo(977, 609, 891.5, 740, 827, 740)
    ..cubicTo(784.079, 740, 751.261, 700.6, 807.811, 619);

  @override
  void paint(Canvas canvas, Size size) {
    final scale = (size.width / 1020).clamp(0.0, size.height / 480).toDouble();
    canvas.save();
    canvas.translate(
      (size.width - 1020 * scale) / 2,
      (size.height - 480 * scale) / 2,
    );
    canvas.scale(scale);
    canvas.translate(-60, -290);
    canvas.drawPath(
      _path,
      Paint()
        ..color = color
        ..style = PaintingStyle.stroke
        ..strokeWidth = 51
        ..strokeCap = StrokeCap.round,
    );
    canvas.restore();
  }

  @override
  bool shouldRepaint(_LumaLogoPainter oldDelegate) =>
      color != oldDelegate.color;
}
