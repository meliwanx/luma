import 'dart:ui' as ui;

import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:luma_client/brand.dart';

void main() {
  for (final brightness in Brightness.values) {
    testWidgets('wordmark paints accessible ${brightness.name} theme ink', (
      tester,
    ) async {
      final semantics = tester.ensureSemantics();
      try {
        const boundaryKey = ValueKey('logo-pixels');
        await tester.pumpWidget(
          MaterialApp(
            theme: ThemeData(brightness: brightness),
            home: const Scaffold(
              body: Center(
                child: RepaintBoundary(
                  key: boundaryKey,
                  child: LumaLogo(width: 120),
                ),
              ),
            ),
          ),
        );
        await tester.pump();
        expect(find.bySemanticsLabel('Luma'), findsOneWidget);
        final size = tester.getSize(find.byType(LumaLogo));
        expect(size.width, 120);
        expect(size.width / size.height, closeTo(LumaLogo.aspectRatio, .001));

        final boundary = tester.renderObject<RenderRepaintBoundary>(
          find.byKey(boundaryKey),
        );
        final data = await tester.runAsync(() async {
          final image = await boundary.toImage(pixelRatio: 2);
          try {
            return await image.toByteData(format: ui.ImageByteFormat.rawRgba);
          } finally {
            image.dispose();
          }
        });
        final pixels = data!.buffer.asUint8List();
        final expectedInk = brightness == Brightness.dark
            ? const [255, 255, 255]
            : const [37, 99, 235];
        var solidPixels = 0;
        var transparentPixels = 0;
        for (var index = 0; index < pixels.length; index += 4) {
          if (pixels[index + 3] == 255) {
            expect(pixels.sublist(index, index + 3), expectedInk);
            solidPixels++;
          } else if (pixels[index + 3] == 0) {
            transparentPixels++;
          }
        }
        expect(solidPixels, greaterThan(500));
        expect(transparentPixels, greaterThan(solidPixels));
        expect(tester.takeException(), isNull);
      } finally {
        semantics.dispose();
      }
    });
  }
}
