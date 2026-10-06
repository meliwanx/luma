import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:luma_client/api.dart';
import 'package:luma_client/theme.dart';
import 'package:luma_client/views/attachment_picker.dart';

class _AttachmentApi extends AssistantApi {
  _AttachmentApi(this.handler);

  final Future<List<Map<String, dynamic>>> Function() handler;
  int requests = 0;

  @override
  Future<List<Map<String, dynamic>>> listFiles({
    String? sessionId,
    int limit = 100,
  }) {
    requests++;
    return handler();
  }
}

Future<void> _openPicker(
  WidgetTester tester,
  AssistantApi api, {
  ValueChanged<Map<String, dynamic>?>? onSelected,
}) async {
  await tester.pumpWidget(
    MaterialApp(
      theme: lumaTheme,
      home: Builder(
        builder: (context) => Scaffold(
          body: Center(
            child: TextButton(
              onPressed: () async {
                final selected =
                    await showModalBottomSheet<Map<String, dynamic>>(
                      context: context,
                      isScrollControlled: true,
                      builder: (_) => AttachmentPicker(api: api),
                    );
                onSelected?.call(selected);
              },
              child: const Text('选择附件'),
            ),
          ),
        ),
      ),
    ),
  );
  await tester.tap(find.text('选择附件'));
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 400));
}

void main() {
  testWidgets('attachment picker loads and returns an authenticated file id', (
    tester,
  ) async {
    final api = _AttachmentApi(
      () async => [
        {
          'id': 'file-1',
          'filename': '旅行计划.pdf',
          'size_bytes': 2048,
          'url': 'https://untrusted.example/file',
        },
      ],
    );
    addTearDown(api.close);
    Map<String, dynamic>? selected;
    await _openPicker(tester, api, onSelected: (file) => selected = file);
    await tester.pump();

    expect(find.text('旅行计划.pdf'), findsOneWidget);
    expect(find.text('2.0 KB'), findsOneWidget);
    expect(find.text('https://untrusted.example/file'), findsNothing);
    final fileRow = find.byKey(const ValueKey('attachment-file-file-1'));
    expect(tester.getSize(fileRow).height, greaterThanOrEqualTo(44));
    await tester.tap(fileRow);
    await tester.pumpAndSettle();

    expect(selected?['id'], 'file-1');
    expect(selected?['filename'], '旅行计划.pdf');
    expect(find.byType(AttachmentPicker), findsNothing);
    expect(api.requests, 1);
  });

  testWidgets('empty resources explain that files can be uploaded from Web', (
    tester,
  ) async {
    final api = _AttachmentApi(() async => []);
    addTearDown(api.close);
    await _openPicker(tester, api);
    await tester.pump();

    expect(find.text('资源库还没有文件'), findsOneWidget);
    expect(find.text('可以先在 Web 端上传，再从这里选择。'), findsOneWidget);
    expect(find.byType(ListTile), findsNothing);
  });

  testWidgets('file-list failures can be retried without exposing exceptions', (
    tester,
  ) async {
    var attempts = 0;
    final api = _AttachmentApi(() async {
      attempts++;
      if (attempts == 1) throw StateError('private server detail');
      return [
        {'id': 'file-2', 'filename': '备忘录.txt'},
      ];
    });
    addTearDown(api.close);
    await _openPicker(tester, api);
    await tester.pump();

    expect(find.text('文件列表加载失败，请重试'), findsOneWidget);
    expect(find.textContaining('private server detail'), findsNothing);
    await tester.tap(find.text('重试'));
    await tester.pump();
    await tester.pump();

    expect(find.text('备忘录.txt'), findsOneWidget);
    expect(attempts, 2);
  });

  testWidgets('files without an id cannot be selected even with a URL', (
    tester,
  ) async {
    final api = _AttachmentApi(
      () async => [
        {'filename': '无标识文件.txt', 'url': 'https://untrusted.example/file'},
        {'id': '', 'filename': '空标识文件.txt'},
        {'id': 123, 'filename': '非法标识文件.txt'},
      ],
    );
    addTearDown(api.close);
    await _openPicker(tester, api);
    await tester.pump();

    expect(find.text('资源库还没有文件'), findsOneWidget);
    expect(find.byType(ListTile), findsNothing);
  });

  testWidgets('closing the picker ignores an in-flight file response', (
    tester,
  ) async {
    final response = Completer<List<Map<String, dynamic>>>();
    final api = _AttachmentApi(() => response.future);
    addTearDown(api.close);
    await _openPicker(tester, api);

    expect(find.byType(CircularProgressIndicator), findsOneWidget);
    await tester.tap(find.byTooltip('关闭附件选择'));
    await tester.pumpAndSettle();
    response.complete([
      {'id': 'late-file', 'filename': '迟到的文件.txt'},
    ]);
    await tester.pump();

    expect(find.byType(AttachmentPicker), findsNothing);
    expect(tester.takeException(), isNull);
  });
}
