import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:luma_client/api.dart';
import 'package:luma_client/theme.dart';
import 'package:luma_client/views/search.dart';

class _SearchApi extends AssistantApi {
  _SearchApi(this.handler);

  final Future<SearchResults> Function(String) handler;
  final List<String> queries = [];

  @override
  Future<SearchResults> search(String q) {
    queries.add(q);
    return handler(q);
  }
}

const _emptyResults = SearchResults(query: '', sessions: [], messages: []);

Future<void> _showSearch(
  WidgetTester tester,
  AssistantApi api, {
  List<Map<String, dynamic>> sessions = const [],
  ValueChanged<String>? onSelectSession,
}) async {
  await tester.pumpWidget(
    MaterialApp(
      theme: lumaTheme,
      home: Builder(
        builder: (context) => Scaffold(
          body: Center(
            child: TextButton(
              onPressed: () => Navigator.of(context).push(
                MaterialPageRoute<void>(
                  builder: (_) => SearchView(
                    api: api,
                    sessions: sessions,
                    onSelectSession: onSelectSession ?? (_) {},
                  ),
                ),
              ),
              child: const Text('打开搜索'),
            ),
          ),
        ),
      ),
    ),
  );
  await tester.tap(find.text('打开搜索'));
  await tester.pumpAndSettle();
}

