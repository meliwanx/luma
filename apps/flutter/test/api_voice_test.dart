import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:luma_client/api.dart';

class _VoiceClient extends http.BaseClient {
  _VoiceClient(this.handler);

  final Future<http.StreamedResponse> Function(http.BaseRequest request)
  handler;

  @override
  Future<http.StreamedResponse> send(http.BaseRequest request) =>
      handler(request);
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
  const success = {
    'text': '帮我安排明天的会议',
    'transcript': '嗯帮我安排明天的会议',
    'cleaned': true,
    'duration_ms': 8320,
  };
  final recording = utf8.encode('RIFF-test-recording');

  test(
    'transcribe uploads authenticated multipart bytes in smart mode',
    () async {
      final api = AssistantApi(
        token: 'test-token',
        client: _VoiceClient((incoming) async {
          expect(incoming.method, 'POST');
          expect(incoming.url.path, '/api/v1/voice/transcribe');
          expect(incoming.headers['authorization'], 'Bearer test-token');
          final request = incoming as http.MultipartRequest;
          expect(request.fields, {'mode': 'smart'});
          expect(request.files.single.field, 'audio');
          expect(request.files.single.filename, 'voice.wav');
          final body = utf8.decode(await request.finalize().toBytes());
          expect(
            request.headers['content-type'],
            startsWith('multipart/form-data;'),
          );
          expect(body, contains('name="audio"; filename="voice.wav"'));
          expect(body, contains('RIFF-test-recording'));
          return _response(success);
        }),
      );
      addTearDown(api.close);

      expect(await api.transcribe(recording), success);
    },
  );

  test(
    'transcribe sends raw mode and trimmed optional session context',
    () async {
      const rawResult = {'text': '嗯明天', 'transcript': '嗯明天', 'cleaned': false};
      final api = AssistantApi(
        client: _VoiceClient((incoming) async {
          final request = incoming as http.MultipartRequest;
          expect(request.fields, {'mode': 'raw', 'session_id': 'session-1'});
          return _response(rawResult);
        }),
      );
      addTearDown(api.close);

      expect(
        await api.transcribe(recording, sessionId: ' session-1 ', raw: true),
        rawResult,
      );
    },
  );

  test('transcribe reads a File and preserves its audio filename', () async {
    final directory = await Directory.systemTemp.createTemp('luma-voice-api-');
    addTearDown(() => directory.delete(recursive: true));
    final file = await File('${directory.path}/recording.wav')
        .writeAsBytes(recording);
    final api = AssistantApi(
      client: _VoiceClient((incoming) async {
        final request = incoming as http.MultipartRequest;
        expect(request.fields, {'mode': 'smart'});
        expect(request.files.single.filename, 'recording.wav');
        expect(await request.files.single.finalize().toBytes(), recording);
        return _response(success);
      }),
    );
    addTearDown(api.close);

    expect(await api.transcribe(file, sessionId: ' '), success);
    expect(await file.exists(), isTrue);
  });

  test('transcribe keeps uncleaned smart-mode fallback results', () async {
    const fallback = {'text': '明天开会', 'transcript': '明天开会', 'cleaned': false};
    final api = AssistantApi(
      client: _VoiceClient((_) async => _response(fallback)),
    );
    addTearDown(api.close);

    expect(await api.transcribe(recording), fallback);
  });

  for (final entry in {
    413: '录音太长，请控制在 2 分钟内',
    422: '没听清，请靠近麦克风再说一次',
    429: '请求太频繁，请稍后再试',
    502: '语音识别服务暂时不可用',
    503: '语音服务未配置',
    504: '语音识别超时，请稍后重试',
  }.entries) {
    test('transcribe surfaces service detail for HTTP ${entry.key}', () async {
      final api = AssistantApi(
        client: _VoiceClient(
          (_) async => _response({'detail': entry.value}, status: entry.key),
        ),
      );
      addTearDown(api.close);

      await expectLater(
        api.transcribe(recording),
        throwsA(
          isA<AssistantApiException>()
              .having((error) => error.detail, 'detail', entry.value)
              .having((error) => error.statusCode, 'statusCode', entry.key),
        ),
      );
    });
  }

  test(
    'transcribe notifies unauthorized handler and reports server detail',
    () async {
      var unauthorizedCalls = 0;
      final api = AssistantApi(
        onUnauthorized: () => unauthorizedCalls++,
        client: _VoiceClient(
          (_) async => _response({'detail': '请先登录'}, status: 401),
        ),
      );
      addTearDown(api.close);

      await expectLater(
        api.transcribe(recording),
        throwsA(
          isA<AssistantApiException>()
              .having((error) => error.detail, 'detail', '请先登录')
              .having((error) => error.statusCode, 'statusCode', 401),
        ),
      );
      expect(unauthorizedCalls, 1);
    },
  );

  test(
    'transcribe rejects missing or wrongly typed successful fields',
    () async {
      for (final invalid in <Object?>[
        null,
        [],
        {'text': '文字', 'transcript': '原文'},
        {'text': 12, 'transcript': '原文', 'cleaned': true},
        {'text': '文字', 'transcript': null, 'cleaned': true},
        {'text': '文字', 'transcript': '原文', 'cleaned': 'true'},
      ]) {
        final api = AssistantApi(
          client: _VoiceClient((_) async => _response(invalid)),
        );
        addTearDown(api.close);
        await expectLater(
          api.transcribe(recording),
          throwsA(
            isA<AssistantApiException>().having(
              (error) => error.detail,
              'detail',
              '语音识别接口返回了无效响应',
            ),
          ),
        );
      }
    },
  );

  test(
    'transcribe handles malformed JSON without returning response body',
    () async {
      final api = AssistantApi(
        client: _VoiceClient(
          (_) async => http.StreamedResponse(
            http.ByteStream.fromBytes(
              utf8.encode('<html>upstream response</html>'),
            ),
            200,
          ),
        ),
      );
      addTearDown(api.close);

      await expectLater(
        api.transcribe(recording),
        throwsA(
          isA<AssistantApiException>().having(
            (error) => error.detail,
            'detail',
            '语音识别接口返回了无效响应',
          ),
        ),
      );
    },
  );

  test(
    'transcribe uses a safe HTTP fallback when detail is not a string',
    () async {
      final api = AssistantApi(
        client: _VoiceClient(
          (_) async => _response({
            'detail': ['invalid'],
          }, status: 502),
        ),
      );
      addTearDown(api.close);

      await expectLater(
        api.transcribe(recording),
        throwsA(
          isA<AssistantApiException>()
              .having((error) => error.detail, 'detail', '语音识别失败（HTTP 502）')
              .having((error) => error.statusCode, 'statusCode', 502),
        ),
      );
    },
  );

  test('transcribe translates transport errors into a safe detail', () async {
    final api = AssistantApi(
      client: _VoiceClient(
        (_) async => throw http.ClientException('internal error'),
      ),
    );
    addTearDown(api.close);

    await expectLater(
      api.transcribe(recording),
      throwsA(
        isA<AssistantApiException>().having(
          (error) => error.detail,
          'detail',
          '语音识别失败，请稍后重试',
        ),
      ),
    );
  });

  test('transcribe rejects empty audio before making a request', () async {
    var requests = 0;
    final api = AssistantApi(
      client: _VoiceClient((_) async {
        requests++;
        return _response(success);
      }),
    );
    addTearDown(api.close);

    await expectLater(
      api.transcribe(<int>[]),
      throwsA(
        isA<AssistantApiException>().having(
          (error) => error.detail,
          'detail',
          '录音为空，请重新录音',
        ),
      ),
    );
    expect(requests, 0);
  });
}
