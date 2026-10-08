import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:luma_client/markdown.dart';
import 'package:luma_client/preferences.dart';
import 'package:luma_client/theme.dart';
import 'package:luma_client/views/chat.dart';
import 'package:luma_client/views/chat_widgets.dart';
import 'package:luma_client/views/browser_live.dart';

Future<void> _mount(
  WidgetTester tester, {
  required TextEditingController input,
  required ScrollController scroll,
  required List<Map<String, dynamic>> messages,
  bool sending = true,
}) async {
  await tester.pumpWidget(
    LumaPreferencesScope(
      preferences: LumaPreferences.instance,
      child: MaterialApp(
        theme: lumaTheme,
        home: Scaffold(
          body: ChatView(
            messages: messages,
            tasks: const [],
            memories: const [],
            sending: sending,
            input: input,
            scrollController: scroll,
            onRefresh: () async {},
            onSend: () {},
            onWidgetEvent: (id, action, value) async {},
          ),
        ),
      ),
    ),
  );
  await tester.pump(const Duration(milliseconds: 400));
  await tester.pump(const Duration(milliseconds: 100));
}

void main() {
  setUp(() {
    LumaPreferences.instance.value = const AppearancePreferences();
  });
  tearDown(() {
    LumaPreferences.instance.value = const AppearancePreferences();
  });

  testWidgets('主动和动态来源标签显示，主动原因按纯文本弹出', (tester) async {
    final input = TextEditingController();
    final scroll = ScrollController();
    addTearDown(input.dispose);
    addTearDown(scroll.dispose);
    await _mount(
      tester,
      input: input,
      scroll: scroll,
      sending: false,
      messages: [
        {
          'role': 'assistant',
          'content': '记得准备本周报告',
          'metadata': {
            'proactive': {'kind': 'followup', 'reason': '<b>你上周提过报告</b>'},
            'feed_post_id': 'post-1',
          },
        },
      ],
    );
    expect(find.text('主动'), findsOneWidget);
    expect(find.text('来自动态'), findsOneWidget);
    await tester.tap(find.text('主动'));
    await tester.pumpAndSettle();
    expect(find.text('为什么发给你'), findsOneWidget);
    expect(find.text('<b>你上周提过报告</b>'), findsOneWidget);
    expect(tester.takeException(), isNull);
    await tester.pumpWidget(const SizedBox.shrink());
  });

  testWidgets('empty streaming reply is compact and first text replaces dots', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(390, 844);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.reset);
    final input = TextEditingController();
    final scroll = ScrollController();
    addTearDown(input.dispose);
    addTearDown(scroll.dispose);
    final messages = <Map<String, dynamic>>[
      {'role': 'user', 'content': '你好'},
      {'role': 'assistant', 'content': '', 'status': 'streaming'},
    ];
    await _mount(tester, input: input, scroll: scroll, messages: messages);
    final bubble = find.byKey(const ValueKey('chat-thinking-bubble'));
    expect(bubble, findsOneWidget);
    expect(tester.getSize(bubble).width, lessThan(80));
    expect(find.bySemanticsLabel('Luma 正在思考'), findsOneWidget);
    expect(find.text('…'), findsNothing);
    expect(find.text('Luma 正在整理…'), findsNothing);
    final transforms = find.descendant(
      of: bubble,
      matching: find.byType(Transform),
    );
    final before = tester
        .widgetList<Transform>(transforms)
        .map((widget) => widget.transform.getTranslation().y)
        .toList();
    await tester.pump(const Duration(milliseconds: 160));
    final after = tester
        .widgetList<Transform>(transforms)
        .map((widget) => widget.transform.getTranslation().y)
        .toList();
    expect(after, isNot(equals(before)));
    expect(after.toSet().length, greaterThan(1));

    messages.last['content'] = '第一个字';
    await _mount(tester, input: input, scroll: scroll, messages: messages);
    expect(bubble, findsNothing);
    expect(find.bySemanticsLabel('Luma 正在思考'), findsNothing);
    expect(find.text('第一个字'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets(
    'running tool caption is below dots and disappears on completion',
    (tester) async {
      final input = TextEditingController();
      final scroll = ScrollController();
      addTearDown(input.dispose);
      addTearDown(scroll.dispose);
      final tool = <String, dynamic>{
        'connector': 'sample-data',
        'title': '取数',
        'status': 'running',
      };
      final messages = <Map<String, dynamic>>[
        {'role': 'user', 'content': '查数据'},
        {
          'role': 'assistant',
          'content': '',
          'status': 'streaming',
          'tool_states': [tool],
        },
      ];
      await _mount(tester, input: input, scroll: scroll, messages: messages);
      final bubble = find.byKey(const ValueKey('chat-thinking-bubble'));
      final caption = find.byKey(const ValueKey('chat-tool-progress'));
      expect(find.text('正在查询 sample-data · 取数…'), findsOneWidget);
      expect(
        tester.getRect(caption).top,
        greaterThan(tester.getRect(bubble).bottom),
      );
      tool['status'] = 'done';
      await _mount(tester, input: input, scroll: scroll, messages: messages);
      expect(caption, findsNothing);
      expect(find.textContaining('已查询'), findsNothing);
    },
  );

  testWidgets('sending without assistant still has exactly one loader', (
    tester,
  ) async {
    final input = TextEditingController();
    final scroll = ScrollController();
    addTearDown(input.dispose);
    addTearDown(scroll.dispose);
    await _mount(
      tester,
      input: input,
      scroll: scroll,
      messages: [
        {'role': 'user', 'content': '你好'},
      ],
    );
    expect(find.byKey(const ValueKey('chat-thinking-bubble')), findsOneWidget);
  });

  testWidgets(
    'browser progress and live cards render before streamed text arrives',
    (tester) async {
      final input = TextEditingController();
      final scroll = ScrollController();
      addTearDown(input.dispose);
      addTearDown(scroll.dispose);
      final messages = <Map<String, dynamic>>[
        {
          'role': 'assistant',
          'content': '',
          'status': 'streaming',
          'tool_states': [
            {'tool': 'browser.open', 'status': 'running'},
          ],
        },
      ];
      await _mount(tester, input: input, scroll: scroll, messages: messages);
      expect(find.text('正在打开网页…'), findsOneWidget);
      messages.last['tool_states'] = [
        {
          'tool': 'browser.live',
          'status': 'ok',
          'kind': 'tool',
          'data': {
            'kind': 'browser_live',
            'url': 'https://view.tencentags.com/?access_token=fixture',
          },
        },
      ];
      await _mount(tester, input: input, scroll: scroll, messages: messages);
      expect(find.byType(BrowserLiveCard), findsOneWidget);
      expect(find.text('观看实时画面'), findsOneWidget);
      expect(find.byKey(const ValueKey('chat-thinking-bubble')), findsNothing);
      expect(find.textContaining('access_token'), findsNothing);
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox.shrink());
    },
  );

  testWidgets(
    'pseudo markup stays hidden and structured widgets survive markdown',
    (tester) async {
      final input = TextEditingController();
      final scroll = ScrollController();
      addTearDown(input.dispose);
      addTearDown(scroll.dispose);
      await _mount(
        tester,
        input: input,
        scroll: scroll,
        sending: false,
        messages: [
          {
            'role': 'assistant',
            'content':
                '**口径说明**（取数时沿用）\n'
                '<tool_call>{"name":"luma"}</tool_call>\n[[widget:choice]]',
            'metadata': {
              'widgets': [
                {
                  'id': 'choice',
                  'type': 'choice',
                  'spec': {
                    'type': 'choice',
                    'title': '选项',
                    'options': [
                      {'id': 'one', 'label': '第一项'},
                    ],
                  },
                  'state': {'status': 'open'},
                },
              ],
            },
          },
        ],
      );
      expect(find.byType(LumaMarkdown), findsOneWidget);
      expect(find.textContaining('口径说明', findRichText: true), findsWidgets);
      expect(
        find.textContaining('<tool_call>', findRichText: true),
        findsNothing,
      );
      expect(find.byType(ChatWidgetView), findsOneWidget);
      expect(find.text('第一项'), findsOneWidget);
    },
  );

  testWidgets('message font size updates in place for user and assistant', (
    tester,
  ) async {
    final input = TextEditingController();
    final scroll = ScrollController();
    addTearDown(input.dispose);
    addTearDown(scroll.dispose);
    await _mount(
      tester,
      input: input,
      scroll: scroll,
      sending: false,
      messages: [
        {'role': 'user', 'content': '用户文字'},
        {'role': 'assistant', 'content': '**助手文字**'},
      ],
    );
    final markdown = find.byType(LumaMarkdown);
    expect(
      tester
          .widgetList<LumaMarkdown>(markdown)
          .map((item) => item.style.fontSize),
      [16, 16],
    );
    for (final size in [MessageFontSize.large, MessageFontSize.small]) {
      LumaPreferences.instance.value = LumaPreferences.instance.value.copyWith(
        fontSize: size,
      );
      await tester.pump();
      expect(
        tester
            .widgetList<LumaMarkdown>(markdown)
            .map((item) => item.style.fontSize),
        List.filled(2, size == MessageFontSize.large ? 18 : 14),
      );
    }
    expect(tester.takeException(), isNull);
  });

  testWidgets('only hidden streaming fragments use compact loader', (
    tester,
  ) async {
    final input = TextEditingController();
    final scroll = ScrollController();
    addTearDown(input.dispose);
    addTearDown(scroll.dispose);
    await _mount(
      tester,
      input: input,
      scroll: scroll,
      messages: [
        {
          'role': 'assistant',
          'status': 'streaming',
          'content': '```luma-ui\n{"type":"choice"}',
        },
      ],
    );
    expect(find.byKey(const ValueKey('chat-thinking-bubble')), findsOneWidget);
    expect(find.textContaining('choice', findRichText: true), findsNothing);
  });
}
