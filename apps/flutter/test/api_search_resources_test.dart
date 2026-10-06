import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:luma_client/api.dart';

class _SearchClient extends http.BaseClient {
  _SearchClient(this.handler);

  final FutureOr<http.StreamedResponse> Function(http.BaseRequest) handler;

  @override
  Future<http.StreamedResponse> send(http.BaseRequest request) async {
    return handler(request);
  }
}

http.StreamedResponse _response(Object? body, {int status = 200}) {
  final bytes = body == null ? <int>[] : utf8.encode(jsonEncode(body));
  return http.StreamedResponse(
    http.ByteStream.fromBytes(bytes),
    status,
    headers: {'content-type': 'application/json; charset=utf-8'},
  );
}

void main() {
  test(
    'search encodes the query, keeps auth, and parses both result groups',
    () async {
      final api = AssistantApi(
        token: 'search-test-token',
        client: _SearchClient((request) {
          expect(request.method, 'GET');
          expect(request.url.path, '/api/v1/search');
          expect(request.url.queryParameters, {'q': 'Luma & 灵感'});
          expect(request.headers['authorization'], 'Bearer search-test-token');
          expect(request.headers['X-Luma-Client'], isNotEmpty);
          return _response({
            'query': 'Luma & 灵感',
            'sessions': [
              {'id': 'side-1', 'title': 'Luma & 灵感'},
              null,
            ],
            'messages': [
              {
                'id': 'message-1',
                'session_id': 'side-2',
                'session_title': '笔记',
                'snippet': '记录 Luma & 灵感',
              },
            ],
          });
        }),
      );
      addTearDown(api.close);

      final results = await api.search('  Luma & 灵感  ');

      expect(results.query, 'Luma & 灵感');
      expect(results.sessions, [
        {'id': 'side-1', 'title': 'Luma & 灵感'},
      ]);
      expect(results.messages.single['session_id'], 'side-2');
      expect(results.messages.single['snippet'], '记录 Luma & 灵感');
    },
  );

  test('search rejects a malformed result envelope', () async {
    final api = AssistantApi(
      client: _SearchClient((request) => _response({'sessions': []})),
    );
    addTearDown(api.close);

    await expectLater(
      api.search('Luma'),
      throwsA(
        isA<AssistantApiException>().having(
          (error) => error.message,
          'message',
          '搜索接口返回了无效响应',
        ),
      ),
    );
  });

  test('search invokes the existing unauthorized callback', () async {
    var unauthorized = 0;
    final api = AssistantApi(
      onUnauthorized: () => unauthorized++,
      client: _SearchClient(
        (request) => _response({'detail': '登录已失效'}, status: 401),
      ),
    );
    addTearDown(api.close);

    await expectLater(
      api.search('Luma'),
      throwsA(
        isA<AssistantApiException>().having(
          (error) => error.statusCode,
          'statusCode',
          401,
        ),
      ),
    );
    expect(unauthorized, 1);
  });

  test('search preserves service-unavailable handling', () async {
    final api = AssistantApi(
      client: _SearchClient((request) => _response(null, status: 503)),
    );
    addTearDown(api.close);

    await expectLater(
      api.search('Luma'),
      throwsA(
        isA<AssistantApiException>().having(
          (error) => error.isUnavailable,
          'isUnavailable',
          isTrue,
        ),
      ),
    );
  });

  test(
    'file and notification lists send filters and authenticated headers',
    () async {
      final paths = <String>[];
      final api = AssistantApi(
        token: 'resource-test-token',
        client: _SearchClient((request) {
          paths.add(request.url.path);
          expect(
            request.headers['authorization'],
            'Bearer resource-test-token',
          );
          if (request.url.path == '/api/v1/files') {
            expect(request.url.queryParameters, {
              'limit': '12',
              'session_id': 'side-1',
            });
            return _response([
              {'id': 'file-1', 'filename': '笔记.txt'},
            ]);
          }
          expect(request.url.queryParameters, {
            'limit': '15',
            'unread_only': 'true',
          });
          return _response([
            {'id': 'notice-1', 'read_at': null, 'title': '目标完成'},
          ]);
        }),
      );
      addTearDown(api.close);

      expect(
        (await api.listFiles(
          sessionId: ' side-1 ',
          limit: 12,
        )).single['filename'],
        '笔记.txt',
      );
      expect(
        (await api.listNotifications(unreadOnly: true, limit: 15)).single['id'],
        'notice-1',
      );
      expect(paths, ['/api/v1/files', '/api/v1/notifications']);
    },
  );

  test('legacy file and notification endpoints return empty lists', () async {
    final api = AssistantApi(
      client: _SearchClient((request) => _response(null, status: 404)),
    );
    addTearDown(api.close);

    expect(await api.listFiles(), isEmpty);
    expect(await api.listNotifications(), isEmpty);
  });

  test(
    'resource lists reject invalid payloads instead of hiding errors',
    () async {
      final api = AssistantApi(
        client: _SearchClient((request) => _response({'unexpected': true})),
      );
      addTearDown(api.close);

      await expectLater(api.listFiles(), throwsA(isA<AssistantApiException>()));
      await expectLater(
        api.listNotifications(),
        throwsA(isA<AssistantApiException>()),
      );
    },
  );

  test('resource lists retain unauthorized and unavailable handling', () async {
    var unauthorized = 0;
    final api = AssistantApi(
      onUnauthorized: () => unauthorized++,
      client: _SearchClient(
        (request) => _response(
          null,
          status: request.url.path == '/api/v1/files' ? 401 : 503,
        ),
      ),
    );
    addTearDown(api.close);

    await expectLater(
      api.listFiles(),
      throwsA(
        isA<AssistantApiException>().having(
          (error) => error.statusCode,
          'statusCode',
          401,
        ),
      ),
    );
    await expectLater(
      api.listNotifications(),
      throwsA(
        isA<AssistantApiException>().having(
          (error) => error.statusCode,
          'statusCode',
          503,
        ),
      ),
    );
    expect(unauthorized, 1);
  });

  test(
    'markNotificationRead posts an encoded id and parses the update',
    () async {
      final api = AssistantApi(
        token: 'resource-test-token',
        client: _SearchClient((request) {
          expect(request.method, 'POST');
          expect(request.url.pathSegments, [
            'api',
            'v1',
            'notifications',
            'notice/1',
            'read',
          ]);
          expect(
            request.headers['authorization'],
            'Bearer resource-test-token',
          );
          return _response({
            'id': 'notice/1',
            'read_at': '2026-10-04T00:00:00Z',
          });
        }),
      );
      addTearDown(api.close);

      expect(
        (await api.markNotificationRead('notice/1'))['read_at'],
        '2026-10-04T00:00:00Z',
      );
    },
  );

  test(
    'markNotificationRead does not claim a missing notification was read',
    () async {
      final api = AssistantApi(
        client: _SearchClient((request) => _response(null, status: 404)),
      );
      addTearDown(api.close);

      await expectLater(
        api.markNotificationRead('missing'),
        throwsA(
          isA<AssistantApiException>().having(
            (error) => error.statusCode,
            'statusCode',
            404,
          ),
        ),
      );
    },
  );
}
