import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:luma_client/api.dart';
import 'package:luma_client/home.dart';
import 'package:luma_client/theme.dart';

class _FakeApi extends AssistantApi {
  _FakeApi({required this.dashboardBuilder, required this.streamBuilder})
    : super();

  final Map<String, dynamic> Function() dashboardBuilder;
  final Stream<AssistantSseEvent> Function(String? after) streamBuilder;
  int dashboardCalls = 0;
  int resumeCalls = 0;

  @override
  Future<Map<String, dynamic>?> dashboard({String? selectedSessionId}) async {
    dashboardCalls++;
    return dashboardBuilder();
  }

  @override
  Future<Stream<AssistantSseEvent>> streamMessageAfter(
    String messageId, {
    String? after,
  }) async {
    resumeCalls++;
    return streamBuilder(after);
  }
}

Map<String, dynamic> _dashboard({
  required String status,
  String content = '你好',
}) => {
  'conversation_id': 'session-1',
  'messages': [
    {'id': 'user-1', 'role': 'user', 'content': '问题'},
    {
      'id': 'assistant-1',
      'role': 'assistant',
      'content': content,
      'status': status,
      'streaming': status == 'streaming',
    },
  ],
  'tasks': const [],
  'memories': const [],
  'runtime_jobs': const [],
  'approvals': const [],
  'runtime_activity': const [],
};

Widget _shell(Widget child) => MaterialApp(theme: lumaTheme, home: child);

void main() {
  testWidgets('无游标重放的 delta 不会追加数据库已有内容', (tester) async {
    var dashboardCalls = 0;
    final api = _FakeApi(
      dashboardBuilder: () {
        dashboardCalls++;
        return _dashboard(
          status: dashboardCalls == 1 ? 'streaming' : 'complete',
          content: dashboardCalls == 1 ? '你好' : '你好世界',
        );
      },
      streamBuilder: (_) => Stream.fromIterable([
        const AssistantSseEvent('delta', {'content': '你好'}, 'evt-1'),
        const AssistantSseEvent('delta', {'content': '世界'}, 'evt-2'),
        const AssistantSseEvent('done', {'status': 'complete'}, 'evt-3'),
      ]),
    );
    await tester.pumpWidget(
      _shell(LumaHome(api: api, resumeBackoff: (_) => Duration.zero)),
    );
    await tester.pumpAndSettle();

    expect(api.resumeCalls, 1);
    expect(find.text('你好世界'), findsOneWidget);
    expect(find.text('你好你好世界'), findsNothing);
  });

  testWidgets('续读失败时自动连接次数限制为一轮三次', (tester) async {
    final api = _FakeApi(
      dashboardBuilder: () => _dashboard(status: 'streaming'),
      streamBuilder: (_) => const Stream<AssistantSseEvent>.empty(),
    );
    await tester.pumpWidget(
      _shell(LumaHome(api: api, resumeBackoff: (_) => Duration.zero)),
    );
    await tester.pumpAndSettle();

    expect(api.resumeCalls, 3);
    expect(find.text('生成可能已中断'), findsOneWidget);
    expect(find.text('重试'), findsOneWidget);
  });
}
