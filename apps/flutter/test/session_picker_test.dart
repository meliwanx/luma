import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:luma_client/theme.dart';
import 'package:luma_client/views/session_picker.dart';

Future<void> _openPicker(
  WidgetTester tester, {
  Future<bool> Function(Map<String, dynamic>)? onDelete,
  Future<bool> Function(Map<String, dynamic>)? onRename,
  Future<void> Function(String)? onSelect,
  Future<Map<String, dynamic>?> Function()? onCreate,
}) async {
  await tester.pumpWidget(
    MaterialApp(
      theme: lumaTheme,
      home: Scaffold(
        body: Builder(
          builder: (context) => TextButton(
            onPressed: () => showModalBottomSheet<void>(
              context: context,
              isScrollControlled: true,
              builder: (_) => SessionPicker(
                sessions: const [
                  {'id': 'main', 'kind': 'main', 'title': '主聊天'},
                  {
                    'id': 'side-1',
                    'kind': 'side',
                    'title': '旅行计划',
                    'last_message_preview': '上海出发',
                  },
                ],
                selectedSessionId: 'side-1',
                mainSelected: false,
                onMainChat: () async {},
                onSelectSession: onSelect ?? (_) async {},
                onCreateSession: onCreate ?? () async => null,
                onDeleteSession: onDelete ?? (_) async => false,
                onRenameSession: onRename,
              ),
            ),
            child: const Text('打开会话'),
          ),
        ),
      ),
    ),
  );
  await tester.tap(find.text('打开会话'));
  await tester.pumpAndSettle();
}

void main() {
  testWidgets('选择桌面旁聊关闭弹层并回调会话 ID', (tester) async {
    String? selected;
    await _openPicker(tester, onSelect: (id) async => selected = id);
    expect(find.text('上海出发'), findsOneWidget);
    await tester.tap(find.text('旅行计划'));
    await tester.pumpAndSettle();
    expect(selected, 'side-1');
    expect(find.byType(SessionPicker), findsNothing);
  });

  testWidgets('删除确认取消保留旁聊，确认成功更新弹层快照', (tester) async {
    var confirmed = false;
    await _openPicker(tester, onDelete: (_) async => confirmed);
    await tester.longPress(find.text('旅行计划'));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('session-action-delete')));
    await tester.pumpAndSettle();
    expect(find.text('旅行计划'), findsOneWidget);
    confirmed = true;
    await tester.longPress(find.text('旅行计划'));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('session-action-delete')));
    await tester.pumpAndSettle();
    expect(find.text('旅行计划'), findsNothing);
    expect(find.text('发起旁聊'), findsOneWidget);
    expect(find.byType(SessionPicker), findsOneWidget);
  });

  testWidgets('创建失败保持弹层，创建成功关闭并选择新旁聊', (tester) async {
    var createSucceeded = false;
    String? selected;
    await _openPicker(
      tester,
      onCreate: () async => createSucceeded ? {'id': 'new-side'} : null,
      onSelect: (id) async => selected = id,
    );
    await tester.tap(find.byTooltip('新建旁聊'));
    await tester.pumpAndSettle();
    expect(find.byType(SessionPicker), findsOneWidget);
    expect(selected, isNull);
    createSucceeded = true;
    await tester.tap(find.byTooltip('新建旁聊'));
    await tester.pumpAndSettle();
    expect(find.byType(SessionPicker), findsNothing);
    expect(selected, 'new-side');
  });

  testWidgets('操作按钮重命名成功立即更新弹层中的标题', (tester) async {
    await _openPicker(
      tester,
      onRename: (session) async {
        session['title'] = '旅行安排';
        return true;
      },
    );

    await tester.tap(
      find.byKey(const ValueKey('picker-session-actions-side-1')),
    );
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('session-action-rename')));
    await tester.pumpAndSettle();

    expect(find.text('旅行计划'), findsNothing);
    expect(find.text('旅行安排'), findsOneWidget);
    expect(find.byType(SessionPicker), findsOneWidget);
  });
}
