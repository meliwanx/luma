import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:luma_client/api.dart';
import 'package:luma_client/home.dart';
import 'package:luma_client/theme.dart';
import 'package:luma_client/views/feed.dart';
import 'package:luma_client/views/ideas.dart';
import 'package:luma_client/views/library.dart';
import 'package:luma_client/views/chat.dart';
import 'package:luma_client/views/memory.dart';
import 'package:luma_client/views/missions.dart';
import 'package:luma_client/views/mobile_shell.dart';

class _MobileApi extends AssistantApi {
  _MobileApi({this.withMessage = false, this.withIdeas = true});

  final bool withMessage;
  final bool withIdeas;
  Map<String, dynamic>? sentMetadata;
  int dashboardCalls = 0;
  Completer<void>? dashboardGate;
  String? sentSession;
  String? sentContent;

  @override
  Future<Stream<AssistantSseEvent>> streamMessage(
    String sessionId,
    String content, {
    Map<String, dynamic>? metadata,
  }) async {
    sentMetadata = metadata;
    sentSession = sessionId;
    sentContent = content;
    return Stream.fromIterable([
      const AssistantSseEvent('done', {'content': '完成', 'status': 'complete'}),
    ]);
  }

  @override
  Future<Map<String, dynamic>?> dashboard({String? selectedSessionId}) async {
    dashboardCalls++;
    final gate = dashboardGate;
    if (gate != null) await gate.future;
    return {
      'conversation_id': selectedSessionId ?? 'main',
      'sessions': [
        {'id': 'main', 'kind': 'main'},
        {'id': 'side', 'kind': 'side', 'title': '旅行'},
      ],
      'messages': <Map<String, dynamic>>[
        if (withMessage)
          {'id': 'first-message', 'role': 'user', 'content': '顶部测量消息'},
      ],
      'memories': [
        {'id': 'idea', 'category': 'idea', 'content': '做一个灵感板'},
        {'id': 'fact', 'category': 'fact', 'content': '喜欢蓝色'},
      ],
      'tasks': <Map<String, dynamic>>[],
      'runtime_jobs': <Map<String, dynamic>>[],
      'approvals': <Map<String, dynamic>>[],
      'runtime_activity': [
        {
          'title': '保存记忆',
          'created_at': '2026-10-04T10:00:00Z',
          'kind': 'memory',
        },
      ],
    };
  }

  @override
  Future<List<Map<String, dynamic>>> listRoutines({
    bool enabledOnly = false,
  }) async => [];

  @override
  Future<Map<String, dynamic>> ideas() async => {
    'featured': [
      if (withIdeas)
        {
          'id': 'idea-1',
          'title': '我可以帮你规划旅行',
          'summary': '整理行程和预算',
          'plan_markdown': '## 包含哪些内容\n旅行计划\n## 如何进行\n整理需求',
          'sources': <Map<String, dynamic>>[],
        },
    ],
    'groups': <Map<String, dynamic>>[],
    'generating': false,
  };

  @override
  Future<Map<String, dynamic>> startIdea(String id) async => {
    'session_id': 'side',
    'prompt': '我们开始做这个：旅行计划',
  };

  @override
  Future<Map<String, dynamic>> feed({int limit = 20, String? cursor}) async => {
    'items': [
      {
        'id': 'feed-1',
        'title': '旅行新发现',
        'body_markdown': '本周的旅行资讯',
        'sources': <Map<String, dynamic>>[],
        'liked': false,
      },
    ],
    'next_cursor': null,
  };

  @override
  Future<Map<String, dynamic>> discussFeedPost(String id) async => {
    'session_id': 'side',
  };

  @override
  Future<Map<String, dynamic>> library({
    String type = 'all',
    String q = '',
    String sort = 'recent',
    int limit = 50,
    String? cursor,
  }) async => {
    'items': [
      {
        'id': 'file',
        'title': '旅行.pdf',
        'filename': '旅行.pdf',
        'type': 'document',
        'pinned': false,
      },
    ],
    'counts': {'all': 1, 'document': 1},
    'next_cursor': null,
  };

