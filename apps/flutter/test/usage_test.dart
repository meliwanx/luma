import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:luma_client/api.dart';
import 'package:luma_client/theme.dart';
import 'package:luma_client/views/usage.dart';

Map<String, dynamic> _report({int tokens = 1250, bool empty = false}) => {
  'range': '7d',
  'totals': {
    'calls': empty ? 0 : 2,
    'prompt_tokens': empty ? 0 : 1000,
    'completion_tokens': empty ? 0 : 250,
    'total_tokens': empty ? 0 : tokens,
    'estimated_ratio': empty ? 0 : 0.5,
  },
  'performance': {
    'first_token_ms_avg': empty ? null : 150,
    'duration_ms_p50': empty ? null : 500,
    'duration_ms_p95': empty ? null : 700,
  },
  'daily': [
    {
      'date': '2026-10-05',
      'calls': empty ? 0 : 2,
      'total_tokens': empty ? 0 : tokens,
    },
    {'date': '2026-10-06', 'calls': 0, 'total_tokens': 0},
  ],
  'purposes': empty
      ? []
      : [
          {'purpose': 'chat_round', 'calls': 2, 'total_tokens': tokens},
        ],
  'models': empty
      ? []
      : [
          {
            'model': 'model-with-a-long-name-for-narrow-mobile-screen',
            'calls': 2,
            'total_tokens': tokens,
          },
        ],
  'top_sessions': empty
      ? []
      : [
          {
            'session_id': 'session-1',
            'title': '旅行计划',
            'calls': 2,
            'total_tokens': tokens,
          },
        ],
};

class _UsageApi extends AssistantApi {
  _UsageApi(this.handler, {this.sessionHandler});

  final Future<Map<String, dynamic>> Function(String range) handler;
  final Future<Map<String, dynamic>> Function(String id)? sessionHandler;
  final sessionRequests = <String>[];

  @override
  Future<Map<String, dynamic>> usage({String range = '7d'}) => handler(range);

  @override
  Future<Map<String, dynamic>> getSession(String sessionId) {
    sessionRequests.add(sessionId);
    return sessionHandler?.call(sessionId) ?? Future.value({'id': sessionId});
  }
}

Widget _app(
  AssistantApi api, {
  Brightness brightness = Brightness.light,
  ValueChanged<String>? onSelectSession,
}) => MaterialApp(
  theme: buildLumaTheme(brightness),
  home: Scaffold(
    body: SingleChildScrollView(
      child: UsageSection(api: api, onSelectSession: onSelectSession),
    ),
  ),
);

