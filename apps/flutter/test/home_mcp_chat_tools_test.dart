import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:luma_client/api.dart';
import 'package:luma_client/home.dart';
import 'package:luma_client/mcp_utils.dart';
import 'package:luma_client/theme.dart';
import 'package:luma_client/views/chat.dart';

const _secret = 'chat-tools-private-fixture';
const _reference = '{{secret:sec_0123456789abcdef}}';
const _config =
    '{"mcpServers":{"sample-data":{"type":"http","url":"https://example.com/mcp","headers":{"Authorization":"Bearer $_secret"}}}}';
const _saved =
    '{"mcpServers":{"sample-data":{"type":"http","url":"https://example.com/mcp","headers":{"Authorization":"$_reference"}}}}';
const _toolEvent = <String, dynamic>{
  'call_id': 'call-1',
  'tool': 'luma.connectors.add_mcp',
  'title': '连接 MCP',
  'connector': 'Luma',
  'status': 'ok',
};

class _ChatToolsApi extends AssistantApi {
  final events = StreamController<AssistantSseEvent>.broadcast();
  final sent = <String>[];
  var dashboardCalls = 0;
  var failSend = false;
  List<Map<String, dynamic>> initialMessages = [];

  @override
  Future<Map<String, dynamic>?> dashboard({String? selectedSessionId}) async {
    if (dashboardCalls++ > 0) return null;
    return {
      'conversation_id': 'main',
      'messages': initialMessages,
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
  Future<Stream<AssistantSseEvent>> streamMessage(
    String sessionId,
    String content, {
    Map<String, dynamic>? metadata,
  }) async {
    sent.add(content);
    if (failSend) {
      throw const AssistantApiException('transport error $_secret');
    }
    return events.stream;
  }

  @override
  Future<Stream<AssistantSseEvent>> streamMessageAfter(
    String messageId, {
    String? after,
  }) async => events.stream;
}

Future<void> _mount(WidgetTester tester, _ChatToolsApi api) async {
  tester.view.physicalSize = const Size(390, 844);
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.reset);
  await tester.pumpWidget(
    MaterialApp(
      theme: lumaTheme,
      home: LumaHome(api: api),
    ),
  );
  // Resumed replies intentionally keep their loading animation running.
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 400));
  await tester.pump();
}

ChatView _chat(WidgetTester tester) =>
    tester.widget<ChatView>(find.byType(ChatView));