void main() {
  testWidgets('search debounce waits 300ms and keeps only the last query', (
    tester,
  ) async {
    final queries = <String>[];
    final debounce = SearchDebouncer(queries.add);
    addTearDown(debounce.dispose);

    debounce.schedule('first');
    await tester.pump(const Duration(milliseconds: 200));
    debounce.schedule('  second  ');
    await tester.pump(const Duration(milliseconds: 299));
    expect(queries, isEmpty);
    await tester.pump(const Duration(milliseconds: 1));
    expect(queries, ['second']);
  });

  testWidgets('clearing input and disposing cancel pending search timers', (
    tester,
  ) async {
    final queries = <String>[];
    final debounce = SearchDebouncer(queries.add);

    debounce.schedule('first');
    await tester.pump(const Duration(milliseconds: 100));
    debounce.schedule('  ');
    await tester.pump(const Duration(milliseconds: 300));
    expect(queries, isEmpty);
    debounce.schedule('second');
    debounce.dispose();
    debounce.schedule('third');
    await tester.pump(const Duration(milliseconds: 300));
    expect(queries, isEmpty);
  });

  testWidgets(
    'empty search shows recent sessions and opens their conversation',
    (tester) async {
      final api = _SearchApi((_) async => _emptyResults);
      addTearDown(api.close);
      String? selected;
      await _showSearch(
        tester,
        api,
        sessions: const [
          {'id': 'side-1', 'title': '旅行计划'},
        ],
        onSelectSession: (id) => selected = id,
      );

      expect(find.text('最近访问'), findsOneWidget);
      expect(api.queries, isEmpty);
      await tester.tap(find.text('旅行计划'));
      await tester.pumpAndSettle();

      expect(selected, 'side-1');
      expect(find.byType(SearchView), findsNothing);
    },
  );

  testWidgets('search groups plain-text hits and opens a message session_id', (
    tester,
  ) async {
    final api = _SearchApi(
      (query) async => SearchResults(
        query: query,
        sessions: const [
          {'id': 'side-1', 'title': '灵感收集'},
        ],
        messages: const [
          {
            'id': 'message-1',
            'session_id': 'side-2',
            'session_title': '工作笔记',
            'snippet': '今天的新灵感',
          },
        ],
      ),
    );
    addTearDown(api.close);
    String? selected;
    await _showSearch(tester, api, onSelectSession: (id) => selected = id);

    await tester.enterText(find.byType(TextField), '灵感');
    await tester.pump(const Duration(milliseconds: 299));
    expect(api.queries, isEmpty);
    await tester.pump(const Duration(milliseconds: 1));
    await tester.pump();

    expect(api.queries, ['灵感']);
    expect(find.text('对话'), findsOneWidget);
    expect(find.text('消息'), findsOneWidget);
    final snippet = tester.widget<Text>(find.text('今天的新灵感'));
    expect(
      (snippet.textSpan! as TextSpan).children!
          .whereType<TextSpan>()
          .singleWhere((span) => span.text == '灵感')
          .style!
          .fontWeight,
      FontWeight.w600,
    );
    await tester.tap(find.text('今天的新灵感'));
    await tester.pumpAndSettle();
    expect(selected, 'side-2');
  });

  testWidgets('late search responses cannot overwrite the latest query', (
    tester,
  ) async {
    final oldResponse = Completer<SearchResults>();
    final newResponse = Completer<SearchResults>();
    final api = _SearchApi(
      (query) => query == 'old' ? oldResponse.future : newResponse.future,
    );
    addTearDown(api.close);
    await _showSearch(tester, api);

    await tester.enterText(find.byType(TextField), 'old');
    await tester.pump(const Duration(milliseconds: 300));
    await tester.enterText(find.byType(TextField), 'new');
    await tester.pump(const Duration(milliseconds: 300));
    newResponse.complete(
      const SearchResults(
        query: 'new',
        sessions: [
          {'id': 'new-session', 'title': 'new conversation'},
        ],
        messages: [],
      ),
    );
    await tester.pump();
    oldResponse.complete(
      const SearchResults(
        query: 'old',
        sessions: [
          {'id': 'old-session', 'title': 'old conversation'},
        ],
        messages: [],
      ),
    );
    await tester.pump();

    expect(find.text('new conversation'), findsOneWidget);
    expect(find.text('old conversation'), findsNothing);
  });

  testWidgets(
    'clearing input ignores in-flight results and restores recent visits',
    (tester) async {
      final response = Completer<SearchResults>();
      final api = _SearchApi((_) => response.future);
      addTearDown(api.close);
      await _showSearch(tester, api);
      await tester.enterText(find.byType(TextField), 'old');
      await tester.pump(const Duration(milliseconds: 300));
      await tester.tap(find.byTooltip('清除搜索'));
      response.complete(
        const SearchResults(
          query: 'old',
          sessions: [
            {'id': 'old-session', 'title': 'old conversation'},
          ],
          messages: [],
        ),
      );
      await tester.pump();

      expect(find.text('最近访问'), findsOneWidget);
      expect(find.text('old conversation'), findsNothing);
      expect(api.queries, ['old']);
    },
  );

  testWidgets('search errors offer a retry without exposing a raw exception', (
    tester,
  ) async {
    var attempts = 0;
    final api = _SearchApi((_) async {
      attempts++;
      if (attempts == 1) throw StateError('internal detail');
      return _emptyResults;
    });
    addTearDown(api.close);
    await _showSearch(tester, api);
    await tester.enterText(find.byType(TextField), 'Luma');
    await tester.pump(const Duration(milliseconds: 300));
    await tester.pump();

    expect(find.text('搜索暂时不可用，请重试'), findsOneWidget);
    expect(find.textContaining('internal detail'), findsNothing);
    await tester.tap(find.text('重试'));
    await tester.pump();
    await tester.pump();
    expect(attempts, 2);
    expect(find.text('没有匹配的消息'), findsOneWidget);
  });

  testWidgets(
    'leaving search cancels the debounce and ignores a late response',
    (tester) async {
      final response = Completer<SearchResults>();
      final api = _SearchApi((_) => response.future);
      addTearDown(api.close);
      await _showSearch(tester, api);
      await tester.enterText(find.byType(TextField), 'Luma');
      await tester.pump(const Duration(milliseconds: 300));
      await tester.tap(find.byTooltip('返回'));
      await tester.pumpAndSettle();
      response.complete(_emptyResults);
      await tester.pump();

      expect(find.byType(SearchView), findsNothing);
      expect(tester.takeException(), isNull);
    },
  );
}
