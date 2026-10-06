import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:luma_client/api.dart';
import 'package:luma_client/glass.dart';
import 'package:luma_client/markdown.dart';
import 'package:luma_client/theme.dart';
import 'package:luma_client/views/feed.dart';
import 'package:luma_client/views/ideas.dart';

const _idea = <String, dynamic>{
  'id': 'idea-1',
  'title': '我可以整理本周进展',
  'summary': '生成一份可以分享的周报。',
  'plan_markdown': '## 包含哪些内容\n本周进展\n\n## 如何进行\n从对话中提炼要点',
  'icon': 'doc',
  'status': 'active',
  'is_template': false,
  'sources': [
    {'session_id': 'source-1', 'session_title': '项目进度'},
  ],
};

const _post = <String, dynamic>{
  'id': 'post-1',
  'title': '新的产品设计方法',
  'body_markdown': '**设计要点**\n\n<script>alert(1)</script>',
  'icon': 'spark',
  'reason': '你最近关注产品设计',
  'created_at': '2026-10-06T08:00:00Z',
  'liked': false,
  'sources': [
    {'title': 'HTTPS 来源', 'url': 'https://example.com/news'},
    {'title': 'HTTP 来源', 'url': 'http://example.com/post'},
    {'title': '脚本来源', 'url': 'javascript:alert(1)'},
    {'title': '文件来源', 'url': 'file:///private/tmp/news'},
    {'title': '相对来源', 'url': '/api/v1/private'},
  ],
};

class _ContentApi extends AssistantApi {
  int ideaReads = 0;
  int refreshes = 0;
  int instructionsReads = 0;
  bool instructionsSupported = true;
  bool updatesSupported = true;
  Future<Map<String, dynamic>> Function(int)? ideaResponse;
  final calls = <String>[];
  final feedCursors = <String?>[];
  final ideaFeedbacks = <String>[];
  final feedFeedbacks = <String>[];
  Map<String, dynamic>? savedPrefs;
  Future<Map<String, dynamic>> Function(String?)? feedResponse;

  @override
  Future<Map<String, dynamic>> ideas() async {
    ideaReads++;
    if (ideaResponse != null) return ideaResponse!(ideaReads);
    return {
      'featured': [_idea],
      'groups': [
        {
          'name': '效率提升',
          'items': [
            {
              ..._idea,
              'id': 'template-1',
              'title': '我可以安排明天的工作',
              'is_template': true,
            },
          ],
        },
      ],
      'generating': false,
    };
  }

  @override
  Future<void> refreshIdeas() async {
    refreshes++;
  }

  @override
  Future<Map<String, dynamic>> startIdea(String id) async {
    calls.add('start:$id');
    return {'session_id': 'started-session', 'prompt': '我们开始做这个：周报\n\n具体计划'};
  }

  @override
  Future<void> ideaFeedback(String id, String action) async {
    ideaFeedbacks.add('$id:$action');
  }

  @override
  Future<Map<String, dynamic>> feed({int limit = 20, String? cursor}) async {
    feedCursors.add(cursor);
    if (feedResponse != null) return feedResponse!(cursor);
    return {
      'items': [_post],
      'next_cursor': null,
    };
  }

  @override
  Future<Map<String, dynamic>> discussFeedPost(String id) async {
    calls.add('discuss:$id');
    return {'session_id': 'discussion-session'};
  }

  @override
  Future<void> feedFeedback(String id, String action) async {
    feedFeedbacks.add('$id:$action');
  }

  @override
  Future<void> deleteFeedPost(String id) async {
    calls.add('delete:$id');
  }

  @override
  Future<void> refreshFeed() async {
    refreshes++;
  }

  @override
  Future<Map<String, dynamic>> proactivePrefs() async {
    instructionsReads++;
    if (!instructionsSupported) return {};
    return {'enabled': false, 'max_per_day': 2, 'feed_instructions': '关注产品设计'};
  }

