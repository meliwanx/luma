import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:luma_client/api.dart';
import 'package:luma_client/markdown.dart';
import 'package:luma_client/theme.dart';
import 'package:luma_client/views/file_thumbnail.dart';
import 'package:luma_client/views/library.dart';
import 'package:webview_flutter/webview_flutter.dart';

const _document = <String, dynamic>{
  'id': 'file-1',
  'title': '项目计划',
  'filename': 'plan.md',
  'type': 'document',
  'media_type': 'text/markdown',
  'pinned': true,
  'session_id': 'session-1',
  'session_title': '计划讨论',
  'created_at': '2026-10-06T09:00:00Z',
};

class _LibraryApi extends AssistantApi {
  List<Map<String, dynamic>> items = [
    {..._document},
    {
      'id': 'file-2',
      'title': '销售表',
      'filename': 'sales.csv',
      'type': 'sheet',
      'pinned': false,
    },
  ];
  Map<String, dynamic> preview = {'kind': 'none'};
  final queries = <Map<String, dynamic>>[];
  final updates = <Map<String, dynamic>>[];
  final deleted = <String>[];
  final downloaded = <String>[];
  final previewed = <String>[];
  Future<Map<String, dynamic>> Function(String)? queryHandler;

  @override
  Future<Map<String, dynamic>> library({
    String type = 'all',
    String q = '',
    String sort = 'recent',
    int limit = 50,
    String? cursor,
  }) async {
    queries.add({'type': type, 'q': q, 'sort': sort, 'cursor': cursor});
    if (queryHandler != null) return queryHandler!(q);
    return {
      'items': items
          .where((item) => type == 'all' || item['type'] == type)
          .toList(),
      'counts': {'all': 2, 'document': 1, 'sheet': 1},
      'next_cursor': null,
    };
  }

  @override
  Future<Map<String, dynamic>> libraryPreview(String id) async {
    previewed.add(id);
    return preview;
  }

  @override
  Future<Map<String, dynamic>> updateLibrary(
    String id, {
    String? title,
    bool? pinned,
  }) async {
    final update = {
      'id': id,
      'title': ?title,
      'pinned': ?pinned,
    };
    updates.add(update);
    return update;
  }

  @override
  Future<void> deleteFile(String id) async => deleted.add(id);

  @override
  Future<Uint8List> downloadFileBytes(String fileId) async {
    downloaded.add(fileId);
    return base64Decode(
      'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jP1kAAAAASUVORK5CYII=',
    );
  }
}

Widget _shell(Widget child) => MaterialApp(
  theme: lumaTheme,
  home: Scaffold(body: child),
);

Future<void> _showLibrary(
  WidgetTester tester,
  _LibraryApi api, {
  ValueChanged<String>? onSelectSession,
  Future<void> Function(Map<String, dynamic>)? onFile,
}) async {
  await tester.pumpWidget(
    _shell(
      LibraryView(
        api: api,
        onFile: onFile ?? (_) async {},
        onSelectSession: onSelectSession ?? (_) {},
      ),
    ),
  );
  await tester.pumpAndSettle();
}

Future<void> _showPreview(
  WidgetTester tester,
  _LibraryApi api, {
  Map<String, dynamic> item = _document,
  ValueChanged<String>? onSelectSession,
  Future<void> Function(Map<String, dynamic>)? onFile,
}) async {
  await tester.pumpWidget(
    MaterialApp(
      theme: lumaTheme,
      home: Builder(
        builder: (context) => Scaffold(
          body: TextButton(
            onPressed: () => Navigator.of(context).push<void>(
              MaterialPageRoute(
                builder: (_) => LibraryPreviewView(
                  api: api,
                  item: item,
                  onFile: onFile ?? (_) async {},
                  onSelectSession: onSelectSession ?? (_) {},
                ),
              ),
            ),
            child: const Text('打开预览'),
          ),
        ),
      ),
    ),
  );
  await tester.tap(find.text('打开预览'));
  await tester.pumpAndSettle();
}

