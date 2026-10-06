import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:luma_client/api.dart';

Map<String, dynamic> _report() => {
  'range': '30d',
  'totals': {'calls': 2, 'total_tokens': 350},
  'performance': {'first_token_ms_avg': 45},
  'daily': [],
  'purposes': [],
  'models': [],
  'top_sessions': [],
};

void main() {
  test(
    'getSession uses authenticated encoded lookup before navigation',
    () async {
      final api = AssistantApi(
        token: 'test-session',
        client: MockClient((request) async {
          expect(request.method, 'GET');
          expect(request.url.toString(), contains('/sessions/side%2F1'));
          expect(request.headers['authorization'], 'Bearer test-session');
          return http.Response('{"id":"side/1"}', 200);
        }),
      );
      addTearDown(api.close);
      expect((await api.getSession('side/1'))['id'], 'side/1');
    },
  );

  test(
    'getSession rejects deleted sessions and mismatched lookup results',
    () async {
      for (final response in [
        http.Response('{"detail":"Session not found"}', 404),
        http.Response('{"id":"another-session"}', 200),
      ]) {
        final api = AssistantApi(client: MockClient((_) async => response));
        addTearDown(api.close);
        await expectLater(
          api.getSession('session-1'),
          throwsA(isA<AssistantApiException>()),
        );
      }
    },
  );

  test(
    'usage sends authenticated range request and preserves aggregates',
    () async {
      final api = AssistantApi(
        token: 'test-session',
        client: MockClient((request) async {
          expect(request.method, 'GET');
          expect(request.url.path, '/api/v1/usage');
          expect(request.url.queryParameters, {'range': '30d'});
          expect(request.headers['authorization'], 'Bearer test-session');
          return http.Response(jsonEncode(_report()), 200);
        }),
      );
      addTearDown(api.close);
      final result = await api.usage(range: '30d');
      expect(result['totals']['total_tokens'], 350);
    },
  );

  test('usage rejects unsupported ranges before sending a request', () async {
    var requests = 0;
    final api = AssistantApi(
      client: MockClient((_) async {
        requests++;
        return http.Response('{}', 200);
      }),
    );
    addTearDown(api.close);
    await expectLater(
      api.usage(range: '1d'),
      throwsA(isA<AssistantApiException>()),
    );
    expect(requests, 0);
  });

  test('usage marks an unauthorized session and returns an error', () async {
    var unauthorized = 0;
    final api = AssistantApi(
      onUnauthorized: () => unauthorized++,
      client: MockClient(
        (_) async => http.Response(
          '{"detail":"登录已过期"}',
          401,
          headers: {'content-type': 'application/json; charset=utf-8'},
        ),
      ),
    );
    addTearDown(api.close);
    await expectLater(api.usage(), throwsA(isA<AssistantApiException>()));
    expect(unauthorized, 1);
  });

  test('usage rejects incomplete response structures', () async {
    final api = AssistantApi(
      client: MockClient((_) async => http.Response('{"totals":{}}', 200)),
    );
    addTearDown(api.close);
    await expectLater(
      api.usage(),
      throwsA(
        isA<AssistantApiException>().having(
          (error) => error.message,
          'message',
          '用量接口返回了无效响应',
        ),
      ),
    );
  });
}
