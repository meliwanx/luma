import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:luma_client/api.dart';
import 'package:luma_client/home.dart';
import 'package:luma_client/theme.dart';
import 'package:luma_client/views/chat.dart';
import 'package:luma_client/views/mobile_shell.dart';

class _SessionApi extends AssistantApi {
  final sessions = <Map<String, dynamic>>[
    {'id': 'main', 'kind': 'main', 'title': '主聊天'},
    {'id': 'side', 'kind': 'side', 'title': '旅行'},
    {'id': 'other', 'kind': 'side', 'title': '工作'},
  ];
  final stored = <String, List<Map<String, dynamic>>>{};
  final streams = <String, StreamController<AssistantSseEvent>>{};
  final resumed = <String, StreamController<AssistantSseEvent>>{};
  final resumeCursors = <String, String?>{};
  final cancelled = <String>[];
  final deleted = <String>[];
  final renamed = <String, String>{};
  final polled = <String>[];
  bool failDelete = false;
  bool failRename = false;
  bool rejectSend = false;
  bool hidePendingFromDashboard = false;
  Completer<Stream<AssistantSseEvent>>? delayedStart;
  Completer<Map<String, dynamic>?>? delayedDashboard;

  @override
  Future<Map<String, dynamic>?> dashboard({String? selectedSessionId}) async {
    final delayed = delayedDashboard;
    if (delayed != null) {
      delayedDashboard = null;
      return delayed.future;
    }
    final id = selectedSessionId ?? 'main';
    return {
      'conversation_id': id,
      'sessions': [
        for (final session in sessions) {...session},
      ],
      'messages': [
        for (final message
            in (hidePendingFromDashboard && id == 'main'
                ? <Map<String, dynamic>>[]
                : stored[id] ?? <Map<String, dynamic>>[]))
          {...message},
      ],
    };
  }

  @override
  Future<Stream<AssistantSseEvent>> streamMessage(
    String sessionId,
    String content, {
    Map<String, dynamic>? metadata,
  }) async {
    if (rejectSend) {
      throw const AssistantApiException('同时进行的回复太多，请稍后', statusCode: 429);
    }
    stored[sessionId] = [
      {'id': 'user-$sessionId', 'role': 'user', 'content': content},
      {
        'id': 'reply-$sessionId',
        'role': 'assistant',
        'content': '',
        'status': 'streaming',
      },
    ];
    final controller = streams.putIfAbsent(sessionId, StreamController.new);
    if (delayedStart != null) return delayedStart!.future;
    controller.add(
      AssistantSseEvent('start', {'message_id': 'reply-$sessionId'}),
    );
    return controller.stream;
  }

  void delta(String id, String content, String cursor) {
    stored[id]!.last['content'] = content;
    streams[id]!.add(AssistantSseEvent('delta', {'content': content}, cursor));
  }

  void complete(String id) {
    stored[id]!.last.addAll({'content': '$id完成', 'status': 'complete'});
  }

  @override
  Future<Stream<AssistantSseEvent>> streamMessageAfter(
    String messageId, {
    String? after,
  }) async {
    resumeCursors[messageId] = after;
    return resumed.putIfAbsent(messageId, StreamController.new).stream;
  }

  @override
  Future<MessagePage> listMessages(
    String sessionId, {
    int limit = 100,
    String? before,
  }) async {
    polled.add(sessionId);
    return MessagePage(
      messages: [
        for (final message in stored[sessionId] ?? <Map<String, dynamic>>[])
          {...message},
      ],
      hasMore: false,
    );
  }

  @override
  Future<void> cancelMessage(String messageId) async {
    cancelled.add(messageId);
    for (final messages in stored.values) {
      for (final message in messages) {
        if (message['id'] == messageId) message['status'] = 'incomplete';
      }
    }
  }

  @override
  Future<void> deleteSession(String id) async {
    if (failDelete) throw const AssistantApiException('删除失败');
    deleted.add(id);
    sessions.removeWhere((session) => session['id'] == id);
  }

  @override
  Future<Map<String, dynamic>> renameSession(String id, String title) async {
    if (failRename) throw const AssistantApiException('重命名失败');
    renamed[id] = title;
    final session = sessions.firstWhere((session) => session['id'] == id);
    session['title'] = title;
    return {...session};
  }

