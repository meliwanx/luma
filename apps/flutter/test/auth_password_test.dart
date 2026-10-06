import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:luma_client/api.dart';
import 'package:luma_client/auth.dart';
import 'package:luma_client/brand.dart';
import 'package:luma_client/glass.dart';
import 'package:luma_client/home.dart';
import 'package:luma_client/theme.dart';

const _password = 'test-only-password';
http.Response _response(Object value, [int status = 200]) => http.Response(
  jsonEncode(value),
  status,
  headers: {'content-type': 'application/json; charset=utf-8'},
);
http.Response _sessionResponse() => _response({
  'authenticated': true,
  'user': {'user_id': 'user-1'},
  'access_token': 'test-session-token',
});
Widget _app(Widget home, {Brightness brightness = Brightness.light}) =>
    MaterialApp(theme: buildLumaTheme(brightness), home: home);
Finder get _accountField => find.byKey(const Key('login-account'));
Finder get _passwordField => find.byKey(const Key('login-password'));
Finder get _loginButton => find.byKey(const Key('login-submit'));
Future<void> _fill(WidgetTester tester) async {
  await tester.enterText(_accountField, 'user-1');
  await tester.enterText(_passwordField, _password);
}

Future<void> _tap(WidgetTester tester, Finder target) async {
  await tester.ensureVisible(target);
  await tester.tap(target);
  await tester.pumpAndSettle();
}

