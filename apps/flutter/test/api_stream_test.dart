import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:luma_client/api.dart';

class _FakeClient extends http.BaseClient {
  _FakeClient(this.responseBuilder);

  final http.StreamedResponse Function(http.BaseRequest request)
  responseBuilder;
  http.BaseRequest? lastRequest;

  @override
  Future<http.StreamedResponse> send(http.BaseRequest request) async {
    lastRequest = request;
    return responseBuilder(request);
  }
}

http.StreamedResponse _jsonResponse(
  Object body, {
  Map<String, String>? headers,
}) {
  return http.StreamedResponse(
    http.ByteStream.fromBytes(utf8.encode(jsonEncode(body))),
    200,
    headers: headers ?? const <String, String>{},
  );
}

void main() {
  test(
    'generation limit returns the actionable concurrent reply message',
    () async {
      final client = _FakeClient(
        (_) => http.StreamedResponse(
          http.ByteStream.fromBytes(
            utf8.encode(
              jsonEncode({
                'detail': {'code': 'too_many_generations'},
              }),
            ),
          ),
          429,
        ),
      );
      final api = AssistantApi(client: client);
      addTearDown(api.close);

      await expectLater(
        api.streamMessage('session-1', '继续'),
        throwsA(
          isA<AssistantApiException>()
              .having((error) => error.statusCode, 'statusCode', 429)
              .having((error) => error.detail, 'detail', '同时进行的回复太多，请稍后'),
        ),
      );
    },
  );

  test('message pagination reads cursor headers and oldest id', () async {
    final client = _FakeClient(
      (_) => _jsonResponse(
        [
          {'id': 'm-1', 'role': 'user', 'content': '早消息'},
        ],
        headers: {'x-has-more': 'true', 'x-oldest-id': 'm-1'},
      ),
    );
    final api = AssistantApi(client: client);
    addTearDown(api.close);

    final page = await api.listMessages('session-1', before: 'm-2');

    expect(page.hasMore, isTrue);
    expect(page.oldestId, 'm-1');
    expect(page.messages.single['id'], 'm-1');
    expect(client.lastRequest!.url.queryParameters['before'], 'm-2');
  });

  test('resume stream sends after cursor and preserves SSE event id', () async {
    final client = _FakeClient(
      (_) => http.StreamedResponse(
        http.ByteStream.fromBytes(
          utf8.encode(
            'id: evt-2\n'
            'event: delta\n'
            'data: {"content":"继续"}\n\n',
          ),
        ),
        200,
        headers: {'content-type': 'text/event-stream'},
      ),
    );
    final api = AssistantApi(client: client);
    addTearDown(api.close);

    final stream = await api.streamMessageAfter('message-1', after: 'evt-1');
    final events = await stream.toList();

    expect(client.lastRequest!.url.queryParameters['after'], 'evt-1');
    expect(client.lastRequest!.headers['last-event-id'], 'evt-1');
    expect(events.single.name, 'delta');
    expect(events.single.id, 'evt-2');
    expect(events.single.data['content'], '继续');
  });
}