  @override
  Future<List<Map<String, dynamic>>> listFiles({
    String? sessionId,
    int limit = 100,
  }) async => [
    {'id': 'file', 'filename': '旅行.pdf', 'size_bytes': 2048},
  ];

  @override
  Future<List<Map<String, dynamic>>> listNotifications({
    bool unreadOnly = false,
    int limit = 100,
  }) async => [
    {'id': 'notice', 'title': '准备好了', 'body': '任务已完成'},
  ];

  @override
  Future<Map<String, dynamic>> markNotificationRead(String id) async => {
    'id': id,
    'title': '准备好了',
    'body': '任务已完成',
    'read_at': '2026-10-04T12:00:00Z',
  };
}

Future<void> _mountHome(WidgetTester tester, {_MobileApi? api}) async {
  tester.view.physicalSize = const Size(390, 844);
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.reset);
  await tester.pumpWidget(
    MaterialApp(
      theme: lumaTheme,
      home: LumaHome(api: api ?? _MobileApi()),
    ),
  );
  await tester.pumpAndSettle();
}

void main() {
  testWidgets('主聊天点击 logo 打开菜单，旁聊保留标题胶囊及菜单行为', (tester) async {
    await _mountHome(tester);
    final logo = find.byKey(const ValueKey('mobile-header-logo'));
    final titleButton = find.byKey(const ValueKey('mobile-title-button'));
    expect(find.text('Luma'), findsNothing);
    expect(find.descendant(of: titleButton, matching: logo), findsOneWidget);
    await tester.tap(logo);
    await tester.pumpAndSettle();
    expect(find.text('选项卡'), findsOneWidget);
    await tester.tap(find.byKey(const ValueKey('menu-session-side')));
    await tester.pumpAndSettle();
    expect(
      tester.widget<MobileShell>(find.byType(MobileShell)).sessionId,
      'side',
    );
    expect(logo, findsOneWidget);
    expect(
      find.descendant(of: titleButton, matching: find.text('旅行')),
      findsOneWidget,
    );
    expect(find.descendant(of: titleButton, matching: logo), findsNothing);
    await tester.tap(titleButton);
    await tester.pumpAndSettle();
    expect(find.text('选项卡'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('主聊天缩短实际消息顶部留白，安全区和输入栏位置保持正确', (tester) async {
    await _mountHome(tester, api: _MobileApi(withMessage: true));
    tester.view.padding = const FakeViewPadding(top: 24, bottom: 20);
    await tester.pumpAndSettle();
    final message = find.text('顶部测量消息');
    final titleButton = find.byKey(const ValueKey('mobile-title-button'));
    final composer = find.byKey(const ValueKey('chat-mobile-composer'));
    final mainMessageTop = tester.getTopLeft(message).dy;
    final mainHeaderBottom = tester.getBottomLeft(titleButton).dy;
    final composerBottom = tester.getBottomLeft(composer).dy;
    expect(mainHeaderBottom, 24 + 8 + 48);
    expect(mainMessageTop - mainHeaderBottom, inExclusiveRange(16, 60));
    expect(composerBottom, closeTo(844 - 20 - 64 - 16, 1));

    await tester.tap(find.byKey(const ValueKey('mobile-header-logo')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('menu-session-side')));
    await tester.pumpAndSettle();
    final sideMessageTop = tester.getTopLeft(message).dy;
    final sideHeaderBottom = tester.getBottomLeft(titleButton).dy;
    expect(sideMessageTop - mainMessageTop, closeTo(40, 1));
    expect(
      sideMessageTop - sideHeaderBottom,
      closeTo(mainMessageTop - mainHeaderBottom, 2),
    );
    expect(tester.getBottomLeft(composer).dy, closeTo(composerBottom, 1));

    await tester.tap(find.byKey(const ValueKey('mobile-main-chat-button')));
    await tester.pumpAndSettle();
    expect(tester.getTopLeft(message).dy, closeTo(mainMessageTop, 1));
    expect(tester.getBottomLeft(composer).dy, closeTo(composerBottom, 1));
    await tester.tap(find.byKey(const ValueKey('chat-input')));
    tester.view.viewInsets = const FakeViewPadding(bottom: 300);
    await tester.pumpAndSettle();
    expect(tester.getBottomLeft(composer).dy, closeTo(844 - 300 - 8, 1));
    expect(find.byKey(const ValueKey('mobile-tab-bar')), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets('主旁聊聊天区域及下拉刷新指示器保持相同位置', (tester) async {
    final api = _MobileApi(withMessage: true);
    await _mountHome(tester, api: api);
    final chatBounds = <Rect>[];
    final refreshBounds = <Rect>[];
    final spinnerBounds = <Rect>[];
    final list = find.byKey(const ValueKey('chat-message-list'));
    final refresh = find.descendant(
      of: find.byType(ChatView),
      matching: find.byType(RefreshIndicator),
    );
    for (final sideChat in [false, true]) {
      if (sideChat) {
        await tester.tap(find.byKey(const ValueKey('mobile-header-logo')));
        await tester.pumpAndSettle();
        await tester.tap(find.byKey(const ValueKey('menu-session-side')));
        await tester.pumpAndSettle();
      }
      chatBounds.add(tester.getRect(find.byType(ChatView)));
      refreshBounds.add(tester.getRect(refresh));
      final callsBeforeRefresh = api.dashboardCalls;
      final gate = Completer<void>();
      api.dashboardGate = gate;
      addTearDown(() {
        if (!gate.isCompleted) gate.complete();
      });
      await tester.drag(list, const Offset(0, 400));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 300));
      expect(api.dashboardCalls, callsBeforeRefresh + 1);
      final spinner = find.byType(RefreshProgressIndicator);
      expect(spinner, findsOneWidget);
      spinnerBounds.add(tester.getRect(spinner));
      api.dashboardGate = null;
      gate.complete();
      await tester.pumpAndSettle();
    }
    expect(chatBounds.first, const Rect.fromLTWH(0, 0, 390, 844));
    expect(chatBounds.last, chatBounds.first);
    expect(refreshBounds.first, chatBounds.first);
    expect(refreshBounds.last, refreshBounds.first);
    expect(spinnerBounds.last.top, closeTo(spinnerBounds.first.top, 1));
    expect(spinnerBounds.first.top, greaterThanOrEqualTo(0));
    expect(tester.takeException(), isNull);
  });

  testWidgets('快速切换 tab 不会重复挂载聊天滚动控制器', (tester) async {
    await _mountHome(tester);
    for (var i = 0; i < 3; i++) {
      await tester.tap(find.byTooltip('目标'));
      await tester.pump(const Duration(milliseconds: 20));
      await tester.tap(find.byTooltip('聊天'));
      await tester.pump(const Duration(milliseconds: 20));
    }
    await tester.pumpAndSettle();
    expect(find.byType(ChatView), findsOneWidget);
    final chat = tester.widget<ChatView>(find.byType(ChatView));
    expect(chat.scrollController.positions.length, 1);
    expect(tester.takeException(), isNull);
  });

  testWidgets('已有资源文件作为附件随消息发出并清空草稿', (tester) async {
    final api = _MobileApi();
    await _mountHome(tester, api: api);
    await tester.tap(find.byTooltip('附件'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('旅行.pdf'));
    await tester.pumpAndSettle();
    expect(find.text('旅行.pdf'), findsOneWidget);
    await tester.enterText(find.byKey(const ValueKey('chat-input')), '总结这个文件');
    await tester.pump();
    await tester.tap(find.byTooltip('发送'));
    await tester.pumpAndSettle();
    expect((api.sentMetadata!['files'] as List).single['id'], 'file');
    expect((api.sentMetadata!['files'] as List).single['filename'], '旅行.pdf');
    expect(tester.widget<ChatView>(find.byType(ChatView)).attachments, isEmpty);
    expect(tester.takeException(), isNull);
  });

  testWidgets('菜单目标打开现有 MissionsView', (tester) async {
    await _mountHome(tester);
    await tester.tap(find.byTooltip('菜单'));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('menu-tab-3')));
    await tester.pumpAndSettle();
    expect(find.byType(MissionsView), findsOneWidget);
    expect(find.byKey(const ValueKey('mobile-menu-scrim')), findsNothing);
  });

  testWidgets('五个 tab 接入独立动态、点子和资源库', (tester) async {
    await _mountHome(tester);
    for (final label in ['聊天', '动态', '点子', '目标', '资源库']) {
      expect(find.byTooltip(label), findsOneWidget);
    }
    await tester.tap(find.byTooltip('动态'));
    await tester.pumpAndSettle();
    expect(find.byType(FeedView), findsOneWidget);
    expect(find.text('旅行新发现'), findsOneWidget);
    expect(find.text('保存记忆'), findsNothing);
    await tester.tap(find.byTooltip('点子'));
    await tester.pumpAndSettle();
    expect(find.byType(IdeasView), findsOneWidget);
    expect(find.text('我可以帮你规划旅行'), findsOneWidget);
    expect(find.byType(MemoryView), findsNothing);
    expect(find.text('喜欢蓝色'), findsNothing);
    await tester.tap(find.byTooltip('资源库'));
    await tester.pumpAndSettle();
    expect(find.byType(LibraryView), findsOneWidget);
    expect(find.text('喜欢蓝色'), findsNothing);
    expect(find.text('旅行.pdf'), findsOneWidget);
    await tester.pumpWidget(const SizedBox.shrink());
  });

  testWidgets('点子马上开始通过正常聊天流程发送', (tester) async {
    final api = _MobileApi();
    await _mountHome(tester, api: api);
    await tester.tap(find.byTooltip('点子'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('我可以帮你规划旅行'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('马上开始'));
    await tester.pumpAndSettle();
    expect(find.byType(ChatView), findsOneWidget);
    expect(api.sentSession, 'side');
    expect(api.sentContent, '我们开始做这个：旅行计划');
    await tester.pumpWidget(const SizedBox.shrink());
  });

  testWidgets('动态讨论打开会话并聚焦输入框', (tester) async {
    await _mountHome(tester);
    await tester.tap(find.byTooltip('动态'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('讨论'));
    await tester.pumpAndSettle();
    final chat = tester.widget<ChatView>(find.byType(ChatView));
    final field = tester.widget<TextField>(
      find.byKey(const ValueKey('chat-input')),
    );
    expect(chat.sessionId, 'side');
    expect(field.focusNode!.hasFocus, isTrue);
    await tester.pumpWidget(const SizedBox.shrink());
  });

  testWidgets('手机顶栏移除通知铃铛入口', (tester) async {
    await _mountHome(tester);
    expect(find.byTooltip('通知'), findsNothing);
    expect(find.byIcon(Icons.notifications_none_rounded), findsNothing);
    expect(find.byKey(const ValueKey('mobile-notifications')), findsNothing);
  });

  testWidgets('加载中切换点子会等待新会话后再发送', (tester) async {
    final api = _MobileApi();
    await _mountHome(tester, api: api);
    final gate = Completer<void>();
    api.dashboardGate = gate;
    unawaited(tester.widget<ChatView>(find.byType(ChatView)).onRefresh());
    await tester.pump();
    await tester.tap(find.byTooltip('点子'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('我可以帮你规划旅行'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('马上开始'));
    await tester.pump();
    expect(api.sentContent, isNull);
    api.dashboardGate = null;
    gate.complete();
    await tester.pumpAndSettle();
    expect(api.sentSession, 'side');
    expect(api.sentContent, '我们开始做这个：旅行计划');
    expect(tester.widget<ChatView>(find.byType(ChatView)).sending, isFalse);
    await tester.pumpWidget(const SizedBox.shrink());
  });

  testWidgets('点子空态引导通过对话发现灵感', (tester) async {
    final api = _MobileApi(withIdeas: false);
    addTearDown(api.close);
    await tester.pumpWidget(
      MaterialApp(
        theme: lumaTheme,
        home: Scaffold(
          body: IdeasView(
            api: api,
            onStart: (id, prompt) async {},
            onSelectSession: (_) {},
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();
    expect(find.text('还没有点子'), findsOneWidget);
    expect(find.text('和 Luma 聊聊你想做的事，灵感会在这里出现。'), findsOneWidget);
    expect(find.byType(MemoryView), findsNothing);
    await tester.pumpWidget(const SizedBox.shrink());
  });
}