LoginPage _login({
  bool registrationOpen = false,
  bool requiresInvite = false,
  Future<void> Function(String, String)? onLogin,
  Future<void> Function(String, String, String?, String?, String?)? onRegister,
}) => LoginPage(
  busy: false,
  registrationOpen: registrationOpen,
  requiresInvite: requiresInvite,
  onLogin: onLogin ?? (_, _) async {},
  onRegister: onRegister ?? (_, _, _, _, _) async {},
);

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUp(() => FlutterSecureStorage.setMockInitialValues({}));

  test(
    'login posts JSON username or email without a prior bearer token',
    () async {
      final api = AssistantApi(
        token: 'old-token',
        client: MockClient((request) async {
          expect(request.method, 'POST');
          expect(request.url.path, '/api/v1/auth/login');
          expect(request.headers['content-type'], 'application/json');
          expect(request.headers['X-Luma-Client'], isNotEmpty);
          expect(request.headers, isNot(contains('authorization')));
          expect(jsonDecode(request.body), {
            'login': 'user@example.com',
            'password': _password,
          });
          return _sessionResponse();
        }),
      );
      addTearDown(api.close);
      expect(
        await api.login('user@example.com', _password),
        'test-session-token',
      );
    },
  );

  for (final status in [401, 422, 429, 503]) {
    test('login displays the server detail for HTTP $status', () async {
      final api = AssistantApi(
        client: MockClient((_) async => _response({'detail': '接口提示'}, status)),
      );
      addTearDown(api.close);
      await expectLater(
        api.login('user-1', _password),
        throwsA(
          isA<AssistantApiException>()
              .having((error) => error.message, 'detail', '接口提示')
              .having((error) => error.statusCode, 'statusCode', status),
        ),
      );
    });
  }

  test('network errors never reflect passwords', () async {
    final api = AssistantApi(
      client: MockClient((_) async => throw Exception(_password)),
    );
    addTearDown(api.close);
    await expectLater(
      api.login('user-1', _password),
      throwsA(
        isA<AssistantApiException>().having(
          (error) => error.message,
          'message',
          '登录失败，请稍后重试',
        ),
      ),
    );
  });

  test('only the session token is stored after login', () async {
    final repository = AuthRepository(
      api: AssistantApi(client: MockClient((_) async => _sessionResponse())),
    );
    addTearDown(repository.close);
    await repository.login('user-1', _password);
    expect(await FlutterSecureStorage().readAll(), {
      'luma_access_token': 'test-session-token',
    });
  });

  testWidgets('empty credentials do not submit', (tester) async {
    var submissions = 0;
    await tester.pumpWidget(
      _app(_login(onLogin: (_, _) async => submissions++)),
    );
    await _tap(tester, _loginButton);
    expect(find.text('请输入用户名或邮箱'), findsOneWidget);
    expect(find.text('请输入密码'), findsOneWidget);
    expect(submissions, 0);
  });

  testWidgets('login stores token and opens home', (tester) async {
    final repository = AuthRepository(
      api: AssistantApi(
        client: MockClient(
          (request) async => request.url.path.endsWith('/config')
              ? _response({
                  'registration_open': false,
                  'requires_invite': false,
                })
              : _sessionResponse(),
        ),
      ),
    );
    addTearDown(repository.close);
    await tester.pumpWidget(_app(AuthGate(repository: repository)));
    await tester.pumpAndSettle();
    await _fill(tester);
    await _tap(tester, _loginButton);
    expect(find.byType(LumaHome), findsOneWidget);
    expect(await FlutterSecureStorage().readAll(), {
      'luma_access_token': 'test-session-token',
    });
    await tester.pumpWidget(const SizedBox.shrink());
    await tester.pump();
  });

  testWidgets('failed login displays detail and clears password', (
    tester,
  ) async {
    final repository = AuthRepository(
      api: AssistantApi(
        client: MockClient(
          (request) async => request.url.path.endsWith('/config')
              ? _response({
                  'registration_open': false,
                  'requires_invite': false,
                })
              : _response({'detail': '用户名或密码错误'}, 401),
        ),
      ),
    );
    addTearDown(repository.close);
    await tester.pumpWidget(_app(AuthGate(repository: repository)));
    await tester.pumpAndSettle();
    await _fill(tester);
    await _tap(tester, _loginButton);
    expect(find.text('用户名或密码错误'), findsOneWidget);
    expect(find.byType(LumaHome), findsNothing);
    expect(
      tester.widget<TextFormField>(_passwordField).controller!.text,
      isEmpty,
    );
    expect(await FlutterSecureStorage().readAll(), isEmpty);
  });

  testWidgets('busy submission prevents duplicates', (tester) async {
    final completion = Completer<void>();
    var submissions = 0;
    await tester.pumpWidget(
      _app(
        _login(
          onLogin: (_, _) {
            submissions++;
            return completion.future;
          },
        ),
      ),
    );
    await _fill(tester);
    await tester.tap(_loginButton);
    await tester.pump();
    expect(tester.widget<FilledButton>(_loginButton).onPressed, isNull);
    expect(find.byType(CircularProgressIndicator), findsOneWidget);
    expect(submissions, 1);
    completion.complete();
    await tester.pumpAndSettle();
  });

  for (final open in [false, true]) {
    for (final invite in [false, true]) {
      testWidgets(
        'config open=$open invite=$invite controls registration fields',
        (tester) async {
          final repository = AuthRepository(
            api: AssistantApi(
              client: MockClient(
                (_) async => _response({
                  'registration_open': open,
                  'requires_invite': invite,
                }),
              ),
            ),
          );
          addTearDown(repository.close);
          await tester.pumpWidget(_app(AuthGate(repository: repository)));
          await tester.pumpAndSettle();
          expect(
            find.byKey(const Key('auth-tabs')),
            open ? findsOneWidget : findsNothing,
          );
          expect(find.byKey(const Key('register-invite')), findsNothing);
          if (open) {
            await _tap(tester, find.text('注册'));
            expect(find.byKey(const Key('register-email')), findsOneWidget);
            expect(
              find.byKey(const Key('register-invite')),
              invite ? findsOneWidget : findsNothing,
            );
          }
        },
      );
    }
  }

  testWidgets(
    'registration validates password length and confirmation before submitting',
    (tester) async {
      var submissions = 0;
      await tester.pumpWidget(
        _app(
          _login(
            registrationOpen: true,
            onRegister: (_, _, _, _, _) async => submissions++,
          ),
        ),
      );
      await _tap(tester, find.text('注册'));
      await tester.enterText(_accountField, 'new-user');
      await tester.enterText(_passwordField, 'short');
      await tester.enterText(
        find.byKey(const Key('register-confirm-password')),
        'different',
      );
      await _tap(tester, find.byKey(const Key('register-submit')));
      expect(find.text('密码需为 8–128 位'), findsOneWidget);
      expect(find.text('两次输入的密码不一致'), findsOneWidget);
      expect(submissions, 0);
      await tester.enterText(_passwordField, 'x' * 129);
      await tester.enterText(
        find.byKey(const Key('register-confirm-password')),
        'x' * 129,
      );
      await _tap(tester, find.byKey(const Key('register-submit')));
      expect(find.text('密码需为 8–128 位'), findsOneWidget);
      expect(submissions, 0);
      await tester.enterText(_passwordField, _password);
      await tester.enterText(
        find.byKey(const Key('register-confirm-password')),
        _password,
      );
      await _tap(tester, find.byKey(const Key('register-submit')));
      expect(submissions, 1);
    },
  );

  testWidgets(
    'invite registration submits all fields only after invite is provided',
    (tester) async {
      List<Object?>? submitted;
      await tester.pumpWidget(
        _app(
          _login(
            registrationOpen: true,
            requiresInvite: true,
            onRegister: (user, password, email, name, invite) async {
              submitted = [user, password, email, name, invite];
            },
          ),
        ),
      );
      await _tap(tester, find.text('注册'));
      await tester.enterText(_accountField, 'new-user');
      await tester.enterText(_passwordField, _password);
      await tester.enterText(
        find.byKey(const Key('register-confirm-password')),
        _password,
      );
      await tester.enterText(
        find.byKey(const Key('register-email')),
        'new@example.com',
      );
      await tester.enterText(
        find.byKey(const Key('register-display-name')),
        'New User',
      );
      await _tap(tester, find.byKey(const Key('register-submit')));
      expect(find.text('请输入邀请码'), findsOneWidget);
      expect(submitted, isNull);
      await tester.enterText(
        find.byKey(const Key('register-invite')),
        'invite-fixture',
      );
      await _tap(tester, find.byKey(const Key('register-submit')));
      expect(submitted, [
        'new-user',
        _password,
        'new@example.com',
        'New User',
        'invite-fixture',
      ]);
    },
  );

  testWidgets('password visibility and phone glass styles are preserved', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(390, 640);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.reset);
    for (final brightness in [Brightness.light, Brightness.dark]) {
      await tester.pumpWidget(_app(_login(), brightness: brightness));
      await tester.pumpAndSettle();
      expect(find.byType(GlassSurface), findsOneWidget);
      expect(find.byType(LumaLogo), findsOneWidget);
      await _tap(tester, find.byTooltip('显示密码'));
      expect(
        tester
            .widget<TextField>(
              find.descendant(
                of: _passwordField,
                matching: find.byType(TextField),
              ),
            )
            .obscureText,
        isFalse,
      );
      await _tap(tester, find.byTooltip('隐藏密码'));
      expect(
        tester
            .widget<TextField>(
              find.descendant(
                of: _passwordField,
                matching: find.byType(TextField),
              ),
            )
            .obscureText,
        isTrue,
      );
      expect(tester.takeException(), isNull);
    }
  });
}