Future<void> _menu(WidgetTester tester, String label) async {
  await tester.tap(find.byTooltip('资源操作'));
  await tester.pumpAndSettle();
  await tester.tap(find.text(label));
  await tester.pumpAndSettle();
}

void main() {
  testWidgets('library groups pins and changes category with contract counts', (
    tester,
  ) async {
    final api = _LibraryApi();
    addTearDown(api.close);
    await _showLibrary(tester, api);
    expect(find.text('置顶'), findsOneWidget);
    expect(find.text('最近'), findsOneWidget);
    expect(find.text('来自 计划讨论'), findsOneWidget);
    expect(find.text('全部 2'), findsOneWidget);
    final grid = tester.widget<GridView>(find.byType(GridView).first);
    expect(
      (grid.gridDelegate as SliverGridDelegateWithFixedCrossAxisCount)
          .crossAxisCount,
      2,
    );

    await tester.tap(find.byKey(const ValueKey('library-type-sheet')));
    await tester.pumpAndSettle();
    expect(api.queries.last['type'], 'sheet');
    expect(find.text('销售表'), findsOneWidget);
    expect(find.text('项目计划'), findsNothing);
    expect(find.text('置顶'), findsNothing);
  });

  testWidgets('library search debounces and ignores a superseded response', (
    tester,
  ) async {
    final api = _LibraryApi();
    addTearDown(api.close);
    await _showLibrary(tester, api);
    final first = Completer<Map<String, dynamic>>();
    api.queryHandler = (q) async => q == 'old'
        ? first.future
        : {
            'items': [
              {'id': 'new', 'title': '新结果'},
            ],
            'counts': {},
            'next_cursor': null,
          };
    await tester.enterText(find.byType(TextField), 'old');
    await tester.pump(const Duration(milliseconds: 299));
    expect(api.queries.length, 1);
    await tester.pump(const Duration(milliseconds: 1));
    await tester.enterText(find.byType(TextField), 'new');
    await tester.pump(const Duration(milliseconds: 300));
    await tester.pump();
    first.complete({
      'items': [
        {'id': 'old', 'title': '旧结果'},
      ],
    });
    await tester.pumpAndSettle();
    expect(find.text('新结果'), findsOneWidget);
    expect(find.text('旧结果'), findsNothing);
  });

  testWidgets('library sort is sent to the server', (tester) async {
    final api = _LibraryApi();
    addTearDown(api.close);
    await _showLibrary(tester, api);
    await tester.tap(find.byTooltip('排序'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('标题'));
    await tester.pumpAndSettle();
    expect(api.queries.last['sort'], 'title');
  });

  testWidgets('image cards reuse authenticated thumbnails', (tester) async {
    final api = _LibraryApi()
      ..items = [
        {
          'id': 'image-1',
          'filename': 'photo.png',
          'title': '照片',
          'type': 'image',
        },
      ];
    addTearDown(api.close);
    await _showLibrary(tester, api);
    expect(find.byType(FileThumbnail), findsOneWidget);
    expect(api.downloaded, ['image-1']);
  });

  testWidgets('csv preview is a horizontally scrollable table', (tester) async {
    final api = _LibraryApi()
      ..preview = {
        'kind': 'csv',
        'columns': ['名称', '金额'],
        'rows': [
          ['旅行', 200],
          ['书籍'],
        ],
        'total_rows': 20,
        'truncated': true,
      };
    addTearDown(api.close);
    await _showPreview(tester, api);
    expect(api.previewed, ['file-1']);
    expect(find.byType(DataTable), findsOneWidget);
    expect(find.text('共 20 行'), findsOneWidget);
    expect(find.text('200'), findsOneWidget);
    expect(find.text('仅显示部分内容，下载可查看完整文件。'), findsOneWidget);
    expect(
      tester
          .widgetList<SingleChildScrollView>(find.byType(SingleChildScrollView))
          .any((view) => view.scrollDirection == Axis.horizontal),
      isTrue,
    );
  });

  testWidgets('markdown preview uses the safe native renderer', (tester) async {
    final api = _LibraryApi()
      ..preview = {'kind': 'markdown', 'text': '# 计划\n\n**交付内容**'};
    addTearDown(api.close);
    await _showPreview(tester, api);
    expect(find.byType(LumaMarkdown), findsOneWidget);
    expect(find.byType(SelectionArea), findsOneWidget);
    expect(find.byType(WebViewWidget), findsNothing);
  });

  testWidgets('text preview is selectable monospace', (tester) async {
    final api = _LibraryApi()
      ..preview = {
        'kind': 'text',
        'text': 'print("hello")',
        'language': 'python',
      };
    addTearDown(api.close);
    await _showPreview(tester, api);
    final text = tester.widget<SelectableText>(find.byType(SelectableText));
    expect(text.data, 'print("hello")');
    expect(text.style!.fontFamily, 'monospace');
  });

  testWidgets(
    'HTML is source only and downloading delegates authenticated file id',
    (tester) async {
      final api = _LibraryApi()
        ..preview = {
          'kind': 'html_source',
          'text': '<script>alert(1)</script><h1>标题</h1>',
        };
      addTearDown(api.close);
      Map<String, dynamic>? opened;
      await _showPreview(
        tester,
        api,
        item: {..._document, 'type': 'web', 'filename': 'index.html'},
        onFile: (file) async => opened = file,
      );
      expect(find.text('安全预览：仅展示 HTML 源码，不执行或渲染网页。'), findsOneWidget);
      expect(find.text('<script>alert(1)</script><h1>标题</h1>'), findsOneWidget);
      expect(find.byType(WebViewWidget), findsNothing);
      await _menu(tester, '下载或分享');
      expect(opened!['file_id'], 'file-1');
      expect(opened!['type'], 'web');
      expect(find.byType(WebViewWidget), findsNothing);
    },
  );

  testWidgets('image preview zooms authenticated bytes', (tester) async {
    final api = _LibraryApi()..preview = {'kind': 'image'};
    addTearDown(api.close);
    await _showPreview(tester, api);
    expect(find.byType(InteractiveViewer), findsOneWidget);
    expect(api.downloaded, ['file-1']);
    expect(find.byType(Image), findsOneWidget);
  });

  testWidgets('none preview explains download-only support', (tester) async {
    final api = _LibraryApi();
    addTearDown(api.close);
    await _showPreview(tester, api);
    expect(find.text('暂不支持预览'), findsOneWidget);
  });

  testWidgets(
    'preview pin, rename, and source session use their contract actions',
    (tester) async {
      final api = _LibraryApi();
      addTearDown(api.close);
      String? selected;
      await _showPreview(tester, api, onSelectSession: (id) => selected = id);
      await _menu(tester, '取消置顶');
      expect(api.updates.last, {'id': 'file-1', 'pinned': false});
      await _menu(tester, '重命名');
      await tester.enterText(find.byType(TextField), ' 新标题 ');
      await tester.tap(find.text('保存'));
      await tester.pumpAndSettle();
      expect(api.updates.last, {'id': 'file-1', 'title': '新标题'});
      expect(find.text('新标题'), findsOneWidget);
      await _menu(tester, '跳到对话');
      expect(selected, 'session-1');
      expect(find.byType(LibraryPreviewView), findsNothing);
    },
  );

  testWidgets('deleting a resource needs explicit confirmation', (
    tester,
  ) async {
    final api = _LibraryApi();
    addTearDown(api.close);
    await _showPreview(tester, api);
    await _menu(tester, '删除');
    expect(find.text('删除资源？'), findsOneWidget);
    expect(api.deleted, isEmpty);
    await tester.tap(find.text('取消'));
    await tester.pumpAndSettle();
    expect(api.deleted, isEmpty);
    await _menu(tester, '删除');
    await tester.tap(find.widgetWithText(TextButton, '删除'));
    await tester.pumpAndSettle();
    expect(api.deleted, ['file-1']);
    expect(find.byType(LibraryPreviewView), findsNothing);
  });
}
