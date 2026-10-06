import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:luma_client/api.dart';
import 'package:luma_client/auth.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

http.Response _response(Object value, [int status = 200]) => http.Response(
  jsonEncode(value),
  status,
  headers: {'content-type': 'application/json; charset=utf-8'},
);

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  test(
    'registration posts optional fields and invite without authorization',
    () async {
      final api = AssistantApi(
        token: 'old-session',
        client: MockClient((request) async {
          expect(request.method, 'POST');
          expect(request.url.path, '/api/v1/auth/register');
          expect(request.headers, isNot(contains('authorization')));
          expect(jsonDecode(request.body), {
            'username': 'new-user',
            'password': 'fixture-password',
            'email': 'new@example.com',
            'display_name': 'New User',
            'invite_code': 'invite-fixture',
          });
          return _response({
            'authenticated': true,
            'access_token': 'session-fixture',
          });
        }),
      );
      addTearDown(api.close);
      expect(
        await api.register(
          username: 'new-user',
          password: 'fixture-password',
          email: 'new@example.com',
          displayName: 'New User',
          inviteCode: 'invite-fixture',
        ),
        'session-fixture',
      );
    },
  );

  test(
    'registration omits empty optional values and persists only the session',
    () async {
      FlutterSecureStorage.setMockInitialValues({});
      final repository = AuthRepository(
        api: AssistantApi(
          client: MockClient((request) async {
            expect(jsonDecode(request.body), {
              'username': 'new-user',
              'password': 'fixture-password',
            });
            return _response({
              'authenticated': true,
              'access_token': 'session-fixture',
            });
          }),
        ),
      );
      addTearDown(repository.close);
      await repository.register(
        username: 'new-user',
        password: 'fixture-password',
        email: '',
        displayName: ' ',
      );
      expect(await FlutterSecureStorage().readAll(), {
        'luma_access_token': 'session-fixture',
      });
    },
  );

  test(
    'configuration is public and preserves registration and invite flags',
    () async {
      final api = AssistantApi(
        token: 'old-session',
        client: MockClient((request) async {
          expect(request.method, 'GET');
          expect(request.url.path, '/api/v1/auth/config');
          expect(request.headers, isNot(contains('authorization')));
          return _response({
            'registration_open': true,
            'requires_invite': true,
          });
        }),
      );
      addTearDown(api.close);
      expect(await api.authConfig(), {
        'registration_open': true,
        'requires_invite': true,
      });
    },
  );

  test('password change uses the current session and JSON fields', () async {
    final api = AssistantApi(
      token: 'session-fixture',
      client: MockClient((request) async {
        expect(request.method, 'POST');
        expect(request.url.path, '/api/v1/account/password');
        expect(request.headers['authorization'], 'Bearer session-fixture');
        expect(jsonDecode(request.body), {
          'current_password': 'old-fixture',
          'new_password': 'new-fixture',
        });
        return http.Response('', 204);
      }),
    );
    addTearDown(api.close);
    await api.changePassword('old-fixture', 'new-fixture');
  });

  test('incorrect current password displays detail without clearing a valid session', () async {
    var unauthorized = 0;
    final api = AssistantApi(
      token: 'session-fixture',
      onUnauthorized: () => unauthorized++,
      client: MockClient((_) async => _response({'detail': '当前密码错误'}, 401)),
    );
    addTearDown(api.close);
    await expectLater(
      api.changePassword('old-fixture', 'new-fixture'),
      throwsA(
        isA<AssistantApiException>().having(
          (e) => e.message,
          'detail',
          '当前密码错误',
        ),
      ),
    );
    expect(unauthorized, 0);
    expect(api.token, 'session-fixture');
  });

  test('profile edits and deletion follow account JSON contracts', () async {
    var requests = 0;
    final api = AssistantApi(
      token: 'session-fixture',
      client: MockClient((request) async {
        expect(request.headers['authorization'], 'Bearer session-fixture');
        if (requests++ == 0) {
          expect(request.method, 'PATCH');
          expect(request.url.path, '/api/v1/account/profile');
          expect(jsonDecode(request.body), {
            'display_name': 'Edited Name',
            'email': '',
          });
        } else {
          expect(request.method, 'DELETE');
          expect(request.url.path, '/api/v1/account');
          expect(jsonDecode(request.body), {'password': 'fixture-password'});
        }
        return http.Response('', 204);
      }),
    );
    addTearDown(api.close);
    await api.updateProfile(displayName: 'Edited Name', email: '');
    await api.deleteAccount('fixture-password');
    expect(requests, 2);
  });

  test('registration errors show backend detail', () async {
    final api = AssistantApi(
      client: MockClient((_) async => _response({'detail': '邀请码无效'}, 403)),
    );
    addTearDown(api.close);
    await expectLater(
      api.register(username: 'new-user', password: 'fixture-password'),
      throwsA(
        isA<AssistantApiException>().having(
          (e) => e.message,
          'detail',
          '邀请码无效',
        ),
      ),
    );
  });
}