  @override
  Future<Map<String, dynamic>> updateProactivePrefs(
    Map<String, dynamic> prefs,
  ) async {
    savedPrefs = prefs;
    if (!updatesSupported) return {};
    return prefs;
  }
}

Widget _shell(Widget child) => MaterialApp(
  theme: lumaTheme,
  home: Scaffold(body: child),
);

IdeasView _ideas(
  _ContentApi api, {
  Future<void> Function(String, String)? onStart,
  ValueChanged<String>? onSelectSession,
}) => IdeasView(
  api: api,
  onStart: onStart ?? (_, _) async {},
  onSelectSession: onSelectSession ?? (_) {},
);

FeedView _feed(_ContentApi api, {Future<void> Function(String)? onDiscuss}) =>
    FeedView(api: api, onDiscuss: onDiscuss ?? (_) async {});

void main() {
  test('feed source URLs allow only absolute HTTP and HTTPS addresses', () {
    expect(
      feedSourceUrl('https://example.com/news'),
      'https://example.com/news',
    );
    expect(feedSourceUrl('http://example.com/news'), 'http://example.com/news');
    for (final url in [
      'javascript:alert(1)',
      'file:///tmp/file',
      'data:text/html,html',
      'ftp://example.com/file',
      '//example.com/file',
      '/file',
      'https:',
      'https://user:password@example.com/file',
    ]) {
      expect(feedSourceUrl(url), isNull, reason: url);
    }
    expect(feedSourceUrl(null), isNull);
  });

  testWidgets('ideas show personalized rows before grouped templates', (
    tester,
  ) async {
    final api = _ContentApi();
    addTearDown(api.close);
    await tester.pumpWidget(_shell(_ideas(api)));
    await tester.pumpAndSettle();

    expect(find.text('为你推荐'), findsOneWidget);
    expect(find.text('效率提升'), findsOneWidget);
    expect(
      tester.getTopLeft(find.text(_idea['title'] as String)).dy,
      lessThan(tester.getTopLeft(find.text('我可以安排明天的工作')).dy),
    );
    await tester.tap(find.byKey(const ValueKey('ideas-refresh')));
    await tester.pumpAndSettle();
    expect(api.refreshes, 1);
    expect(api.ideaReads, 2);
  });

  testWidgets(
    'generating ideas poll every five seconds and stop after six reloads',
    (tester) async {
      final api = _ContentApi()
        ..ideaResponse = (_) async => {
          'featured': [_idea],
          'groups': [],
          'generating': true,
        };
      addTearDown(api.close);
      await tester.pumpWidget(_shell(_ideas(api)));
      await tester.pump();
      expect(api.ideaReads, 1);
      await tester.pump(const Duration(seconds: 4));
      expect(api.ideaReads, 1);
      await tester.pump(const Duration(seconds: 1));
      await tester.pump();
      expect(api.ideaReads, 2);
      for (var index = 0; index < 5; index++) {
        await tester.pump(const Duration(seconds: 5));
        await tester.pump();
      }
      expect(api.ideaReads, 7);
      expect(find.text('还在生成点子，稍后下拉刷新查看。'), findsOneWidget);
      await tester.pump(const Duration(seconds: 10));
      expect(api.ideaReads, 7);
    },
  );

  testWidgets('polling stops when generation ends or the page is disposed', (
    tester,
  ) async {
    final api = _ContentApi()
      ..ideaResponse = (read) async => {
        'featured': [_idea],
        'groups': [],
        'generating': read == 1,
      };
    addTearDown(api.close);
    await tester.pumpWidget(_shell(_ideas(api)));
    await tester.pump();
    await tester.pump(const Duration(seconds: 5));
    await tester.pump();
    expect(api.ideaReads, 2);
    expect(find.byKey(const ValueKey('ideas-generating')), findsNothing);
    await tester.pump(const Duration(seconds: 10));
    expect(api.ideaReads, 2);

    final pollingApi = _ContentApi()
      ..ideaResponse = (_) async => {
        'featured': [],
        'groups': [],
        'generating': true,
      };
    addTearDown(pollingApi.close);
    await tester.pumpWidget(_shell(_ideas(pollingApi)));
    await tester.pump();
    await tester.pumpWidget(const SizedBox.shrink());
    await tester.pump(const Duration(seconds: 10));
    expect(pollingApi.ideaReads, 1);
    expect(tester.takeException(), isNull);
  });

  testWidgets(
    'start creates a session then sends returned prompt through the normal callback',
    (tester) async {
      final api = _ContentApi();
      addTearDown(api.close);
      await tester.pumpWidget(
        _shell(
          _ideas(
            api,
            onStart: (id, prompt) async {
              api.calls.add('send:$id:$prompt');
            },
          ),
        ),
      );
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const ValueKey('idea-idea-1')));
      await tester.pumpAndSettle();
      expect(find.byType(GlassSurface), findsOneWidget);
      expect(find.byType(LumaMarkdown), findsOneWidget);
      expect(find.text('灵感来自'), findsOneWidget);
      await tester.tap(find.byKey(const ValueKey('idea-start')));
      await tester.pumpAndSettle();
      expect(api.calls, [
        'start:idea-1',
        'send:started-session:我们开始做这个：周报\n\n具体计划',
      ]);
      expect(find.byKey(const ValueKey('idea-start')), findsNothing);
    },
  );

  testWidgets('idea inspiration sources open the source conversation', (
    tester,
  ) async {
    final api = _ContentApi();
    addTearDown(api.close);
    String? selected;
    await tester.pumpWidget(
      _shell(_ideas(api, onSelectSession: (id) => selected = id)),
    );
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('idea-idea-1')));
    await tester.pumpAndSettle();
    await tester.tap(find.text('项目进度'));
    await tester.pumpAndSettle();
    expect(selected, 'source-1');
    expect(find.byKey(const ValueKey('idea-start')), findsNothing);
  });

  testWidgets('feed renders safe source buttons and copies the valid URL', (
    tester,
  ) async {
    final api = _ContentApi();
    addTearDown(api.close);
    String? copied;
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(SystemChannels.platform, (call) async {
          if (call.method == 'Clipboard.setData') {
            copied = (call.arguments as Map)['text'] as String;
          }
          return null;
        });
    addTearDown(
      () => TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
          .setMockMethodCallHandler(SystemChannels.platform, null),
    );
    await tester.pumpWidget(_shell(_feed(api)));
    await tester.pumpAndSettle();
    expect(find.byType(LumaMarkdown), findsOneWidget);
    expect(find.text('HTTPS 来源'), findsOneWidget);
    expect(find.text('HTTP 来源'), findsOneWidget);
    expect(find.text('脚本来源'), findsNothing);
    expect(find.text('文件来源'), findsNothing);
    expect(find.text('相对来源'), findsNothing);
    await tester.tap(find.text('HTTPS 来源'));
    await tester.pump();
    expect(copied, 'https://example.com/news');
  });

  testWidgets('discuss opens the session returned by the server', (
    tester,
  ) async {
    final api = _ContentApi();
    addTearDown(api.close);
    String? selected;
    await tester.pumpWidget(
      _shell(_feed(api, onDiscuss: (id) async => selected = id)),
    );
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('feed-discuss-post-1')));
    await tester.pumpAndSettle();
    expect(api.calls, ['discuss:post-1']);
    expect(selected, 'discussion-session');
  });

  testWidgets('feed pagination appends posts and suppresses duplicates', (
    tester,
  ) async {
    final api = _ContentApi()
      ..feedResponse = (cursor) async => cursor == null
          ? {
              'items': [_post],
              'next_cursor': 'page-2',
            }
          : {
              'items': [
                _post,
                {..._post, 'id': 'post-2', 'title': '第二篇动态'},
              ],
              'next_cursor': null,
            };
    addTearDown(api.close);
    await tester.pumpWidget(_shell(_feed(api)));
    await tester.pumpAndSettle();
    await tester.ensureVisible(find.byKey(const ValueKey('feed-load-more')));
    await tester.pumpAndSettle();
    if (find.byKey(const ValueKey('feed-load-more')).evaluate().isNotEmpty) {
      await tester.tap(find.byKey(const ValueKey('feed-load-more')));
      await tester.pumpAndSettle();
    }
    expect(api.feedCursors, [null, 'page-2']);
    expect(find.byKey(const ValueKey('feed-post-post-1')), findsOneWidget);
    expect(find.byKey(const ValueKey('feed-post-post-2')), findsOneWidget);
    expect(find.byKey(const ValueKey('feed-load-more')), findsNothing);
  });

  testWidgets(
    'feed instructions save without resetting proactive preferences',
    (tester) async {
      final api = _ContentApi();
      addTearDown(api.close);
      await tester.pumpWidget(_shell(_feed(api)));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const ValueKey('feed-instructions')));
      await tester.pumpAndSettle();
      expect(find.byType(GlassSurface), findsOneWidget);
      await tester.enterText(
        find.byKey(const ValueKey('feed-instructions-text')),
        '关注 AI 工具，附上来源',
      );
      await tester.tap(find.byKey(const ValueKey('feed-instructions-save')));
      await tester.pumpAndSettle();
      expect(api.savedPrefs, {
        'enabled': false,
        'max_per_day': 2,
        'feed_instructions': '关注 AI 工具，附上来源',
      });
      expect(
        find.byKey(const ValueKey('feed-instructions-text')),
        findsNothing,
      );
    },
  );

  testWidgets('feed like toggles using the contract actions', (tester) async {
    final api = _ContentApi();
    addTearDown(api.close);
    await tester.pumpWidget(_shell(_feed(api)));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('feed-like-post-1')));
    await tester.pumpAndSettle();
    expect(find.byTooltip('取消喜欢'), findsOneWidget);
    await tester.tap(find.byKey(const ValueKey('feed-like-post-1')));
    await tester.pumpAndSettle();
    expect(api.feedFeedbacks, ['post-1:like', 'post-1:unlike']);
  });

  testWidgets('unsupported preferences do not open the instructions editor', (
    tester,
  ) async {
    final api = _ContentApi()..instructionsSupported = false;
    addTearDown(api.close);
    await tester.pumpWidget(_shell(_feed(api)));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('feed-instructions')));
    await tester.pumpAndSettle();
    expect(find.text('当前服务暂不支持动态说明'), findsOneWidget);
    expect(find.byKey(const ValueKey('feed-instructions-text')), findsNothing);
    expect(api.savedPrefs, isNull);
  });

  testWidgets(
    'unsupported preference updates keep the editor open with an error',
    (tester) async {
      final api = _ContentApi()..updatesSupported = false;
      addTearDown(api.close);
      await tester.pumpWidget(_shell(_feed(api)));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const ValueKey('feed-instructions')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const ValueKey('feed-instructions-save')));
      await tester.pumpAndSettle();
      expect(find.text('当前服务暂不支持保存动态说明'), findsOneWidget);
      expect(
        find.byKey(const ValueKey('feed-instructions-text')),
        findsOneWidget,
      );
    },
  );

  testWidgets('disposed pages ignore pending network reads', (tester) async {
    final read = Completer<Map<String, dynamic>>();
    final api = _ContentApi()..ideaResponse = (_) => read.future;
    addTearDown(api.close);
    await tester.pumpWidget(_shell(_ideas(api)));
    await tester.pumpWidget(const SizedBox.shrink());
    read.complete({'featured': [], 'groups': [], 'generating': true});
    await tester.pump();
    await tester.pump(const Duration(seconds: 10));
    expect(api.ideaReads, 1);
    expect(tester.takeException(), isNull);
  });
}