  @override
  void close() {
    for (final controller in [...streams.values, ...resumed.values]) {
      unawaited(controller.close());
    }
    super.close();
  }
}

Future<void> _frames(WidgetTester tester) async {
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 400));
  await tester.pump();
}

Future<void> _mount(
  WidgetTester tester,
  _SessionApi api, {
  DateTime Function()? generationNow,
}) async {
  tester.view.physicalSize = const Size(390, 844);
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.reset);
  await tester.pumpWidget(
    MaterialApp(
      theme: lumaTheme,
      home: generationNow == null
          ? LumaHome(api: api)
          : LumaHome(api: api, generationNow: generationNow),
    ),
  );
  await _frames(tester);
}

ChatView _chat(WidgetTester tester) => tester.widget(find.byType(ChatView));

Future<void> _menu(WidgetTester tester) async {
  await tester.tap(find.byKey(const ValueKey('mobile-title-button')));
  await _frames(tester);
}

Future<void> _select(WidgetTester tester, String id) async {
  await _menu(tester);
  await tester.tap(
    id == 'main' ? find.text('主要聊天') : find.byKey(ValueKey('menu-session-$id')),
  );
  await _frames(tester);
}

Future<void> _send(WidgetTester tester, String text) async {
  await tester.enterText(find.byKey(const ValueKey('chat-input')), text);
  await tester.pump();
  await tester.tap(find.byTooltip('发送'));
  await _frames(tester);
}

