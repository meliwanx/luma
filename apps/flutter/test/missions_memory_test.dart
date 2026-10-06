import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:luma_client/theme.dart';
import 'package:luma_client/views/memory.dart';
import 'package:luma_client/views/missions.dart';

Widget _testShell(Widget child) => MaterialApp(
  theme: lumaTheme,
  home: Scaffold(body: child),
);

void main() {
  testWidgets('审批卡片在允许时显示始终允许', (tester) async {
    var remembered = false;
    await tester.pumpWidget(
      _testShell(
        MissionsView(
          tasks: const [],
          approvals: const [
            {'id': 'approval-1', 'allow_always': true, 'description': '更新订单'},
          ],
          runtimeJobs: const [],
          onDecideApproval: _ignoreApproval,
          onDecideApprovalWithRemember: (id, approve, remember) async {
            remembered = approve && remember;
          },
        ),
      ),
    );
    expect(find.text('始终允许'), findsOneWidget);
    await tester.tap(find.text('始终允许'));
    await tester.pump();
    expect(remembered, isTrue);
  });

  testWidgets('例程显示支持的频率格式', (tester) async {
    await tester.pumpWidget(
      _testShell(
        MissionsView(
          tasks: const [],
          approvals: const [],
          runtimeJobs: const [],
          routines: const [
            {
              'id': 'routine-1',
              'title': '早间简报',
              'prompt': '整理今天的安排',
              'schedule': 'daily 09:00',
              'enabled': true,
            },
            {
              'id': 'routine-2',
              'title': '周报',
              'prompt': '总结本周进展',
              'schedule': 'weekly 1 18:30',
              'enabled': false,
            },
            {
              'id': 'routine-3',
              'title': '轮询',
              'prompt': '检查待处理事项',
              'schedule': 'every 30m',
              'enabled': true,
            },
          ],
          onDecideApproval: _ignoreApproval,
        ),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('例程'), findsOneWidget);
    expect(find.textContaining('每天 09:00'), findsOneWidget);
    expect(find.text('每周一 18:30'), findsOneWidget);
    expect(find.text('每 30 分钟'), findsOneWidget);
  });

  testWidgets('推断记忆显示确认和置顶操作', (tester) async {
    await tester.pumpWidget(
      _testShell(
        MemoryView(
          memories: const [
            {
              'id': 'mem-1',
              'content': '用户可能喜欢简洁的日报',
              'category': 'inferred',
              'pinned': false,
              'metadata': <String, dynamic>{},
            },
          ],
        ),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('推断'), findsAtLeastNWidgets(1));
    expect(find.text('确认推断'), findsOneWidget);
    expect(find.text('置顶'), findsOneWidget);
    expect(find.byType(Switch), findsOneWidget);
  });
}

Future<void> _ignoreApproval(String ignoredId, bool approved) async {}
