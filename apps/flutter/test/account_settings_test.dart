import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:luma_client/api.dart';
import 'package:luma_client/views/account.dart';
import 'package:luma_client/views/settings.dart';

http.Response _response(Object value, [int status = 200]) => http.Response(
  jsonEncode(value),
  status,
  headers: {'content-type': 'application/json; charset=utf-8'},
);
Widget _app(Widget home) => MaterialApp(home: home);
Future<void> _tap(WidgetTester tester, String key) async {
  await tester.tap(find.byKey(Key(key)));
  await tester.pumpAndSettle();
}

void main() {
  testWidgets('profile displays username and saves editable fields', (
    tester,
  ) async {
    Map<String, dynamic>? saved;
    final api = AssistantApi(
      client: MockClient((request) async {
        if (request.method == 'GET') {
          return _response({
            'username': 'profile-user',
            'email': 'user@example.com',
            'display_name': 'Initial Name',
          });
        }
        saved = jsonDecode(request.body) as Map<String, dynamic>;
        return http.Response('', 204);
      }),
    );
    addTearDown(api.close);
    await tester.pumpWidget(_app(AccountProfileView(api: api)));
    await tester.pumpAndSettle();
    expect(find.text('profile-user'), findsOneWidget);
    await tester.enterText(
      find.byKey(const Key('profile-display-name')),
      'Edited Name',
    );
    await tester.enterText(
      find.byKey(const Key('profile-email')),
      'edited@example.com',
    );
    await _tap(tester, 'profile-save');
    expect(saved, {
      'display_name': 'Edited Name',
      'email': 'edited@example.com',
    });
    expect(find.text('资料已保存'), findsOneWidget);
  });

  testWidgets('password change validates and displays current password error', (
    tester,
  ) async {
    var calls = 0;
    final api = AssistantApi(
      client: MockClient((request) async {
        calls++;
        expect(jsonDecode(request.body), {
          'current_password': 'old-fixture',
          'new_password': 'new-password',
        });
        return _response({'detail': '当前密码错误'}, 401);
      }),
    );
    addTearDown(api.close);
    await tester.pumpWidget(_app(AccountPasswordView(api: api)));
    await _tap(tester, 'password-save');
    expect(calls, 0);
    expect(find.text('请输入当前密码'), findsOneWidget);
    expect(find.text('密码需为 8–128 位'), findsOneWidget);
    await tester.enterText(
      find.byKey(const Key('password-current')),
      'old-fixture',
    );
    await tester.enterText(
      find.byKey(const Key('password-new')),
      'new-password',
    );
    await tester.enterText(
      find.byKey(const Key('password-confirm')),
      'different',
    );
    await _tap(tester, 'password-save');
    expect(calls, 0);
    expect(find.text('两次输入的密码不一致'), findsOneWidget);
    await tester.enterText(
      find.byKey(const Key('password-confirm')),
      'new-password',
    );
    await _tap(tester, 'password-save');
    expect(calls, 1);
    expect(find.text('当前密码错误'), findsOneWidget);
  });

  testWidgets('deletion requires a password and a separate confirmation', (
    tester,
  ) async {
    var calls = 0;
    var logout = 0;
    final api = AssistantApi(
      client: MockClient((request) async {
        calls++;
        expect(jsonDecode(request.body), {'password': 'delete-fixture'});
        return http.Response('', 204);
      }),
    );
    addTearDown(api.close);
    await tester.pumpWidget(
      _app(AccountDeleteView(api: api, onLogout: () => logout++)),
    );
    await _tap(tester, 'account-delete-submit');
    expect(calls, 0);
    expect(find.text('请输入密码'), findsOneWidget);
    await tester.enterText(
      find.byKey(const Key('account-delete-password')),
      'delete-fixture',
    );
    await _tap(tester, 'account-delete-submit');
    expect(calls, 0);
    expect(find.text('永久删除账户？'), findsOneWidget);
    await tester.tap(find.text('取消'));
    await tester.pumpAndSettle();
    expect(calls, 0);
    await tester.enterText(
      find.byKey(const Key('account-delete-password')),
      'delete-fixture',
    );
    await _tap(tester, 'account-delete-submit');
    await _tap(tester, 'account-delete-confirm');
    expect(calls, 1);
    expect(logout, 1);
  });

  for (final deletion in [false, true]) {
    testWidgets(
      'pending ${deletion ? "deletion" : "password change"} tolerates leaving the page',
      (tester) async {
        final completion = Completer<http.Response>();
        final api = AssistantApi(client: MockClient((_) => completion.future));
        addTearDown(api.close);
        await tester.pumpWidget(
          _app(
            deletion
                ? AccountDeleteView(api: api)
                : AccountPasswordView(api: api),
          ),
        );
        if (deletion) {
          await tester.enterText(
            find.byKey(const Key('account-delete-password')),
            'delete-fixture',
          );
          await _tap(tester, 'account-delete-submit');
          await tester.tap(find.byKey(const Key('account-delete-confirm')));
        } else {
          await tester.enterText(
            find.byKey(const Key('password-current')),
            'old-fixture',
          );
          await tester.enterText(
            find.byKey(const Key('password-new')),
            'new-password',
          );
          await tester.enterText(
            find.byKey(const Key('password-confirm')),
            'new-password',
          );
          await tester.tap(find.byKey(const Key('password-save')));
        }
        await tester.pump();
        await tester.pumpWidget(const SizedBox.shrink());
        completion.complete(http.Response('', 204));
        await tester.pumpAndSettle();
        expect(tester.takeException(), isNull);
      },
    );
  }

  testWidgets('numeric device timestamps render and self revocation exits', (
    tester,
  ) async {
    var logout = 0;
    final api = AssistantApi(
      client: MockClient((request) async {
        if (request.method == 'DELETE') return _response({'revoked': true});
        if (request.url.path.endsWith('/me')) {
          return _response({'detail': '登录已过期'}, 401);
        }
        return _response([
          {
            'id': 'device-fixture',
            'client': 'mobile',
            'last_used_at': 1700000000,
          },
        ]);
      }),
    );
    addTearDown(api.close);
    await tester.pumpWidget(
      _app(AccountSessionsView(api: api, onLogout: () => logout++)),
    );
    await tester.pumpAndSettle();
    expect(find.textContaining('最近使用：2023-11-'), findsOneWidget);
    await tester.tap(find.text('踢出'));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(FilledButton, '踢出'));
    await tester.pumpAndSettle();
    expect(logout, 1);
  });
}