void main() {
  testWidgets('尚未收到消息ID时停止，收到start后才取消云端生成', (tester) async {
    final api = _SessionApi()
      ..delayedStart = Completer<Stream<AssistantSseEvent>>();
    await _mount(tester, api);
    await _send(tester, '慢连接停止');
    await tester.tap(find.byTooltip('停止'));
    await _frames(tester);
    expect(api.cancelled, isEmpty);
    expect(_chat(tester).sending, isTrue);
    api.delayedStart!.complete(api.streams['main']!.stream);
    api.streams['main']!.add(
      const AssistantSseEvent('start', {'message_id': 'reply-main'}),
    );
    await _frames(tester);
    expect(api.cancelled, ['reply-main']);
    expect(_chat(tester).sending, isFalse);
    expect(api.streams['main']!.hasListener, isFalse);
    await tester.pumpWidget(const SizedBox.shrink());
  });

  testWidgets('切走再切回尚无消息ID的会话，停止仍取消该会话', (tester) async {
    final api = _SessionApi()
      ..hidePendingFromDashboard = true
      ..delayedStart = Completer<Stream<AssistantSseEvent>>();
    await _mount(tester, api);
    await _send(tester, '切换后停止');
    await _select(tester, 'side');
    await _select(tester, 'main');
    await tester.tap(find.byTooltip('停止'));
    await _frames(tester);
    expect(api.cancelled, isEmpty);
    api.delayedStart!.complete(api.streams['main']!.stream);
    api.streams['main']!.add(
      const AssistantSseEvent('start', {'message_id': 'reply-main'}),
    );
    await _frames(tester);
    expect(api.cancelled, ['reply-main']);
    expect(_chat(tester).sending, isFalse);
    expect(api.streams['main']!.hasListener, isFalse);
    await tester.pumpWidget(const SizedBox.shrink());
  });

  testWidgets('切换只断开订阅，另一会话可发送且返回时带游标续接', (tester) async {
    final api = _SessionApi();
    await _mount(tester, api);
    await _send(tester, '主聊天问题');
    api.delta('main', '前半段', 'cursor-1');
    await _frames(tester);
    await _select(tester, 'side');
    expect(api.cancelled, isEmpty);
    expect(api.streams['main']!.hasListener, isFalse);
    expect(_chat(tester).sending, isFalse);
    await _send(tester, '旁聊问题');
    expect(_chat(tester).sending, isTrue);
    await _menu(tester);
    expect(
      find.byKey(const ValueKey('session-generating-main')),
      findsOneWidget,
    );
    expect(
      find.byKey(const ValueKey('session-generating-side')),
      findsOneWidget,
    );
    await tester.tap(find.text('主要聊天'));
    await _frames(tester);
    expect(api.resumeCursors['reply-main'], 'cursor-1');
    api.resumed['reply-main']!.add(
      const AssistantSseEvent('delta', {'content': '后半段'}, 'cursor-2'),
    );
    await _frames(tester);
    expect(find.text('前半段后半段'), findsOneWidget);
    expect(api.cancelled, isEmpty);
    await tester.pumpWidget(const SizedBox.shrink());
  });

  testWidgets('后台完成显示菜单和主聊天提醒，不覆盖当前消息，打开后清除提醒', (tester) async {
    final api = _SessionApi();
    await _mount(tester, api);
    await _send(tester, '主聊天问题');
    await _select(tester, 'side');
    api.stored['side'] = [
      {'id': 'side-history', 'role': 'user', 'content': '旁聊历史'},
    ];
    api.complete('main');
    await tester.pump(const Duration(seconds: 5));
    await _frames(tester);
    expect(api.polled, contains('main'));
    expect(find.text('旁聊历史'), findsOneWidget);
    expect(find.text('main完成'), findsNothing);
    expect(find.byKey(const ValueKey('mobile-main-completed')), findsOneWidget);
    await _menu(tester);
    expect(
      find.byKey(const ValueKey('session-completed-main')),
      findsOneWidget,
    );
    await tester.tap(find.text('主要聊天'));
    await _frames(tester);
    expect(find.text('main完成'), findsOneWidget);
    expect(find.byKey(const ValueKey('mobile-main-completed')), findsNothing);
    await tester.pumpWidget(const SizedBox.shrink());
  });

  testWidgets('pending消息自动续接，停止只取消当前会话', (tester) async {
    final api = _SessionApi();
    api.stored['side'] = [
      {
        'id': 'reply-side',
        'role': 'assistant',
        'content': '',
        'status': 'pending',
      },
    ];
    await _mount(tester, api);
    await _send(tester, '后台主聊天');
    await _select(tester, 'side');
    expect(api.resumeCursors, contains('reply-side'));
    await tester.tap(find.byTooltip('停止'));
    await _frames(tester);
    expect(api.cancelled, ['reply-side']);
    await tester.pumpWidget(const SizedBox.shrink());
  });

  testWidgets('POST尚未收到start时切走，收到消息ID后释放订阅并跟踪后台', (tester) async {
    final api = _SessionApi()
      ..delayedStart = Completer<Stream<AssistantSseEvent>>();
    await _mount(tester, api);
    await _send(tester, '慢连接');
    await _select(tester, 'side');
    api.delayedStart!.complete(api.streams['main']!.stream);
    api.streams['main']!.add(
      const AssistantSseEvent('start', {'message_id': 'reply-main'}),
    );
    await _frames(tester);
    expect(api.streams['main']!.hasListener, isFalse);
    expect(_chat(tester).sending, isFalse);
    api.complete('main');
    await tester.pump(const Duration(seconds: 5));
    await _frames(tester);
    expect(find.byKey(const ValueKey('mobile-main-completed')), findsOneWidget);
    expect(api.cancelled, isEmpty);
    await tester.pumpWidget(const SizedBox.shrink());
  });

  testWidgets('后台状态轮询最多十分钟', (tester) async {
    final api = _SessionApi();
    var now = DateTime(2026, 10, 5);
    await _mount(tester, api, generationNow: () => now);
    await _send(tester, '长任务');
    await _select(tester, 'side');
    now = now.add(const Duration(minutes: 11));
    await tester.pump(const Duration(seconds: 5));
    await _frames(tester);
    final calls = api.polled.length;
    await tester.pump(const Duration(seconds: 10));
    await _frames(tester);
    expect(api.polled.length, calls);
    await tester.pumpWidget(const SizedBox.shrink());
  });

  testWidgets('发送前启动的迟到刷新不会覆盖已完成的新回复', (tester) async {
    final api = _SessionApi();
    await _mount(tester, api);
    final stale = Completer<Map<String, dynamic>?>();
    api.delayedDashboard = stale;
    await tester.pump(const Duration(seconds: 5));
    await _frames(tester);
    await _send(tester, '刷新竞态');
    api.complete('main');
    api.streams['main']!.add(
      const AssistantSseEvent('done', {
        'content': 'main完成',
        'status': 'complete',
      }),
    );
    unawaited(api.streams['main']!.close());
    await _frames(tester);
    stale.complete({
      'conversation_id': 'main',
      'sessions': api.sessions,
      'messages': <Map<String, dynamic>>[],
    });
    await _frames(tester);
    expect(find.text('main完成'), findsOneWidget);
    expect(_chat(tester).sending, isFalse);
    await tester.pumpWidget(const SizedBox.shrink());
  });

  testWidgets('旁聊⋯删除二次确认后调用API并立即移除当前旁聊', (tester) async {
    final api = _SessionApi();
    await _mount(tester, api);
    await _select(tester, 'side');
    await _send(tester, '正在生成也能删除');
    await _menu(tester);
    await tester.tap(find.byKey(const ValueKey('menu-session-actions-side')));
    await _frames(tester);
    await tester.tap(find.byKey(const ValueKey('session-action-delete')));
    await _frames(tester);
    expect(api.deleted, isEmpty);
    expect(find.text('删除旁聊？'), findsOneWidget);
    await tester.tap(find.widgetWithText(FilledButton, '删除'));
    await _frames(tester);
    expect(api.deleted, ['side']);
    final shell = tester.widget<MobileShell>(find.byType(MobileShell));
    expect(shell.sessions.length, 2);
    expect(shell.sessionId, 'main');
    expect(api.cancelled, isEmpty);
    await tester.pumpWidget(const SizedBox.shrink());
  });

  testWidgets('旁聊⋯重命名保存后调用API并立即刷新列表', (tester) async {
    final api = _SessionApi();
    await _mount(tester, api);
    await _menu(tester);
    await tester.tap(find.byKey(const ValueKey('menu-session-actions-side')));
    await _frames(tester);
    await tester.tap(find.byKey(const ValueKey('session-action-rename')));
    await _frames(tester);
    await tester.enterText(
      find.byKey(const ValueKey('rename-session-input')),
      '新旅行',
    );
    await tester.tap(find.widgetWithText(FilledButton, '保存'));
    await _frames(tester);
    expect(api.renamed['side'], '新旅行');
    expect(find.text('新旅行'), findsOneWidget);
    expect(find.text('旅行'), findsNothing);
    await tester.pumpWidget(const SizedBox.shrink());
  });

  testWidgets('删除与重命名失败显示错误且保留列表', (tester) async {
    final api = _SessionApi()..failDelete = true;
    await _mount(tester, api);
    await _menu(tester);
    await tester.tap(find.byKey(const ValueKey('menu-session-actions-side')));
    await _frames(tester);
    await tester.tap(find.byKey(const ValueKey('session-action-delete')));
    await _frames(tester);
    await tester.tap(find.widgetWithText(FilledButton, '删除'));
    await _frames(tester);
    expect(find.text('删除失败'), findsOneWidget);
    expect(
      tester.widget<MobileShell>(find.byType(MobileShell)).sessions.length,
      3,
    );
    api.failRename = true;
    await tester.tap(find.byKey(const ValueKey('menu-session-actions-side')));
    await _frames(tester);
    await tester.tap(find.byKey(const ValueKey('session-action-rename')));
    await _frames(tester);
    await tester.enterText(
      find.byKey(const ValueKey('rename-session-input')),
      '不保存',
    );
    await tester.tap(find.widgetWithText(FilledButton, '保存'));
    await _frames(tester);
    await tester.pump(const Duration(seconds: 4));
    await _frames(tester);
    expect(find.text('重命名失败'), findsOneWidget);
    expect(find.text('旅行'), findsOneWidget);
    await tester.pumpWidget(const SizedBox.shrink());
  });

  testWidgets('超出生成上限显示友好错误并恢复当前会话发送', (tester) async {
    final api = _SessionApi()..rejectSend = true;
    await _mount(tester, api);
    await _send(tester, '第四条');
    expect(find.text('同时进行的回复太多，请稍后'), findsWidgets);
    expect(_chat(tester).sending, isFalse);
    await tester.pumpWidget(const SizedBox.shrink());
  });
}
