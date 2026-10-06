import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:luma_client/api.dart';
import 'package:luma_client/home.dart';
import 'package:luma_client/theme.dart';
import 'package:luma_client/views/chat.dart';

class _ConfirmationApi extends AssistantApi {
  _ConfirmationApi(this.action);

  final String action;
  final events = StreamController<AssistantSseEvent>.broadcast();
  final sent = <Map<String, dynamic>>[];
  var dashboardCalls = 0;

  Map<String, dynamic> get confirmation => {
    'id': 'confirmation-1',
    'type': 'confirm',
    'spec': {'type': 'confirm', 'title': '删除记录'},
    'state': {'status': action == 'cancel' ? 'cancelled' : 'done'},
  };

  @override
  Future<Map<String, dynamic>?> dashboard({String? selectedSessionId}) async {
    if (dashboardCalls++ > 0) return null;
    return {
      'conversation_id': 'session-1',
      'messages': [
        {'id': 'user-1', 'role': 'user', 'content': '删除这条记录'},
        {
          'id': 'assistant-1',
          'role': 'assistant',
          'content': '请确认删除',
          'status': 'complete',
          'metadata': {
            'widgets': [
              {
                ...confirmation,
                'state': {'status': 'pending'},
              },
            ],
          },
        },
        {'id': 'user-2', 'role': 'user', 'content': '另一个问题'},
        {
          'id': 'assistant-2',
          'role': 'assistant',
          'content': '另一条回复',
          'status': 'complete',
        },
      ],
      'tasks': const [],
      'memories': const [],
      'runtime_jobs': const [],
      'approvals': const [],
      'runtime_activity': const [],
    };
  }

  @override
  Future<List<Map<String, dynamic>>> listNotifications({
    bool unreadOnly = false,
    int limit = 100,
  }) async => [];

  @override
  Future<Map<String, dynamic>> widgetEvent(
    String widgetId,
    String action,
    Object? value,
  ) async => {'widget': confirmation, 'message': '已确认执行删除'};

  @override
  Future<Stream<AssistantSseEvent>> streamMessage(
    String sessionId,
    String content, {
    Map<String, dynamic>? metadata,
  }) async {
    sent.add({'content': content, 'metadata': metadata});
    return events.stream;
  }
}

ChatView _chat(WidgetTester tester) =>
    tester.widget<ChatView>(find.byType(ChatView));

void main() {
  for (final action in ['confirm', 'cancel']) {
    testWidgets('$action continues the original assistant without extra rows', (
      tester,
    ) async {
      final api = _ConfirmationApi(action);
      await tester.pumpWidget(
        MaterialApp(
          theme: lumaTheme,
          home: LumaHome(api: api),
        ),
      );
      await tester.pumpAndSettle();

      unawaited(_chat(tester).onWidgetEvent('confirmation-1', action, null));
      await tester.pump();
      expect(_chat(tester).messages, hasLength(4));
      expect(
        _chat(tester).messages.where((message) => message['role'] == 'user'),
        hasLength(2),
      );
      expect(_chat(tester).messages[1]['streaming'], isTrue);
      expect(api.sent.single['metadata'], {
        'widget_event': {'widget_id': 'confirmation-1', 'action': action},
      });

      api.events.add(
        const AssistantSseEvent('start', {
          'message_id': 'assistant-1',
          'user_message': {
            'id': 'user-1',
            'role': 'user',
            'content': '删除这条记录（已保存）',
          },
        }),
      );
      api.events.add(const AssistantSseEvent('delta', {'content': '操作'}));
      await tester.pump();
      expect(_chat(tester).messages, hasLength(4));
      expect(_chat(tester).messages[0]['content'], '删除这条记录（已保存）');
      expect(_chat(tester).messages[1]['content'], '操作');
      expect(_chat(tester).messages[2]['content'], '另一个问题');
      expect(_chat(tester).messages[3]['content'], '另一条回复');

      api.events.add(
        const AssistantSseEvent('done', {
          'id': 'assistant-1',
          'content': '操作结束',
          'status': 'complete',
        }),
      );
      await api.events.close();
      await tester.pumpAndSettle();
      expect(_chat(tester).messages, hasLength(4));
      expect(_chat(tester).messages[1]['content'], '操作结束');
      expect(_chat(tester).messages[1]['streaming'], isFalse);
      expect(
        _chat(tester).messages
            .where((message) => message['id'] == 'assistant-1'),
        hasLength(1),
      );
      await tester.pumpWidget(const SizedBox.shrink());
    });
  }
}