void main() {
  testWidgets('usage shows aggregates, distributions and opens a top session', (
    tester,
  ) async {
    String? openedSession;
    final ranges = <String>[];
    final api = _UsageApi((range) async {
      ranges.add(range);
      return _report();
    });
    addTearDown(api.close);
    await tester.pumpWidget(
      _app(api, onSelectSession: (id) => openedSession = id),
    );
    await tester.pumpAndSettle();
    expect(ranges, ['7d']);
    expect(find.text('1,250'), findsOneWidget);
    expect(find.text('150 ms'), findsOneWidget);
    expect(find.text('对话轮次'), findsOneWidget);
    expect(find.textContaining('部分为估算'), findsOneWidget);
    expect(find.byKey(const Key('usage-daily-chart')), findsOneWidget);
    await tester.ensureVisible(
      find.byKey(const ValueKey('usage-session-session-1')),
    );
    await tester.tap(find.text('旅行计划'));
    await tester.pumpAndSettle();
    expect(api.sessionRequests, ['session-1']);
    expect(openedSession, 'session-1');
    expect(tester.takeException(), isNull);
  });

  testWidgets(
    'deleted top sessions keep the current view and do not navigate',
    (tester) async {
      String? openedSession;
      final api = _UsageApi(
        (_) async => _report(),
        sessionHandler: (_) async => throw const AssistantApiException(
          'Session not found',
          statusCode: 404,
        ),
      );
      addTearDown(api.close);
      await tester.pumpWidget(
        _app(api, onSelectSession: (id) => openedSession = id),
      );
      await tester.pumpAndSettle();
      await tester.ensureVisible(
        find.byKey(const ValueKey('usage-session-session-1')),
      );
      await tester.tap(find.text('旅行计划'));
      await tester.pumpAndSettle();
      expect(openedSession, isNull);
      expect(find.byType(UsageSection), findsOneWidget);
      expect(find.text('会话已删除或无法访问'), findsOneWidget);
      expect(api.sessionRequests, ['session-1']);
    },
  );

  testWidgets(
    'top session navigation waits for ownership lookup and ignores display title',
    (tester) async {
      String? openedSession;
      final session = Completer<Map<String, dynamic>>();
      final report = _report();
      (report['top_sessions'] as List).first['title'] = '已删除的会话';
      final api = _UsageApi(
        (_) async => report,
        sessionHandler: (_) => session.future,
      );
      addTearDown(api.close);
      await tester.pumpWidget(
        _app(api, onSelectSession: (id) => openedSession = id),
      );
      await tester.pumpAndSettle();
      final tile = find.byKey(const ValueKey('usage-session-session-1'));
      await tester.ensureVisible(tile);
      await tester.tap(tile);
      await tester.pump();
      expect(openedSession, isNull);
      expect(api.sessionRequests, ['session-1']);
      await tester.tap(tile);
      await tester.pump();
      expect(api.sessionRequests, ['session-1']);
      session.complete({'id': 'session-1'});
      await tester.pumpAndSettle();
      expect(openedSession, 'session-1');
    },
  );

  testWidgets('range changes discard stale responses', (tester) async {
    final first = Completer<Map<String, dynamic>>();
    final api = _UsageApi(
      (range) =>
          range == '7d' ? first.future : Future.value(_report(tokens: 3000)),
    );
    addTearDown(api.close);
    await tester.pumpWidget(_app(api));
    await tester.pump();
    await tester.tap(find.text('30 天'));
    await tester.pumpAndSettle();
    expect(find.text('3,000'), findsOneWidget);
    first.complete(_report(tokens: 1250));
    await tester.pumpAndSettle();
    expect(find.text('3,000'), findsOneWidget);
    expect(find.text('1,250'), findsNothing);
  });

  testWidgets('failed usage requests can be retried', (tester) async {
    var requests = 0;
    final api = _UsageApi((_) async {
      if (++requests == 1) throw const AssistantApiException('用量加载失败');
      return _report();
    });
    addTearDown(api.close);
    await tester.pumpWidget(_app(api));
    await tester.pumpAndSettle();
    expect(find.text('用量加载失败'), findsOneWidget);
    await tester.tap(find.text('重试'));
    await tester.pumpAndSettle();
    expect(find.text('1,250'), findsOneWidget);
    expect(requests, 2);
  });

  for (final brightness in Brightness.values) {
    testWidgets(
      'usage fits a narrow ${brightness.name} themed screen with empty data',
      (tester) async {
        tester.view.physicalSize = const Size(320, 700);
        tester.view.devicePixelRatio = 1;
        addTearDown(tester.view.reset);
        final api = _UsageApi((_) async => _report(empty: true));
        addTearDown(api.close);
        await tester.pumpWidget(_app(api, brightness: brightness));
        await tester.pumpAndSettle();
        expect(find.text('此范围暂无模型调用'), findsOneWidget);
        expect(find.byKey(const Key('usage-estimated')), findsNothing);
        final chart = tester.widget<CustomPaint>(
          find.byKey(const Key('usage-daily-chart')),
        );
        final painter = chart.painter! as UsageBarChartPainter;
        expect(painter.axisMaximum, greaterThan(0));
        expect(
          painter.barColor,
          brightness == Brightness.dark
              ? MuseColors.dark.accent
              : MuseColors.light.accent,
        );
        expect(
          painter.labelColor,
          brightness == Brightness.dark
              ? MuseColors.dark.muted
              : MuseColors.light.muted,
        );
        expect(tester.takeException(), isNull);
      },
    );
  }

  test(
    'chart chooses a finite readable scale for tiny and large token values',
    () {
      for (final tokens in [1, 8, 3500, 125000000]) {
        final painter = UsageBarChartPainter(
          daily: [
            {'total_tokens': tokens},
          ],
          barColor: Colors.blue,
          gridColor: Colors.grey,
          labelColor: Colors.black,
          textDirection: TextDirection.ltr,
        );
        expect(painter.axisMaximum.isFinite, isTrue);
        expect(painter.axisMaximum, greaterThanOrEqualTo(tokens));
        expect(painter.axisMaximum, lessThanOrEqualTo(tokens * 10));
      }
    },
  );
}
