import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:luma_client/brand.dart';
import 'package:luma_client/theme.dart';
import 'package:luma_client/views/side_menu.dart';

Future<void> _pumpMenu(
  WidgetTester tester, {
  Future<bool> Function(Map<String, dynamic>)? onDelete,
  Future<bool> Function(Map<String, dynamic>)? onRename,
  Set<String> generating = const {},
  Set<String> completed = const {},
}) async {
  tester.view.physicalSize = const Size(390, 844);
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.reset);
  await tester.pumpWidget(
    MaterialApp(
      theme: lumaTheme,
      home: Scaffold(
        body: SideMenu(
          tab: 0,
          sessions: const [
            {'id': 'main', 'kind': 'main', 'title': '主聊天'},
            {'id': 'side-1', 'kind': 'side', 'title': '旅行计划'},
          ],
          sessionId: 'side-1',
          mainSessionId: 'main',
          generatingSessionIds: generating,
          completedSessionIds: completed,
          onTabChanged: (_) {},
          onMainChat: () {},
          onSelectSession: (_) {},
          onDeleteSession: onDelete ?? (_) async => false,
          onRenameSession: onRename ?? (_) async => false,
          onSettings: () {},
          onSearch: () {},
          onNewSession: () {},
        ),
      ),
    ),
  );
  await tester.pump();
}

void main() {
  testWidgets('抽屉标题展示手写 logo 并保留设置入口', (tester) async {
    await _pumpMenu(tester);
    expect(find.byKey(const ValueKey('drawer-logo')), findsOneWidget);
    expect(find.byType(LumaLogo), findsOneWidget);
    expect(find.text('Luma'), findsNothing);
    expect(find.byTooltip('设置'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('旁聊操作按钮有44px触控区，删除只在选择操作后回调', (tester) async {
    Map<String, dynamic>? deleted;
    await _pumpMenu(
      tester,
      onDelete: (session) async {
        deleted = session;
        return true;
      },
    );
    final button = find.byKey(const ValueKey('menu-session-actions-side-1'));
    expect(tester.getSize(button).width, greaterThanOrEqualTo(44));
    expect(tester.getSize(button).height, greaterThanOrEqualTo(44));
    expect(find.byType(Dismissible), findsNothing);

    await tester.tap(button);
    await tester.pumpAndSettle();
    expect(find.text('重命名'), findsOneWidget);
    expect(deleted, isNull);
    await tester.tap(find.byKey(const ValueKey('session-action-delete')));
    await tester.pumpAndSettle();

    expect(deleted?['id'], 'side-1');
  });

  testWidgets('旁聊长按和操作按钮共用重命名入口', (tester) async {
    Map<String, dynamic>? renamed;
    await _pumpMenu(
      tester,
      onRename: (session) async {
        renamed = session;
        return true;
      },
    );

    await tester.longPress(find.text('旅行计划'));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('session-action-rename')));
    await tester.pumpAndSettle();

    expect(renamed?['id'], 'side-1');
  });

  testWidgets('菜单展示会话生成转圈和主聊天完成圆点', (tester) async {
    final semantics = tester.ensureSemantics();
    try {
      await _pumpMenu(tester, generating: {'side-1'}, completed: {'main'});
      expect(
        find.byKey(const ValueKey('session-generating-side-1')),
        findsOneWidget,
      );
      expect(
        find.byKey(const ValueKey('session-completed-main')),
        findsOneWidget,
      );
      expect(find.bySemanticsLabel(RegExp('回复生成中')), findsOneWidget);
    } finally {
      semantics.dispose();
    }
  });
}
