import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:luma_client/api.dart';
import 'package:luma_client/theme.dart';
import 'package:luma_client/views/chat.dart';
import 'package:luma_client/views/chat_widgets.dart';

Widget _app(Widget child) => MaterialApp(
  theme: lumaTheme,
  home: Scaffold(body: child),
);

Map<String, dynamic> _choice({bool submitted = false}) => {
  'id': 'wgt_choice',
  'type': 'choice',
  'spec': {
    'type': 'choice',
    'title': '先做什么？',
    'options': [
      {'id': 'report', 'label': '写周报'},
      {'id': 'visit', 'label': '客户回访'},
    ],
  },
  'state': {
    'status': submitted ? 'submitted' : 'open',
    'selected': submitted ? ['report'] : [],
  },
};

void main() {
  testWidgets('confirm pending renders details and callbacks', (tester) async {
    final actions = <String>[];
    await tester.pumpWidget(
      _app(
        ChatWidgetView(
          widget: {
            'id': 'wgt_confirm',
            'type': 'confirm',
            'spec': {
              'type': 'confirm',
              'title': '需要你确认',
              'body': '将更新数据',
              'details': [
                {'label': '项目', 'value': '演示'},
              ],
            },
            'state': {'status': 'pending'},
          },
          disabled: false,
          onEvent: (id, action, value) async => actions.add(action),
        ),
      ),
    );
    expect(find.text('将更新数据'), findsOneWidget);
    expect(find.text('项目'), findsOneWidget);
    await tester.tap(find.text('确认执行'));
    await tester.pump();
    expect(actions, ['confirm']);
  });

  testWidgets('confirm with allow_always exposes remember action', (
    tester,
  ) async {
    Object? value;
    await tester.pumpWidget(
      _app(
        ChatWidgetView(
          widget: {
            'id': 'wgt_confirm_always',
            'type': 'confirm',
            'spec': {'type': 'confirm', 'allow_always': true},
            'state': {'status': 'pending'},
          },
          disabled: false,
          onEvent: (id, action, nextValue) async {
            if (action == 'confirm') value = nextValue;
          },
        ),
      ),
    );
    expect(find.text('始终允许'), findsOneWidget);
    await tester.tap(find.text('始终允许'));
    await tester.pump();
    expect(value, {'remember': true});
  });

  for (final status in ['running', 'done', 'failed', 'cancelled']) {
    testWidgets('confirm $status hides buttons and shows status', (
      tester,
    ) async {
      await tester.pumpWidget(
        _app(
          ChatWidgetView(
            widget: {
              'id': 'wgt_confirm_$status',
              'type': 'confirm',
              'spec': {'type': 'confirm', 'title': '操作'},
              'state': {'status': status},
            },
            disabled: false,
            onEvent: (id, action, value) async {},
          ),
        ),
      );
      expect(
        find.text(switch (status) {
          'running' => '正在执行…',
          'done' => '已执行',
          'failed' => '执行失败',
          _ => '已取消',
        }),
        findsOneWidget,
      );
      expect(find.text('确认执行'), findsNothing);
    });
  }

  testWidgets(
    'assistant tool status row renders running and confirmation labels',
    (tester) async {
      final input = TextEditingController();
      final scroll = ScrollController();
      addTearDown(input.dispose);
      addTearDown(scroll.dispose);
      await tester.pumpWidget(
        _app(
          SizedBox(
            height: 400,
            child: ChatView(
              messages: [
                {'role': 'user', 'content': '查一下'},
                {
                  'role': 'assistant',
                  'content': '结果',
                  'tool_states': [
                    {'connector': 'erp', 'title': '订单查询', 'status': 'running'},
                    {'title': '更新订单', 'status': 'needs_confirmation'},
                  ],
                },
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
      expect(find.text('正在查询 erp · 订单查询…'), findsOneWidget);
      expect(find.text('更新订单 需要你确认'), findsOneWidget);
    },
  );

  testWidgets('choice renders and reports selection', (tester) async {
    String? action;
    Object? value;
    await tester.pumpWidget(
      _app(
        ChatWidgetView(
          widget: _choice(),
          disabled: false,
          onEvent: (id, nextAction, nextValue) async {
            action = nextAction;
            value = nextValue;
          },
        ),
      ),
    );
    expect(find.text('写周报'), findsOneWidget);
    expect(find.text('客户回访'), findsOneWidget);
    await tester.tap(find.text('写周报'));
    expect(action, 'submit');
    expect(value, ['report']);
  });

  testWidgets('submitted choice is read only', (tester) async {
    var calls = 0;
    await tester.pumpWidget(
      _app(
        ChatWidgetView(
          widget: _choice(submitted: true),
          disabled: false,
          onEvent: (id, action, value) async => calls++,
        ),
      ),
    );
    await tester.tap(find.text('客户回访'));
    expect(calls, 0);
  });

  testWidgets(
    'streaming luma-ui block keeps visible text without another loader',
    (tester) async {
      final input = TextEditingController();
      final scroll = ScrollController();
      addTearDown(input.dispose);
      addTearDown(scroll.dispose);
      await tester.pumpWidget(
        _app(
          SizedBox(
            height: 500,
            child: ChatView(
              messages: [
                {'role': 'user', 'content': '给我几个选项'},
                {
                  'role': 'assistant',
                  'streaming': true,
                  'content': '准备好了\n```luma-ui\n{"type":"choice"}',
                },
              ],
              tasks: const [],
              memories: const [],
              sending: true,
              input: input,
              scrollController: scroll,
              onRefresh: () async {},
              onSend: () {},
              onWidgetEvent: (id, action, value) async {},
            ),
          ),
        ),
      );
      expect(find.bySemanticsLabel('Luma 正在思考'), findsNothing);
      expect(find.text('准备好了'), findsOneWidget);
      expect(find.text('正在生成卡片…'), findsNothing);
      expect(find.textContaining('{"type":"choice"}'), findsNothing);
    },
  );

  testWidgets('message status renders interruption and retry affordance', (
    tester,
  ) async {
    final input = TextEditingController();
    final scroll = ScrollController();
    addTearDown(input.dispose);
    addTearDown(scroll.dispose);
    var retries = 0;
    await tester.pumpWidget(
      _app(
        SizedBox(
          height: 500,
          child: ChatView(
            messages: [
              {'role': 'user', 'content': '继续'},
              {'role': 'assistant', 'content': '已收到', 'status': 'incomplete'},
              {
                'role': 'assistant',
                'content': '失败',
                'status': 'error',
                'error': '上游不可用',
              },
            ],
            tasks: const [],
            memories: const [],
            sending: false,
            input: input,
            scrollController: scroll,
            onRefresh: () async {},
            onSend: () {},
            onWidgetEvent: (id, action, value) async {},
            onRetry: (_) async {
              retries++;
            },
          ),
        ),
      ),
    );
    expect(find.text('已中断'), findsOneWidget);
    expect(find.text('重试'), findsOneWidget);
    await tester.tap(find.text('重试'));
    expect(retries, 1);
  });

  testWidgets('sandbox tool event is rendered as plain text fields', (
    tester,
  ) async {
    final input = TextEditingController();
    final scroll = ScrollController();
    addTearDown(input.dispose);
    addTearDown(scroll.dispose);
    await tester.pumpWidget(
      _app(
        SizedBox(
          height: 500,
          child: ChatView(
            messages: [
              {'role': 'user', 'content': '运行'},
              {
                'role': 'assistant',
                'content': '结果',
                'tool_states': [
                  {
                    'kind': 'sandbox',
                    'language': 'python',
                    'code': 'print(1)',
                    'output': '1',
                    'exit_code': 0,
                  },
                ],
              },
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
    expect(find.text('沙箱工具'), findsOneWidget);
    expect(find.text('python'), findsOneWidget);
    expect(find.text('print(1)'), findsOneWidget);
    expect(find.text('1'), findsOneWidget);
    expect(find.text('0'), findsOneWidget);
  });

  testWidgets('file tool event renders a card and invokes file callback', (
    tester,
  ) async {
    final input = TextEditingController();
    final scroll = ScrollController();
    addTearDown(input.dispose);
    addTearDown(scroll.dispose);
    Map<String, dynamic>? opened;
    await tester.pumpWidget(
      _app(
        SizedBox(
          height: 500,
          child: ChatView(
            messages: [
              {'role': 'user', 'content': '导出'},
              {
                'role': 'assistant',
                'content': '完成',
                'tool_states': [
                  {
                    'kind': 'file',
                    'file_id': 'file-1',
                    'filename': 'report.csv',
                    'size_bytes': 12,
                    'media_type': 'text/csv',
                  },
                ],
              },
            ],
            tasks: const [],
            memories: const [],
            sending: false,
            input: input,
            scrollController: scroll,
            onRefresh: () async {},
            onSend: () {},
            onWidgetEvent: (id, action, value) async {},
            onFile: (file) async => opened = file,
          ),
        ),
      ),
    );
    expect(find.text('report.csv'), findsOneWidget);
    expect(find.text('text/csv · 12 bytes'), findsOneWidget);
    await tester.tap(find.text('report.csv'));
    await tester.pump();
    expect(opened?['file_id'], 'file-1');
  });

  testWidgets(
    'persisted screenshot remains visible after transient events clear',
    (tester) async {
      final input = TextEditingController();
      final scroll = ScrollController();
      addTearDown(input.dispose);
      addTearDown(scroll.dispose);
      final png = base64Decode(
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l1sAAAAASUVORK5CYII=',
      );
      final api = AssistantApi(
        token: 'fixture',
        client: MockClient((request) async {
          expect(request.url.path, '/api/v1/files/screenshot-1/content');
          expect(request.headers['Authorization'], 'Bearer fixture');
          return http.Response.bytes(png, 200);
        }),
      );
      addTearDown(api.close);
      Map<String, dynamic>? opened;
      final file = <String, dynamic>{
        'kind': 'file',
        'file_id': 'screenshot-1',
        'filename': 'homepage.png',
        'media_type': 'image/png',
        'size_bytes': png.length,
        'url': 'https://untrusted.example/image.png',
      };
      final call = <String, dynamic>{
        'call_id': 'screenshot-call',
        'tool': 'browser.screenshot',
        'status': 'ok',
        'data': file,
      };
      Widget tree({bool transient = false, bool legacy = false}) => _app(
        SizedBox(
          height: 600,
          child: ChatView(
            messages: [
              {'role': 'user', 'content': '截图'},
              {
                'role': 'assistant',
                'content': '已截取首页。',
                'status': 'complete',
                if (transient) 'tool_states': [call],
                'metadata': {
                  if (legacy) 'tool_calls': [call],
                  'tool_events': [
                    {
                      'kind': 'tool_result',
                      'call_id': 'screenshot-call',
                      'payload': file,
                    },
                  ],
                },
              },
            ],
            tasks: const [],
            memories: const [],
            sending: false,
            input: input,
            scrollController: scroll,
            onRefresh: () async {},
            onSend: () {},
            onWidgetEvent: (id, action, value) async {},
            onFile: (file) async => opened = file,
            api: api,
          ),
        ),
      );
      for (final messageTree in [
        tree(transient: true, legacy: true),
        tree(legacy: true),
        tree(),
      ]) {
        await tester.pumpWidget(messageTree);
        await tester.pumpAndSettle();
        expect(find.text('homepage.png'), findsOneWidget);
        expect(find.byType(Image), findsOneWidget);
        expect(find.textContaining('untrusted.example'), findsNothing);
      }
      await tester.tap(find.text('homepage.png'));
      await tester.pump();
      expect(opened?['file_id'], 'screenshot-1');
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets(
    'preview only offers copy for HTTPS and sandbox jobs show status',
    (tester) async {
      final input = TextEditingController();
      final scroll = ScrollController();
      addTearDown(input.dispose);
      addTearDown(scroll.dispose);
      await tester.pumpWidget(
        _app(
          SizedBox(
            height: 600,
            child: ChatView(
              messages: [
                {'role': 'user', 'content': '预览'},
                {
                  'role': 'assistant',
                  'content': '任务完成',
                  'tool_states': [
                    {
                      'kind': 'preview',
                      'url': 'https://preview.example.test/app',
                      'note': '沙箱运行时有效',
                      'public': true,
                    },
                    {
                      'kind': 'preview',
                      'url': 'javascript:alert(1)',
                      'public': false,
                    },
                    {
                      'kind': 'sandbox_job',
                      'job_id': 'job-1',
                      'command': 'sleep 1',
                      'status': 'running',
                    },
                  ],
                },
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
      expect(find.text('预览链接'), findsNWidgets(2));
      expect(find.text('拿到链接的人在沙箱运行期间都能访问'), findsOneWidget);
      expect(find.text('仅支持 HTTPS 链接'), findsOneWidget);
      expect(find.byTooltip('复制链接'), findsOneWidget);
      expect(find.text('沙箱任务'), findsOneWidget);
      expect(find.text('running'), findsOneWidget);
      expect(find.text('sleep 1'), findsOneWidget);
    },
  );

  testWidgets('scrolling to the top requests an older message page', (
    tester,
  ) async {
    final input = TextEditingController();
    final scroll = ScrollController();
    addTearDown(input.dispose);
    addTearDown(scroll.dispose);
    var requested = 0;
    final messages = <Map<String, dynamic>>[
      {'role': 'user', 'content': '开始'},
      for (var i = 0; i < 30; i++) {'role': 'assistant', 'content': '消息 $i'},
    ];
    await tester.pumpWidget(
      _app(
        SizedBox(
          height: 240,
          child: ChatView(
            messages: messages,
            tasks: const [],
            memories: const [],
            sending: false,
            input: input,
            scrollController: scroll,
            onRefresh: () async {},
            onSend: () {},
            onWidgetEvent: (id, action, value) async {},
            hasMoreMessages: true,
            onLoadOlder: () async {
              requested++;
            },
          ),
        ),
      ),
    );
    await tester.pump();
    scroll.jumpTo(scroll.position.maxScrollExtent);
    await tester.pump();
    scroll.jumpTo(0);
    await tester.pump();
    expect(requested, greaterThanOrEqualTo(1));
  });
}