void main() {
  test(
    'secret JSON is hidden optimistically while ordinary JSON is unchanged',
    () {
      for (final text in [
        _config,
        '```json\n$_config\n```',
        '帮我配置 $_config 然后查询',
        '{"headers":{"Authorization":"$_secret"',
        _config.replaceFirst('headers', r'head\u0065rs'),
      ]) {
        expect(optimisticChatContent(text), isNot(contains(_secret)));
      }
      for (final key in [
        'headers',
        'env',
        'Authorization',
        'token',
        'apiKey',
        'api_key',
        'PASSWORD',
        'secret',
        'access_token',
        'accessKey',
        'client-secret',
        'API-KEY',
      ]) {
        expect(
          optimisticChatContent('保存 {"$key":"$_secret"} 然后继续'),
          isNot(contains(_secret)),
        );
      }
      for (final text in [
        '{"answer":42}',
        '{"mcpServers":{"public":{"url":"https://example.com/mcp"}}}',
        'hello mcpServers world',
      ]) {
        expect(optimisticChatContent(text), text);
      }
    },
  );

  test(
    'display masking preserves JSON names, addresses, and the stored reference',
    () {
      final display = displaySecretReferences('帮我配置 $_saved 谢谢');
      expect(display, contains('••••••（已加密保存）'));
      expect(display, contains('sample-data'));
      expect(display, contains('https://example.com/mcp'));
      expect(display, isNot(contains(_reference)));
      expect(_saved, contains(_reference));
      expect(displaySecretReferences('{"answer":42}'), '{"answer":42}');
    },
  );

  testWidgets('server content replaces safe optimism and clears undo history', (
    tester,
  ) async {
    final api = _ChatToolsApi();
    await _mount(tester, api);
    await tester.enterText(find.byKey(const ValueKey('chat-input')), _config);
    await tester.pump(const Duration(milliseconds: 600));
    final previousEditable = tester.state(find.byType(EditableText));
    await tester.tap(find.byTooltip('发送'));
    await tester.pump();

    expect(api.sent, [_config]);
    expect(find.byType(AlertDialog), findsNothing);
    expect(_chat(tester).messages.toString(), isNot(contains(_secret)));
    expect(_chat(tester).input.text, isEmpty);
    expect(tester.state(find.byType(EditableText)), isNot(previousEditable));
    final undo = tester.state<UndoHistoryState<TextEditingValue>>(
      find.byType(UndoHistory<TextEditingValue>),
    );
    undo.undo();
    undo.redo();
    expect(_chat(tester).input.text, isEmpty);

    api.events.add(
      const AssistantSseEvent('start', {
        'message_id': 'assistant-1',
        'user_message': {'id': 'user-1', 'role': 'user', 'content': _saved},
      }),
    );
    api.events.add(const AssistantSseEvent('tool', _toolEvent, 'event-1'));
    await tester.pump();
    expect(_chat(tester).messages.first['content'], _saved);
    expect(find.text(displaySecretReferences(_saved)), findsOneWidget);
    expect(find.textContaining(_reference), findsNothing);
    expect(find.text('已查询 Luma · 连接 MCP'), findsNothing);
    expect(find.byKey(const ValueKey('chat-tool-progress')), findsNothing);
    expect(_chat(tester).messages.last['tool_states'], [_toolEvent]);

    api.events.add(
      const AssistantSseEvent('done', {
        'content': '已连接 sample-data，可以开始查询了。',
        'status': 'complete',
        'metadata': {
          'tool_calls': [_toolEvent],
        },
      }),
    );
    await api.events.close();
    await tester.pumpAndSettle();
    expect(find.text('已连接 sample-data，可以开始查询了。'), findsOneWidget);
    expect(_chat(tester).messages.toString(), isNot(contains(_secret)));
    await tester.pumpWidget(const SizedBox.shrink());
  });

  testWidgets(
    'failed sends keep secrets out of errors, drafts, and retry content',
    (tester) async {
      final api = _ChatToolsApi()..failSend = true;
      await _mount(tester, api);
      await tester.enterText(find.byKey(const ValueKey('chat-input')), _config);
      await tester.pump();
      await tester.tap(find.byTooltip('发送'));
      await tester.pumpAndSettle();

      expect(_chat(tester).input.text, isEmpty);
      expect(_chat(tester).messages.toString(), isNot(contains(_secret)));
      expect(find.text('敏感信息未确认保存，请重新粘贴后重试'), findsOneWidget);
      final retry = _chat(tester).onRetry!(_chat(tester).messages.last);
      await tester.pumpAndSettle();
      await retry;
      expect(api.sent.last, isNot(contains(_secret)));
      await tester.pumpWidget(const SizedBox.shrink());
      await api.events.close();
    },
  );

  testWidgets(
    'live browser URL stays in memory tool state after durable completion metadata arrives',
    (tester) async {
      final api = _ChatToolsApi();
      await _mount(tester, api);
      await tester.enterText(
        find.byKey(const ValueKey('chat-input')),
        '打开实时画面',
      );
      await tester.pump();
      await tester.tap(find.byTooltip('发送'));
      await tester.pump();
      api.events.add(
        const AssistantSseEvent('start', {'message_id': 'assistant-live'}),
      );
      const live = {
        'call_id': 'live-1',
        'tool': 'browser.live',
        'status': 'ok',
        'data': {
          'kind': 'browser_live',
          'url': 'https://view.tencentags.com/?access_token=temporary-fixture',
        },
      };
      api.events.add(const AssistantSseEvent('tool', live));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 400));
      expect(find.text('观看实时画面'), findsOneWidget);
      api.events.add(
        const AssistantSseEvent('done', {
          'content': '已为用户打开实时画面',
          'status': 'complete',
          'metadata': {
            'tool_calls': [
              {'call_id': 'live-1', 'tool': 'browser.live', 'status': 'ok'},
            ],
          },
        }),
      );
      await api.events.close();
      await tester.pumpAndSettle();
      final message = _chat(tester).messages.last;
      expect(find.text('观看实时画面'), findsOneWidget);
      expect(message['tool_states'], [live]);
      expect('${message['content']}', isNot(contains('access_token')));
      expect('${message['metadata']}', isNot(contains('access_token')));
      expect(find.textContaining('temporary-fixture'), findsNothing);
      await tester.pumpWidget(const SizedBox.shrink());
    },
  );

  testWidgets(
    'resumed tool calls retain metadata and hide completed progress',
    (tester) async {
      final api = _ChatToolsApi()
        ..initialMessages = [
          {'id': 'user-1', 'role': 'user', 'content': _saved},
          {
            'id': 'assistant-1',
            'role': 'assistant',
            'content': '',
            'status': 'streaming',
            'metadata': {'provider': 'fixture'},
          },
        ];
      await _mount(tester, api);
      expect(api.events.hasListener, isTrue);
      api.events.add(const AssistantSseEvent('tool', _toolEvent));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 400));
      expect(find.text('已查询 Luma · 连接 MCP'), findsNothing);
      expect(find.byKey(const ValueKey('chat-tool-progress')), findsNothing);
      expect(_chat(tester).messages.last['tool_states'], [_toolEvent]);
      api.events.add(
        const AssistantSseEvent('done', {
          'content': '连接器已经配好了。',
          'status': 'complete',
          'metadata': {
            'tool_calls': [_toolEvent],
          },
        }),
      );
      await api.events.close();
      await tester.pumpAndSettle();
      expect(find.text('连接器已经配好了。'), findsOneWidget);
      expect(find.textContaining(_reference), findsNothing);
      expect(_chat(tester).messages.last['metadata']['provider'], 'fixture');
      await tester.pumpWidget(const SizedBox.shrink());
    },
  );

  for (final width in [390.0, 1200.0]) {
    testWidgets(
      'references render masked in user and assistant bubbles at $width',
      (tester) async {
        tester.view.physicalSize = Size(width, 844);
        tester.view.devicePixelRatio = 1;
        addTearDown(tester.view.reset);
        final input = TextEditingController();
        final scroll = ScrollController();
        addTearDown(input.dispose);
        addTearDown(scroll.dispose);
        await tester.pumpWidget(
          MaterialApp(
            theme: lumaTheme,
            home: Scaffold(
              body: ChatView(
                messages: [
                  {'role': 'user', 'content': _saved},
                  {'role': 'assistant', 'content': '引用 $_reference 已保管。'},
                ],
                tasks: const [],
                memories: const [],
                sending: false,
                input: input,
                scrollController: scroll,
                onRefresh: () async {},
                onSend: () {},
                onWidgetEvent: (id, action, value) async {},
              ),
            ),
          ),
        );
        await tester.pumpAndSettle();
        expect(find.textContaining('••••••（已加密保存）'), findsNWidgets(2));
        expect(find.textContaining(_reference), findsNothing);
        expect(find.text(displaySecretReferences(_saved)), findsOneWidget);
      },
    );
  }
}
