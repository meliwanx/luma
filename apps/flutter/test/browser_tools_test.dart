import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:luma_client/api.dart';
import 'package:luma_client/browser_tools.dart';
import 'package:luma_client/views/browser_live.dart';
import 'package:luma_client/views/file_thumbnail.dart';
import 'package:luma_client/views/settings.dart';

void main() {
  test('live navigation only allows secure Tencent subdomains', () {
    expect(
      isBrowserLiveUrl('https://9000-id.ap-hongkong.tencentags.com/novnc/'),
      isTrue,
    );
    for (final url in [
      'https://tencentags.com/',
      'http://view.tencentags.com/',
      'https://view.tencentags.com.evil.example/',
      'https://evil.example/',
      'https://user:password@view.tencentags.com/',
      'https://view.tencentags.com:8443/',
      'javascript:alert(1)',
    ]) {
      expect(isBrowserLiveUrl(url), isFalse, reason: url);
    }
  });

  test('browser captions and image types are bounded', () {
    expect(browserProgressLabel('browser.open'), '正在打开网页…');
    expect(browserProgressLabel('browser.read'), '正在读取页面…');
    expect(browserProgressLabel('mcp.read'), isNull);
    expect(isImageFile('image/png'), isTrue);
    expect(isImageFile('image/svg+xml'), isFalse);
    expect(isImageFile('text/html'), isFalse);
    expect(
      liveBrowserStates([
        {'tool': 'browser.read'},
        {
          'data': {
            'kind': 'browser_live',
            'url': 'https://view.tencentags.com/',
          },
        },
      ]),
      hasLength(1),
    );
  });

  testWidgets(
    'live cards hide the temporary URL and expire the viewing button',
    (tester) async {
      await tester.pumpWidget(
        const MaterialApp(
          home: Scaffold(
            body: BrowserLiveCard(
              url:
                  'https://view.tencentags.com/?access_token=temporary-fixture',
              expiresIn: 1,
            ),
          ),
        ),
      );
      expect(find.text('观看实时画面'), findsOneWidget);
      expect(find.textContaining('temporary-fixture'), findsNothing);
      expect(
        tester.widget<TextButton>(find.byType(TextButton)).onPressed,
        isNotNull,
      );
      await tester.pump(const Duration(seconds: 1));
      expect(
        tester.widget<TextButton>(find.byType(TextButton)).onPressed,
        isNull,
      );
      expect(find.text('实时画面已过期，请重新打开'), findsOneWidget);
      await tester.pumpWidget(const SizedBox.shrink());
    },
  );

  testWidgets(
    'invalid live view has a close action and never creates a WebView',
    (tester) async {
      await tester.pumpWidget(
        MaterialApp(
          home: Builder(
            builder: (context) => TextButton(
              onPressed: () => Navigator.of(context).push(
                MaterialPageRoute<void>(
                  builder: (_) =>
                      const BrowserLiveView(url: 'https://evil.example/'),
                ),
              ),
              child: const Text('打开'),
            ),
          ),
        ),
      );
      await tester.tap(find.text('打开'));
      await tester.pumpAndSettle();
      expect(find.text('实时画面地址无效'), findsOneWidget);
      await tester.tap(find.byTooltip('关闭'));
      await tester.pumpAndSettle();
      expect(find.text('打开'), findsOneWidget);
    },
  );

  testWidgets(
    'thumbnail fetch uses the authenticated file endpoint and is reused',
    (tester) async {
      final png = base64Decode(
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l1sAAAAASUVORK5CYII=',
      );
      var requests = 0;
      final api = AssistantApi(
        token: 'fixture',
        client: MockClient((request) async {
          requests++;
          expect(request.url.path, '/api/v1/files/file-id/content');
          expect(request.headers['Authorization'], 'Bearer fixture');
          return http.Response.bytes(png, 200);
        }),
      );
      addTearDown(api.close);
      Widget tree() => MaterialApp(
        home: Scaffold(
          body: FileThumbnail(api: api, fileId: 'file-id', filename: '截图.png'),
        ),
      );
      await tester.pumpWidget(tree());
      await tester.pumpAndSettle();
      expect(find.byType(Image), findsOneWidget);
      expect(
        tester.widget<Image>(find.byType(Image)).image,
        isA<MemoryImage>(),
      );
      await tester.pumpWidget(tree());
      await tester.pumpAndSettle();
      expect(requests, 1);
      expect(tester.takeException(), isNull);
    },
  );

  test('file download propagates a fixed authentication failure', () async {
    var unauthorized = false;
    final api = AssistantApi(
      client: MockClient((_) async => http.Response('private response', 401)),
      onUnauthorized: () => unauthorized = true,
    );
    addTearDown(api.close);
    await expectLater(
      api.downloadFileBytes('missing'),
      throwsA(
        isA<AssistantApiException>().having(
          (error) => error.message,
          'message',
          '文件下载失败',
        ),
      ),
    );
    expect(unauthorized, isTrue);
  });

  testWidgets('browser permission uses its browser category label', (
    tester,
  ) async {
    final api = AssistantApi(
      client: MockClient(
        (_) async => http.Response(
          jsonEncode({
            'items': [
              {
                'key': 'browser.submit',
                'category': 'browser',
                'label': '提交网页',
                'mode': 'ask',
                'allow_always': true,
              },
            ],
          }),
          200,
          headers: {'content-type': 'application/json; charset=utf-8'},
        ),
      ),
    );
    addTearDown(api.close);
    await tester.pumpWidget(
      MaterialApp(home: PermissionsSandboxView(api: api, showSandbox: false)),
    );
    await tester.pumpAndSettle();
    expect(find.text('提交网页'), findsOneWidget);
    expect(find.text('浏览器权限 · 可始终允许'), findsOneWidget);
    expect(find.text('Luma 权限 · 可始终允许'), findsNothing);
  });
}
