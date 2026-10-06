import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:luma_client/api.dart';

class _ApiClient extends http.BaseClient {
  _ApiClient(this.handler);

  final http.StreamedResponse Function(http.BaseRequest request) handler;
  final List<http.BaseRequest> requests = <http.BaseRequest>[];

  @override
  Future<http.StreamedResponse> send(http.BaseRequest request) async {
    requests.add(request);
    return handler(request);
  }
}

http.StreamedResponse _response(Object? body, {int status = 200}) {
  final bytes = body == null ? <int>[] : utf8.encode(jsonEncode(body));
  return http.StreamedResponse(http.ByteStream.fromBytes(bytes), status);
}

void main() {
  test('permissions reads the items envelope', () async {
    final client = _ApiClient(
      (_) => _response({
        'items': [
          {'key': 'luma.routines.create', 'mode': 'ask', 'allow_always': true},
        ],
      }),
    );
    final api = AssistantApi(client: client);
    addTearDown(api.close);

    final items = await api.permissions();

    expect(items, hasLength(1));
    expect(items.single['key'], 'luma.routines.create');
    expect(items.single['allow_always'], isTrue);
  });

  test('legacy permissions endpoint returns an empty list on 404', () async {
    final api = AssistantApi(
      client: _ApiClient((_) => _response(null, status: 404)),
    );
    addTearDown(api.close);

    expect(await api.permissions(), isEmpty);
  });

  test('setPermission sends the encoded key and mode', () async {
    late http.BaseRequest request;
    final client = _ApiClient((incoming) {
      request = incoming;
      return _response({'key': 'mcp:connector:tool', 'mode': 'always'});
    });
    final api = AssistantApi(client: client);
    addTearDown(api.close);

    final item = await api.setPermission('mcp:connector:tool', 'always');

    expect(request.method, 'PUT');
    expect(request.url.pathSegments.last, 'mcp:connector:tool');
    expect((request as http.Request).body, jsonEncode({'mode': 'always'}));
    expect(item['mode'], 'always');
  });

  test('setPermission surfaces an unknown permission as 404', () async {
    final api = AssistantApi(
      client: _ApiClient((_) => _response(null, status: 404)),
    );
    addTearDown(api.close);

    await expectLater(
      api.setPermission('unknown.permission', 'always'),
      throwsA(
        isA<AssistantApiException>().having(
          (error) => error.statusCode,
          'statusCode',
          404,
        ),
      ),
    );
  });

  test('sandboxStatus reads state and treats a legacy 404 as absent', () async {
    final stateClient = _ApiClient(
      (_) => _response({
        'state': 'paused',
        'last_seen_at': '2026-10-04T00:00:00Z',
      }),
    );
    final api = AssistantApi(client: stateClient);
    addTearDown(api.close);

    expect((await api.sandboxStatus())?['state'], 'paused');

    final legacyApi = AssistantApi(
      client: _ApiClient((_) => _response(null, status: 404)),
    );
    addTearDown(legacyApi.close);
    expect(await legacyApi.sandboxStatus(), isNull);
  });

  test('resetSandbox accepts success and a legacy 404', () async {
    final statuses = <int>[204, 404];
    final client = _ApiClient(
      (_) => _response(null, status: statuses.removeAt(0)),
    );
    final api = AssistantApi(client: client);
    addTearDown(api.close);

    await api.resetSandbox();
    await api.resetSandbox();
    expect(
      client.requests.map((request) => request.url.path),
      everyElement('/api/v1/sandbox/reset'),
    );
  });

  test('decideApproval includes the remember flag', () async {
    late http.Request request;
    final client = _ApiClient((incoming) {
      request = incoming as http.Request;
      return _response({'id': 'approval-1', 'status': 'approved'});
    });
    final api = AssistantApi(client: client);
    addTearDown(api.close);

    final result = await api.decideApproval('approval-1', true, remember: true);

    expect(request.method, 'POST');
    expect(request.url.path, '/api/v1/runtime/approvals/approval-1/decision');
    expect(jsonDecode(request.body), {'decision': 'approve', 'remember': true});
    expect(result?['status'], 'approved');
  });
}
