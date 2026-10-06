import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:luma_client/api.dart';
import 'package:luma_client/preferences.dart';
import 'package:luma_client/theme.dart';
import 'package:luma_client/views/memory.dart';
import 'package:luma_client/views/proactive_settings.dart';
import 'package:luma_client/views/settings.dart';

class _SettingsClient extends http.BaseClient {
  _SettingsClient(this.handler);

  final http.StreamedResponse Function(http.Request) handler;

  @override
  Future<http.StreamedResponse> send(http.BaseRequest request) async =>
      handler(request as http.Request);
}

http.StreamedResponse _response(Object? body, {int status = 200}) =>
    http.StreamedResponse(
      http.ByteStream.fromBytes(
        body == null ? [] : utf8.encode(jsonEncode(body)),
      ),
      status,
      headers: {'content-type': 'application/json; charset=utf-8'},
    );

Map<String, dynamic> _prefs() => {
  'enabled': true,
  'max_per_day': 2,
  'window_start': '09:00',
  'window_end': '21:30',
  'timezone': 'Asia/Shanghai',
  'topics_like': '科技',
  'topics_avoid': '广告',
  'style': '简短',
  'feed_enabled': true,
  'feed_per_day': 1,
  'feed_instructions': '技术资讯',
};

void main() {
  testWidgets('proactive settings saves all fields with bounded counts', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(390, 1800);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.reset);
    Map<String, dynamic>? saved;
    final api = AssistantApi(
      client: _SettingsClient((request) {
        expect(request.url.path, '/api/v1/proactive/prefs');
        if (request.method == 'PUT') {
          saved = Map<String, dynamic>.from(jsonDecode(request.body));
          return _response(saved);
        }
        return _response(_prefs());
      }),
    );
    addTearDown(api.close);
    await tester.pumpWidget(
      MaterialApp(
        theme: lumaTheme,
        home: ProactiveSettingsView(api: api),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('09:00'), findsOneWidget);
    expect(find.text('21:30'), findsOneWidget);
    await tester.tap(find.byKey(const Key('proactive-enabled')));
    for (var index = 0; index < 3; index++) {
      await tester.tap(find.byKey(const Key('proactive-max-plus')));
      await tester.pump();
    }
    expect(
      tester.widget<Text>(find.byKey(const Key('proactive-max-value'))).data,
      '5',
    );
    expect(
      tester
          .widget<IconButton>(find.byKey(const Key('proactive-max-plus')))
          .onPressed,
      isNull,
    );
    await tester.enterText(
      find.byKey(const Key('proactive-topics-like')),
      '数据分析',
    );
    await tester.enterText(
      find.byKey(const Key('proactive-topics-avoid')),
      '营销',
    );
    await tester.enterText(find.byKey(const Key('proactive-style')), '先给结论');
    await tester.tap(find.byKey(const Key('proactive-feed-enabled')));
    await tester.tap(find.byKey(const Key('proactive-feed-max-plus')));
    await tester.enterText(
      find.byKey(const Key('proactive-feed-instructions')),
      '每篇提供原文来源',
    );
    await tester.tap(find.byKey(const Key('proactive-save')));
    await tester.pumpAndSettle();

    expect(saved, {
      'enabled': false,
      'max_per_day': 5,
      'window_start': '09:00',
      'window_end': '21:30',
      'timezone': 'Asia/Shanghai',
      'topics_like': '数据分析',
      'topics_avoid': '营销',
      'style': '先给结论',
      'feed_enabled': false,
      'feed_per_day': 2,
      'feed_instructions': '每篇提供原文来源',
    });
    expect(find.text('主动消息设置已保存'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('time picker and timezone save contract values', (tester) async {
    tester.view.physicalSize = const Size(390, 1800);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.reset);
    Map<String, dynamic>? saved;
    final api = AssistantApi(
      client: _SettingsClient((request) {
        if (request.method == 'PUT') {
          saved = Map<String, dynamic>.from(jsonDecode(request.body));
          return _response(saved);
        }
        return _response(_prefs());
      }),
    );
    addTearDown(api.close);
    await tester.pumpWidget(
      MaterialApp(
        theme: lumaTheme,
        home: ProactiveSettingsView(api: api),
      ),
    );
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('proactive-window-start')));
    await tester.pumpAndSettle();
    await tester.tap(find.byIcon(Icons.keyboard_outlined));
    await tester.pumpAndSettle();
    final dialogFields = find.descendant(
      of: find.byType(TimePickerDialog),
      matching: find.byType(TextField),
    );
    await tester.enterText(dialogFields.at(0), '10');
    await tester.enterText(dialogFields.at(1), '15');
    await tester.tap(find.text('OK'));
    await tester.pumpAndSettle();
    await tester.tap(find.byType(DropdownButtonFormField<String>));
    await tester.pumpAndSettle();
    await tester.tap(find.text('UTC').last);
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('proactive-save')));
    await tester.pumpAndSettle();

    expect(saved?['window_start'], '10:15');
    expect(saved?['timezone'], 'UTC');
    expect(tester.takeException(), isNull);
  });

  testWidgets(
    'legacy proactive prefs show an unavailable state without fake save',
    (tester) async {
      var puts = 0;
      final api = AssistantApi(
        client: _SettingsClient((request) {
          if (request.method == 'PUT') puts++;
          return _response(null, status: 404);
        }),
      );
      addTearDown(api.close);
      await tester.pumpWidget(
        MaterialApp(
          theme: lumaTheme,
          home: ProactiveSettingsView(api: api),
        ),
      );
      await tester.pumpAndSettle();
      expect(find.text('当前服务器暂不支持主动消息设置'), findsOneWidget);
      await tester.scrollUntilVisible(
        find.byKey(const Key('proactive-save')),
        300,
        scrollable: find
            .descendant(
              of: find.byKey(const Key('proactive-settings-list')),
              matching: find.byType(Scrollable),
            )
            .first,
      );
      expect(
        tester
            .widget<FilledButton>(find.byKey(const Key('proactive-save')))
            .onPressed,
        isNull,
      );
      expect(puts, 0);
    },
  );

  testWidgets('memory lives in settings and refreshes only memories', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(390, 1800);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.reset);
    final paths = <String>[];
    Map<String, String>? memoryQuery;
    final api = AssistantApi(
      client: _SettingsClient((request) {
        paths.add(request.url.path);
        if (request.url.path == '/api/v1/memories') {
          memoryQuery = request.url.queryParameters;
          return _response([
            {'id': 'mem-1', 'content': '刷新后的记忆', 'category': 'fact'},
          ]);
        }
        return _response(null, status: 404);
      }),
    );
    addTearDown(api.close);
    final preferences = LumaPreferences();
    addTearDown(preferences.dispose);
    var refreshes = 0;
    await tester.pumpWidget(
      LumaPreferencesScope(
        preferences: preferences,
        child: MaterialApp(
          theme: lumaTheme,
          home: SettingsView(
            api: api,
            memories: const [
              {'id': 'mem-1', 'content': '已有记忆', 'category': 'fact'},
            ],
            onRefresh: () async => refreshes++,
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();
    await tester.scrollUntilVisible(
      find.byKey(const Key('settings-memory')),
      300,
    );
    await tester.tap(find.byKey(const Key('settings-memory')));
    await tester.pumpAndSettle();
    expect(find.byType(MemoryView), findsOneWidget);
    expect(find.text('已有记忆'), findsOneWidget);
    expect(paths, isNot(contains('/api/v1/files')));
    final refresh = tester
        .state<RefreshIndicatorState>(find.byType(RefreshIndicator))
        .show();
    await tester.pumpAndSettle();
    await refresh;
    await tester.pumpAndSettle();
    expect(find.text('刷新后的记忆'), findsOneWidget);
    expect(refreshes, 1);
    expect(memoryQuery, {'limit': '200'});
    expect(paths.where((path) => path == '/api/v1/memories'), hasLength(1));
    expect(paths, isNot(contains('/api/v1/files')));
  });
}
