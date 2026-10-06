import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:luma_client/api.dart';

class _ResourceClient extends http.BaseClient {
  _ResourceClient(this.handler);

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

const _prefs = {
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
  'feed_instructions': '每日技术资讯',
};

void main() {
  test(
    'library sends contract filters and keeps authenticated results',
    () async {
      final api = AssistantApi(
        token: 'resource-test-token',
        client: _ResourceClient((request) {
          expect(request.method, 'GET');
          expect(request.url.path, '/api/v1/library');
          expect(request.url.queryParameters, {
            'type': 'sheet',
            'q': '报表 & 总计',
            'sort': 'title',
            'limit': '12',
            'cursor': 'next/value',
          });
          expect(
            request.headers['authorization'],
            'Bearer resource-test-token',
          );
          return _response({
            'items': [
              {'id': 'file-1', 'title': '报表', 'type': 'sheet', 'pinned': true},
            ],
            'next_cursor': 'next-2',
            'counts': {'all': 4, 'sheet': 1},
          });
        }),
      );
      addTearDown(api.close);

      final result = await api.library(
        type: 'sheet',
        q: '  报表 & 总计  ',
        sort: 'title',
        limit: 12,
        cursor: 'next/value',
      );
      expect(result['items'].single['pinned'], isTrue);
      expect(result['counts']['sheet'], 1);
      expect(result['next_cursor'], 'next-2');
    },
  );

  for (final preview in [
    {
      'kind': 'csv',
      'columns': ['项目'],
      'rows': [
        ['Luma'],
      ],
      'total_rows': 1,
    },
    {'kind': 'markdown', 'text': '# 计划', 'truncated': false},
    {'kind': 'text', 'text': 'print(1)', 'language': 'python'},
    {'kind': 'html_source', 'text': '<script>unsafe()</script>'},
    {'kind': 'image'},
    {'kind': 'none'},
  ]) {
    test(
      'library parses ${preview['kind']} preview without changing content',
      () async {
        final api = AssistantApi(
          client: _ResourceClient((request) {
            expect(request.url.pathSegments, [
              'api',
              'v1',
              'library',
              'file/id',
              'preview',
            ]);
            return _response(preview);
          }),
        );
        addTearDown(api.close);
        expect(await api.libraryPreview('file/id'), preview);
      },
    );
  }

  test(
    'library updates optional fields and deletes through files endpoint',
    () async {
      final calls = <String>[];
      final api = AssistantApi(
        client: _ResourceClient((request) {
          calls.add('${request.method} ${request.url.path}');
          if (request.method == 'PATCH') {
            expect(jsonDecode(request.body), {'title': '新标题', 'pinned': true});
            return _response({'id': 'file-1', 'title': '新标题', 'pinned': true});
          }
          return _response(null, status: 204);
        }),
      );
      addTearDown(api.close);
      expect(
        (await api.updateLibrary(
          'file-1',
          title: '新标题',
          pinned: true,
        ))['title'],
        '新标题',
      );
      await api.deleteFile('file-1');
      expect(calls, [
        'PATCH /api/v1/library/file-1',
        'DELETE /api/v1/files/file-1',
      ]);
    },
  );

  test(
    'ideas parses groups, sends feedback and returns start prompt',
    () async {
      final calls = <String>[];
      final api = AssistantApi(
        client: _ResourceClient((request) {
          calls.add('${request.method} ${request.url.path}');
          switch (request.url.path) {
            case '/api/v1/ideas':
              return _response({
                'featured': [
                  {'id': 'idea-1', 'is_template': false},
                ],
                'groups': [
                  {
                    'name': '效率提升',
                    'items': [
                      {'id': 'template-1'},
                    ],
                  },
                ],
                'generated_at': '2026-10-06T08:00:00Z',
                'generating': true,
              });
            case '/api/v1/ideas/idea-1/start':
              return _response({
                'session_id': 'side-1',
                'prompt': '我们开始做这个：日报',
              });
            case '/api/v1/ideas/idea-1/feedback':
              expect(jsonDecode(request.body), {'action': 'more_like'});
              return _response(null, status: 204);
            default:
              return _response(null, status: 202);
          }
        }),
      );
      addTearDown(api.close);
      final result = await api.ideas();
      expect(result['generating'], isTrue);
      expect(result['groups'].single['name'], '效率提升');
      await api.refreshIdeas();
      await api.ideaFeedback('idea-1', 'more_like');
      expect(await api.startIdea('idea-1'), {
        'session_id': 'side-1',
        'prompt': '我们开始做这个：日报',
      });
      expect(calls, [
        'GET /api/v1/ideas',
        'POST /api/v1/ideas/refresh',
        'POST /api/v1/ideas/idea-1/feedback',
        'POST /api/v1/ideas/idea-1/start',
      ]);
    },
  );

  test('feed sends pagination and contract action methods', () async {
    final calls = <String>[];
    final api = AssistantApi(
      client: _ResourceClient((request) {
        calls.add('${request.method} ${request.url.path}');
        if (request.method == 'GET') {
          expect(request.url.queryParameters, {
            'limit': '7',
            'cursor': 'cursor/1',
          });
          return _response({
            'items': [
              {'id': 'post-1', 'liked': true},
            ],
            'next_cursor': 'cursor-2',
          });
        }
        if (request.url.path.endsWith('/discuss')) {
          return _response({'session_id': 'side-2'});
        }
        if (request.url.path.endsWith('/feedback')) {
          expect(jsonDecode(request.body), {'action': 'unlike'});
        }
        return _response(
          null,
          status: request.url.path.endsWith('/refresh') ? 202 : 204,
        );
      }),
    );
    addTearDown(api.close);
    final feed = await api.feed(limit: 7, cursor: 'cursor/1');
    expect(feed['items'].single['liked'], isTrue);
    expect(feed['next_cursor'], 'cursor-2');
    await api.feedFeedback('post-1', 'unlike');
    await api.deleteFeedPost('post-1');
    expect(await api.discussFeedPost('post-1'), {'session_id': 'side-2'});
    await api.refreshFeed();
    expect(calls, [
      'GET /api/v1/feed',
      'POST /api/v1/feed/post-1/feedback',
      'DELETE /api/v1/feed/post-1',
      'POST /api/v1/feed/post-1/discuss',
      'POST /api/v1/feed/refresh',
    ]);
  });

  test('proactive prefs reads and saves every contract field', () async {
    final methods = <String>[];
    final api = AssistantApi(
      client: _ResourceClient((request) {
        expect(request.url.path, '/api/v1/proactive/prefs');
        methods.add(request.method);
        if (request.method == 'PUT') expect(jsonDecode(request.body), _prefs);
        return _response(_prefs);
      }),
    );
    addTearDown(api.close);
    expect(await api.proactivePrefs(), _prefs);
    expect(await api.updateProactivePrefs(_prefs), _prefs);
    expect(methods, ['GET', 'PUT']);
  });

  test('new resource endpoints tolerate legacy 404 responses', () async {
    final api = AssistantApi(
      client: _ResourceClient((_) => _response(null, status: 404)),
    );
    addTearDown(api.close);
    final library = await api.library();
    expect(library['items'], isEmpty);
    expect(library['counts'], {
      'all': 0,
      'document': 0,
      'sheet': 0,
      'web': 0,
      'image': 0,
      'code': 0,
      'archive': 0,
      'other': 0,
    });
    expect(await api.libraryPreview('missing'), {'kind': 'none'});
    expect(await api.updateLibrary('missing', pinned: true), isEmpty);
    await api.deleteFile('missing');
    expect(await api.listMemories(), isEmpty);
    expect(await api.ideas(), {
      'featured': [],
      'groups': [],
      'generated_at': null,
      'generating': false,
    });
    await api.refreshIdeas();
    await api.ideaFeedback('missing', 'not_interested');
    expect(await api.startIdea('missing'), isEmpty);
    expect(await api.feed(), {'items': [], 'next_cursor': null});
    await api.feedFeedback('missing', 'not_interested');
    await api.deleteFeedPost('missing');
    expect(await api.discussFeedPost('missing'), isEmpty);
    await api.refreshFeed();
    expect(await api.proactivePrefs(), isEmpty);
    expect(await api.updateProactivePrefs(_prefs), isEmpty);
  });

  final operations = <String, Future<dynamic> Function(AssistantApi)>{
    'library': (api) => api.library(),
    'preview': (api) => api.libraryPreview('file'),
    'update library': (api) => api.updateLibrary('file', pinned: true),
    'delete file': (api) => api.deleteFile('file'),
    'ideas': (api) => api.ideas(),
    'refresh ideas': (api) => api.refreshIdeas(),
    'idea feedback': (api) => api.ideaFeedback('idea', 'more_like'),
    'start idea': (api) => api.startIdea('idea'),
    'feed': (api) => api.feed(),
    'feed feedback': (api) => api.feedFeedback('post', 'like'),
    'delete feed': (api) => api.deleteFeedPost('post'),
    'discuss feed': (api) => api.discussFeedPost('post'),
    'refresh feed': (api) => api.refreshFeed(),
    'get prefs': (api) => api.proactivePrefs(),
    'save prefs': (api) => api.updateProactivePrefs(_prefs),
  };
  for (final operation in operations.entries) {
    test('${operation.key} keeps auth failure and its status', () async {
      var unauthorized = 0;
      final api = AssistantApi(
        onUnauthorized: () => unauthorized++,
        client: _ResourceClient(
          (_) => _response({'detail': '登录已失效'}, status: 401),
        ),
      );
      addTearDown(api.close);
      await expectLater(
        operation.value(api),
        throwsA(
          isA<AssistantApiException>().having(
            (error) => error.statusCode,
            'status',
            401,
          ),
        ),
      );
      expect(unauthorized, 1);
    });
  }

  test('generation limits remain actionable 429 errors', () async {
    final api = AssistantApi(
      client: _ResourceClient(
        (_) => _response({'detail': '今天已达到生成次数上限'}, status: 429),
      ),
    );
    addTearDown(api.close);
    for (final refresh in [api.refreshIdeas, api.refreshFeed]) {
      await expectLater(
        refresh(),
        throwsA(
          isA<AssistantApiException>().having(
            (error) => error.statusCode,
            'status',
            429,
          ),
        ),
      );
    }
  });

  test('resource APIs reject malformed successful envelopes', () async {
    final api = AssistantApi(
      client: _ResourceClient(
        (_) => _response({'kind': 'webview', 'items': null}),
      ),
    );
    addTearDown(api.close);
    for (final operation in [
      api.library,
      () => api.libraryPreview('file'),
      api.ideas,
      api.feed,
      () => api.startIdea('idea'),
      () => api.discussFeedPost('post'),
    ]) {
      await expectLater(operation(), throwsA(isA<AssistantApiException>()));
    }
  });

  test('feedback rejects actions outside the contract whitelist', () {
    final api = AssistantApi();
    addTearDown(api.close);
    expect(
      () => api.ideaFeedback('idea', 'delete'),
      throwsA(isA<AssistantApiException>()),
    );
    expect(
      () => api.feedFeedback('post', 'start'),
      throwsA(isA<AssistantApiException>()),
    );
  });
}
