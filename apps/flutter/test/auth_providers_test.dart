import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:luma_client/api.dart';
import 'package:luma_client/auth.dart';
import 'package:luma_client/theme.dart';

http.Response _response(Object value, [int status = 200, Map<String, String>? headers]) =>
    http.Response(
      jsonEncode(value),
      status,
      headers: {'content-type': 'application/json; charset=utf-8', ...?headers},
    );

Widget _app(Widget home) => MaterialApp(theme: buildLumaTheme(Brightness.light), home: home);

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUp(() => FlutterSecureStorage.setMockInitialValues({}));

  test('callback reader accepts same-origin ticket and rejects a foreign origin', () {
    final accepted = readSsoCallback(
      Uri.parse('https://app.example.test/sso-callback?ticket=ticket-token-ok-16'),
      appBase: 'https://app.example.test',
    );
    expect(accepted?.ticket, 'ticket-token-ok-16');
    expect(accepted?.state, isNull);
    final matched = readSsoCallback(
      Uri.parse('https://app.example.test/sso-callback?ticket=ticket-token-ok-16&state=abc'),
      appBase: 'https://app.example.test',
      expectedState: 'abc',
    );
    expect(matched?.state, 'abc');
    expect(
      readSsoCallback(
        Uri.parse('https://app.example.test/sso-callback?ticket=ticket-token-ok-16&state=other'),
        appBase: 'https://app.example.test',
        expectedState: 'abc',
      ),
      isNull,
    );
    expect(
      isAppSsoCallback(Uri.parse('https://other.example/sso-callback?ticket=ticket-token-ok-16'), 'https://app.example.test'),
      isFalse,
    );
    expect(AssistantApi.originOf('http://10.1.1.1', requireSecure: true), isNull);
    expect(AssistantApi.originOf('http://127.0.0.1:8000', requireSecure: true), 'http://127.0.0.1:8000');
  });

  test('sso start allows a different https host and loopback http', () {
    expect(ssoStartUrlAllowed('https://login.other.test/api/sso/federated-login'), isTrue);
    expect(ssoStartUrlAllowed('https://sso.example.test/start'), isTrue);
    expect(ssoStartUrlAllowed('http://127.0.0.1:8000/start'), isTrue);
    expect(ssoStartUrlAllowed('http://localhost/start'), isTrue);
    expect(ssoStartUrlAllowed('http://[::1]/start'), isTrue);
    expect(ssoStartUrlAllowed('http://evil.example/start'), isFalse);
    expect(ssoStartUrlAllowed('http://10.1.1.1/start'), isFalse);
    expect(ssoStartUrlAllowed('javascript:alert(1)'), isFalse);
    expect(ssoStartUrlAllowed('https://'), isFalse);
    expect(ssoStartUrlAllowed(''), isFalse);
  });

  test('sso password login uses fixed errors and does not echo the password', () async {
    const secret = 'do-not-echo-this-password';
    final api = AssistantApi(
      client: MockClient((request) async {
        expect(request.url.path, '/api/v1/auth/password/login');
        expect(jsonDecode(request.body)['account'], 'ada');
        return _response({'detail': secret}, 401);
      }),
    );
    addTearDown(api.close);
    await expectLater(
      api.ssoPasswordLogin('ada', secret),
      throwsA(
        isA<AssistantApiException>()
            .having((error) => error.message, 'message', '账号或密码错误')
            .having((error) => error.message.contains(secret), 'leaks password', isFalse),
      ),
    );
  });

  test('locked and unavailable password responses stay generic', () async {
    final locked = AssistantApi(
      client: MockClient((_) async => _response({'detail': 'raw'}, 429, {'retry-after': '900'})),
    );
    addTearDown(locked.close);
    await expectLater(
      locked.ssoPasswordLogin('ada', 'pw'),
      throwsA(isA<AssistantApiException>().having((error) => error.message, 'message', '尝试次数过多，请 15 分钟后再试')),
    );
    final down = AssistantApi(client: MockClient((_) async => _response({'detail': 'raw'}, 503)));
    addTearDown(down.close);
    await expectLater(
      down.ssoPasswordLogin('ada', 'pw'),
      throwsA(isA<AssistantApiException>().having((error) => error.message, 'message', '登录服务暂时不可用')),
    );
  });

  testWidgets('providers render redirect and account forms without the local form', (tester) async {
    var calls = 0;
    await tester.pumpWidget(_app(LoginPage(
      busy: false,
      showLocalPassword: false,
      showSsoRedirect: true,
      showSsoCredentials: true,
      ssoLabel: '单点登录',
      accountLabel: '账号',
      onLogin: (_, _) async {},
      onRegister: (_, _, _, _, _) async {},
      onSso: () async => calls++,
      onSsoPassword: (_, _) async => calls++,
    )));
    expect(find.byKey(const Key('login-submit')), findsNothing);
    expect(find.text('单点登录'), findsOneWidget);
    await tester.tap(find.byKey(const Key('sso-login')));
    await tester.pump();
    expect(calls, 1);
    await tester.enterText(find.byKey(const Key('sso-account')), 'ada');
    await tester.enterText(find.byKey(const Key('sso-password')), 'secret');
    await tester.tap(find.byKey(const Key('sso-credentials-submit')));
    await tester.pump();
    expect(calls, 2);
    expect(tester.widget<TextFormField>(find.byKey(const Key('sso-password'))).controller!.text, isEmpty);
  });

  testWidgets('auth gate falls back to local password when providers are absent', (tester) async {
    final repository = AuthRepository(
      api: AssistantApi(
        client: MockClient((request) async {
          if (request.url.path.endsWith('/providers')) {
            return _response({'authenticated': true});
          }
          if (request.url.path.endsWith('/config')) {
            return _response({'registration_open': false, 'requires_invite': false});
          }
          return _response({'detail': '用户名或密码错误'}, 401);
        }),
      ),
    );
    addTearDown(repository.close);
    await tester.pumpWidget(_app(AuthGate(repository: repository)));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('login-account')), findsOneWidget);
    expect(find.byKey(const Key('sso-login')), findsNothing);
  });

  testWidgets('auth gate renders the providers returned by the server', (tester) async {
    final repository = AuthRepository(
      api: AssistantApi(
        client: MockClient((request) async {
          expect(request.url.path, '/api/v1/auth/providers');
          return _response({
            'providers': [
              {'name': 'sso', 'label': '单点登录', 'kind': 'redirect'},
              {'name': 'sso', 'label': '账号', 'kind': 'credentials', 'account_label': '账号'},
            ],
            'registration_open': false,
            'requires_invite': false,
            'account_label': '账号',
            'sso_label': '单点登录',
          });
        }),
      ),
    );
    addTearDown(repository.close);
    await tester.pumpWidget(_app(AuthGate(repository: repository)));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('login-submit')), findsNothing);
    expect(find.byKey(const Key('sso-login')), findsOneWidget);
    expect(find.byKey(const Key('sso-account')), findsOneWidget);
  });
}
