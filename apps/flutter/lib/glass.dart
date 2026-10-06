import 'dart:ui';

import 'package:flutter/material.dart';

enum GlassShape { rounded, circle, capsule }

/// A translucent surface whose rim catches the light above the blurred content.
class GlassSurface extends StatelessWidget {
  const GlassSurface({
    super.key,
    this.borderRadius = 22,
    this.padding = EdgeInsets.zero,
    this.shape = GlassShape.rounded,
    required this.child,
  });

  final double borderRadius;
  final EdgeInsetsGeometry padding;
  final GlassShape shape;
  final Widget child;

  @override
  Widget build(BuildContext context) {
    final dark = Theme.of(context).brightness == Brightness.dark;
    final radius = shape == GlassShape.rounded ? borderRadius : 999.0;
    final rounded = BorderRadius.circular(radius);
    return DecoratedBox(
      decoration: BoxDecoration(
        borderRadius: rounded,
        boxShadow: [
          BoxShadow(
            color: Colors.black.withValues(alpha: dark ? .18 : .06),
            blurRadius: 18,
            offset: const Offset(0, 5),
          ),
        ],
      ),
      child: ClipRRect(
        borderRadius: rounded,
        child: BackdropFilter(
          filter: ImageFilter.blur(sigmaX: 24, sigmaY: 24),
          child: CustomPaint(
            foregroundPainter: _GlassRim(radius: radius, dark: dark),
            child: ColoredBox(
              color: Colors.white.withValues(alpha: dark ? .08 : .55),
              child: Padding(padding: padding, child: child),
            ),
          ),
        ),
      ),
    );
  }
}

class _GlassRim extends CustomPainter {
  const _GlassRim({required this.radius, required this.dark});

  final double radius;
  final bool dark;

  @override
  void paint(Canvas canvas, Size size) {
    final bounds = Offset.zero & size;
    final paint = Paint()
      ..style = PaintingStyle.stroke
      ..strokeWidth = 1
      ..shader = LinearGradient(
        begin: Alignment.topLeft,
        end: Alignment.bottomRight,
        colors: [
          Colors.white.withValues(alpha: dark ? .34 : .95),
          Colors.white.withValues(alpha: dark ? .10 : .35),
          Colors.white.withValues(alpha: dark ? .16 : .65),
        ],
        stops: const [0, .58, 1],
      ).createShader(bounds);
    canvas.drawRRect(
      RRect.fromRectAndRadius(bounds.deflate(.5), Radius.circular(radius)),
      paint,
    );
  }

  @override
  bool shouldRepaint(_GlassRim oldDelegate) =>
      radius != oldDelegate.radius || dark != oldDelegate.dark;
}
