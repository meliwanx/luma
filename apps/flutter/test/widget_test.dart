// This is a basic Flutter widget test.
//
// To perform an interaction with a widget in your test, use the WidgetTester
// utility in the flutter_test package. For example, you can send tap and scroll
// gestures. You can also use WidgetTester to find child widgets in the widget
// tree, read text, and verify that the values of widget properties are correct.

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:luma_client/main.dart';

void main() {
  testWidgets('Luma assistant shell renders', (WidgetTester tester) async {
    await tester.pumpWidget(const MyApp());
    await tester.pump(const Duration(milliseconds: 100));
    expect(find.text('早上好，我是 Luma'), findsOneWidget);
    expect(find.text('消息'), findsOneWidget);
    expect(find.byTooltip('刷新'), findsOneWidget);
  });

  testWidgets('Luma shell fits a phone screen', (WidgetTester tester) async {
    tester.view.physicalSize = const Size(390, 844);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.reset);
    await tester.pumpWidget(const MyApp());
    await tester.pump(const Duration(milliseconds: 100));
    expect(find.text('早上好，我是 Luma'), findsOneWidget);
    for (final label in ['动态', '点子', '目标', '资源库', '聊天']) {
      await tester.tap(find.byTooltip(label));
      await tester.pump(const Duration(milliseconds: 300));
    }
  });
}
