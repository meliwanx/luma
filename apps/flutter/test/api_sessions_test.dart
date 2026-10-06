import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:luma_client/api.dart';

class _SessionClient extends http.BaseClient {
  _SessionClient(this.handler);

  final http.StreamedResponse Function(http.BaseRequest request) handler;

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
    headers: const {'content-type': 'application/json; charset=utf-8'},
  );
}

void main() {
  test('explicit session selection wins over main and list fallback', () {
    expect(
      AssistantApi.selectSessionId(
        selectedSessionId: 'side-1',
        mainSession: {'id': 'main-1'},
        sessions: [
          {'id': 'side-2'},
        ],
      ),
      'side-1',
    );
    expect(
      AssistantApi.selectSessionId(mainSession: {'id': 'main-1'}),
      'main-1',
    );
    expect(
      AssistantApi.selectSessionId(
        sessions: [
          {'id': 'legacy-1'},
        ],
      ),
      'legacy-1',
    );
  });

  test('mainSession treats a legacy 404 as unavailable', () async {
    final client = _SessionClient((request) {
      expect(request.url.path, '/api/v1/sessions/main');
      return _response(null, status: 404);
    });
    final api = AssistantApi(client: client);
    addTearDown(api.close);

    expect(await api.mainSession(), isNull);
  });

  test(
    'renameSession patches an encoded session id and trimmed title',
    () async {
      final client = _SessionClient((request) {
        expect(request.method, 'PATCH');
        expect(request.url.toString(), contains('/sessions/side%2F1'));
        expect(request.headers['content-type'], contains('application/json'));
        expect(jsonDecode((request as http.Request).body), {'title': '旅行安排'});
        return _response({'id': 'side/1', 'title': '旅行安排', 'kind': 'side'});
      });
      final api = AssistantApi(client: client);
      addTearDown(api.close);

      final session = await api.renameSession('side/1', ' 旅行安排 ');

      expect(session['title'], '旅行安排');
    },
  );

  test('renameSession surfaces failure without returning a session', () async {
    final api = AssistantApi(
      client: _SessionClient(
        (_) => _response({'detail': '会话不存在'}, status: 404),
      ),
    );
    addTearDown(api.close);

    await expectLater(
      api.renameSession('missing', '新标题'),
      throwsA(
        isA<AssistantApiException>().having(
          (error) => error.detail,
          'detail',
          '会话不存在',
        ),
      ),
    );
  });

  test(
    'dashboard falls back to main when the selected session is gone',
    () async {
      final client = _SessionClient((request) {
        final path = request.url.path;
        if (path == '/api/v1/sessions') {
          return _response([
            {'id': 'side-1', 'kind': 'side'},
            {'id': 'main-1', 'kind': 'main'},
          ]);
        }
        if (path == '/api/v1/sessions/main') {
          return _response({'id': 'main-1', 'kind': 'main'});
        }
        if (path == '/api/v1/sessions/side-1/messages') {
          return _response(null, status: 404);
        }
        if (path == '/api/v1/sessions/main-1/messages') {
          return _response([]);
        }
        return _response([]);
      });
      final api = AssistantApi(client: client);
      addTearDown(api.close);

      final dashboard = await api.dashboard(selectedSessionId: 'side-1');

      expect(dashboard?['conversation_id'], 'main-1');
      expect(dashboard?['selection_fallback'], isTrue);
    },
  );

  test(
    'dashboard uses the first session when main endpoint is legacy 404',
    () async {
      final client = _SessionClient((request) {
        final path = request.url.path;
        if (path == '/api/v1/sessions') {
          return _response([
            {'id': 'legacy-1', 'kind': 'side'},
          ]);
        }
        if (path == '/api/v1/sessions/main') {
          return _response(null, status: 404);
        }
        if (path == '/api/v1/sessions/legacy-1/messages') {
          return _response([]);
        }
        return _response([]);
      });
      final api = AssistantApi(client: client);
      addTearDown(api.close);

      final dashboard = await api.dashboard();

      expect(dashboard?['conversation_id'], 'legacy-1');
    },
  );
}
