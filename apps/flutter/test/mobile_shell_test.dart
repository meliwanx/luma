import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:luma_client/brand.dart';
import 'package:luma_client/glass.dart';
import 'package:luma_client/theme.dart';
import 'package:luma_client/views/chat.dart';
import 'package:luma_client/views/missions.dart';
import 'package:luma_client/views/mobile_shell.dart';

Future<void> _pumpShell(
  WidgetTester tester, {
  double height = 844,
  FocusNode? focusNode,
  bool actualChat = false,
  bool sideChat = false,
  Set<String> completedSessionIds = const {},
}) async {
  tester.view.physicalSize = Size(390, height);
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.reset);
  await tester.pumpWidget(
    MaterialApp(
      theme: lumaDarkTheme,
      home: _ShellHarness(
        focusNode: focusNode,
        actualChat: actualChat,
        sideChat: sideChat,
        completedSessionIds: completedSessionIds,
      ),
    ),
  );
  await tester.pumpAndSettle();
}

void main() {
  test('无主会话 ID 时通过 kind 识别旁聊，空初始化会话使用主聊天布局', () {
    expect(
      MobileShell.isSideSession(
        sessionId: 'side',
        sessions: const [
          {'id': 'side', 'kind': 'side', 'title': '旅行计划'},
        ],
      ),
      isTrue,
    );
    expect(
      MobileShell.isSideSession(
        sessionId: 'main',
        sessions: const [
          {'id': 'main', 'kind': 'main'},
          {'id': 'side', 'kind': 'side'},
        ],
      ),
      isFalse,
    );
    expect(
      MobileShell.isSideSession(sessionId: 'ses_default', sessions: const []),
      isFalse,
    );
    expect(
      MobileShell.isSideSession(sessionId: '', sessions: const []),
      isFalse,
    );
  });

  testWidgets('主聊天仅显示可打开会话菜单的圆形玻璃 logo', (tester) async {
    await _pumpShell(tester);
    final logo = find.byKey(const ValueKey('mobile-header-logo'));
    final titleButton = find.byKey(const ValueKey('mobile-title-button'));
    expect(logo, findsOneWidget);
    expect(find.text('Luma'), findsNothing);
    expect(find.text('L'), findsNothing);
    expect(find.descendant(of: titleButton, matching: logo), findsOneWidget);
    expect(tester.getSize(titleButton), const Size(48, 48));
    expect(
      find.ancestor(
        of: titleButton,
        matching: find.byWidgetPredicate(
          (widget) =>
              widget is Semantics &&
              widget.properties.label == '打开会话菜单' &&
              widget.properties.button == true,
        ),
      ),
      findsOneWidget,
    );
    final glass = tester.widget<GlassSurface>(
      find.ancestor(of: logo, matching: find.byType(GlassSurface)).first,
    );
    expect(glass.shape, GlassShape.circle);
    expect(tester.widget<LumaLogo>(logo).width, 38);
    await tester.tap(logo);
    await tester.pumpAndSettle();
    expect(find.text('选项卡'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('主聊天完成点位于 logo 按钮右上角且可点击打开菜单', (tester) async {
    await _pumpShell(tester, completedSessionIds: const {'main'});
    final button = find.byKey(const ValueKey('mobile-title-button'));
    final completed = find.byKey(const ValueKey('mobile-main-completed'));
    expect(completed, findsOneWidget);
    expect(find.descendant(of: button, matching: completed), findsOneWidget);
    final buttonRect = tester.getRect(button);
    final dotRect = tester.getRect(completed);
    expect(dotRect.size, const Size(8, 8));
    expect(dotRect.center.dx, greaterThan(buttonRect.center.dx));
    expect(dotRect.center.dy, lessThan(buttonRect.center.dy));
    expect(buttonRect.contains(dotRect.center), isTrue);
    expect(find.text('Luma'), findsNothing);
    await tester.tap(completed);
    await tester.pumpAndSettle();
    expect(find.text('选项卡'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('旁聊保留标题旁主聊天完成点，导航报告实际高度与隐藏状态', (tester) async {
    tester.view.physicalSize = const Size(390, 844);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.reset);
    final heights = <double>[];
    await tester.pumpWidget(
      MaterialApp(
        theme: lumaTheme,
        home: MobileShell(
          tab: 0,
          title: '旅行计划',
          sessions: const [],
          sessionId: 'side-1',
          mainSessionId: 'main',
          completedSessionIds: const {'main'},
          onTabChanged: (_) {},
          onMainChat: () {},
          onNewSession: () {},
          onSettings: () {},
          onSearch: () {},
          onSelectSession: (_) {},
          onDeleteSession: (_) async => false,
          onRenameSession: (_) async => false,
          onTabBarHeightChanged: heights.add,
          child: const SizedBox.expand(),
        ),
      ),
    );
    await tester.pumpAndSettle();
    expect(find.byKey(const ValueKey('mobile-main-completed')), findsOneWidget);
    expect(find.text('旅行计划'), findsOneWidget);
    expect(find.byKey(const ValueKey('mobile-header-logo')), findsOneWidget);
    expect(
      find.descendant(
        of: find.byKey(const ValueKey('mobile-title-button')),
        matching: find.byKey(const ValueKey('mobile-main-completed')),
      ),
      findsNothing,
    );
    expect(
      heights.last,
      tester.getSize(find.byKey(const ValueKey('mobile-tab-bar'))).height,
    );
    tester.view.viewInsets = const FakeViewPadding(bottom: 300);
    await tester.pumpAndSettle();
    expect(heights.last, 0);
    tester.view.viewInsets = const FakeViewPadding();
    await tester.pumpAndSettle();
    expect(heights.last, 64);
  });

  testWidgets('菜单按钮推出主屏，点主屏遮罩关闭菜单', (tester) async {
    await _pumpShell(tester);
    expect(find.text('选项卡'), findsNothing);
    await tester.tap(find.byKey(const ValueKey('mobile-menu-button')));
    await tester.pumpAndSettle();
    expect(find.text('选项卡'), findsOneWidget);
    expect(find.text('主要聊天'), findsOneWidget);
    expect(find.text('你发起的旁聊会显示在这里。'), findsOneWidget);
    await tester.tap(find.byKey(const ValueKey('mobile-menu-scrim')));
    await tester.pumpAndSettle();
    expect(find.text('选项卡'), findsNothing);
  });

  testWidgets('菜单选择目标切换到 MissionsView 并关闭菜单', (tester) async {
    await _pumpShell(tester);
    await tester.tap(find.byKey(const ValueKey('mobile-menu-button')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('menu-tab-3')));
    await tester.pumpAndSettle();
    expect(find.byType(MissionsView), findsOneWidget);
    expect(find.text('选项卡'), findsNothing);
  });

  testWidgets('玻璃底部导航有五个图标，每个都能切换页面', (tester) async {
    await _pumpShell(tester);
    for (var index = 0; index < mobileTabLabels.length; index++) {
      final tab = find.byKey(ValueKey('mobile-tab-$index'));
      expect(tab, findsOneWidget);
      expect(tester.getSize(tab).width, greaterThanOrEqualTo(44));
      expect(tester.getSize(tab).height, greaterThanOrEqualTo(44));
      await tester.tap(tab);
      await tester.pumpAndSettle();
      final shell = tester.widget<MobileShell>(find.byType(MobileShell));
      expect(shell.tab, index);
      expect(shell.title, index == 0 ? 'Luma' : mobileTabLabels[index]);
      if (index == 0) {
        expect(
          find.byKey(const ValueKey('mobile-header-logo')),
          findsOneWidget,
        );
        expect(find.text('Luma'), findsNothing);
      } else {
        expect(find.byKey(const ValueKey('mobile-header-logo')), findsNothing);
        expect(
          find.descendant(
            of: find.byKey(const ValueKey('mobile-title-button')),
            matching: find.text(mobileTabLabels[index]),
          ),
          findsOneWidget,
        );
      }
    }
    expect(find.byType(GlassSurface), findsWidgets);
  });

  testWidgets('点聊天空白区域收起输入焦点', (tester) async {
    final focus = FocusNode();
    addTearDown(focus.dispose);
    await _pumpShell(tester, focusNode: focus);
    await tester.tap(find.byKey(const ValueKey('shell-input')));
    await tester.pump();
    expect(focus.hasFocus, isTrue);
    await tester.tapAt(const Offset(195, 420));
    await tester.pump();
    expect(focus.hasFocus, isFalse);
  });

  testWidgets('键盘可见时点击消息的手势区域也会移除输入焦点', (tester) async {
    final focus = FocusNode();
    addTearDown(focus.dispose);
    await _pumpShell(tester, focusNode: focus);
    await tester.tap(find.byKey(const ValueKey('shell-input')));
    tester.view.viewInsets = const FakeViewPadding(bottom: 300);
    await tester.pumpAndSettle();
    expect(focus.hasFocus, isTrue);
    await tester.tap(find.byKey(const ValueKey('shell-message')));
    await tester.pump();
    expect(focus.hasFocus, isFalse);
  });

  testWidgets('键盘可见时点击菜单先移除焦点并打开抽屉，主屏不可重新获得焦点', (tester) async {
    final focus = FocusNode();
    addTearDown(focus.dispose);
    await _pumpShell(tester, focusNode: focus);
    await tester.tap(find.byKey(const ValueKey('shell-input')));
    tester.view.viewInsets = const FakeViewPadding(bottom: 300);
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('mobile-menu-button')));
    await tester.pump();
    expect(focus.hasFocus, isFalse);
    await tester.pumpAndSettle();
    expect(find.text('选项卡'), findsOneWidget);
    focus.requestFocus();
    await tester.pump();
    expect(focus.hasFocus, isFalse);
  });

  testWidgets('主聊天 logo 和边缘滑动打开抽屉前都会收起键盘', (tester) async {
    final focus = FocusNode();
    addTearDown(focus.dispose);
    await _pumpShell(tester, focusNode: focus);
    for (final openFromTitle in [true, false]) {
      await tester.tap(find.byKey(const ValueKey('shell-input')));
      tester.view.viewInsets = const FakeViewPadding(bottom: 300);
      await tester.pumpAndSettle();
      if (openFromTitle) {
        await tester.tap(find.byKey(const ValueKey('mobile-header-logo')));
      } else {
        await tester.dragFrom(const Offset(4, 300), const Offset(170, 0));
      }
      await tester.pumpAndSettle();
      expect(focus.hasFocus, isFalse);
      expect(find.text('选项卡'), findsOneWidget);
      await tester.tap(find.byKey(const ValueKey('mobile-menu-scrim')));
      tester.view.viewInsets = const FakeViewPadding();
      await tester.pumpAndSettle();
    }
  });

  testWidgets('旁聊标题与返回按钮显示，点击返回主聊天后恢复菜单按钮', (tester) async {
    await _pumpShell(tester, sideChat: true);
    expect(find.text('旅行计划'), findsOneWidget);
    expect(find.byKey(const ValueKey('mobile-header-logo')), findsOneWidget);
    expect(
      find.descendant(
        of: find.byKey(const ValueKey('mobile-title-button')),
        matching: find.text('旅行计划'),
      ),
      findsOneWidget,
    );
    expect(
      find.descendant(
        of: find.byKey(const ValueKey('mobile-title-button')),
        matching: find.byKey(const ValueKey('mobile-header-logo')),
      ),
      findsNothing,
    );
    expect(find.byTooltip('返回主聊天'), findsOneWidget);
    expect(find.byKey(const ValueKey('mobile-menu-button')), findsNothing);
    expect(find.byIcon(Icons.notifications_none_rounded), findsNothing);
    await tester.tap(find.byKey(const ValueKey('mobile-main-chat-button')));
    await tester.pumpAndSettle();
    expect(
      tester.widget<MobileShell>(find.byType(MobileShell)).sessionId,
      'main',
    );
    expect(find.byKey(const ValueKey('mobile-menu-button')), findsOneWidget);
    expect(find.byTooltip('返回主聊天'), findsNothing);
    expect(find.text('Luma'), findsNothing);
    expect(find.text('旅行计划'), findsNothing);
  });

  testWidgets('旁聊仍可通过左边缘手势和标题打开抽屉', (tester) async {
    await _pumpShell(tester, sideChat: true);
    await tester.dragFrom(const Offset(4, 400), const Offset(170, 0));
    await tester.pumpAndSettle();
    expect(find.text('选项卡'), findsOneWidget);
    await tester.tap(find.byKey(const ValueKey('mobile-menu-scrim')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('mobile-title-button')));
    await tester.pumpAndSettle();
    expect(find.text('选项卡'), findsOneWidget);
  });

  testWidgets('抽屉打开期间键盘高度变化不挤压主聊天和输入栏', (tester) async {
    await _pumpShell(tester, actualChat: true);
    await tester.tap(find.byKey(const ValueKey('chat-input')));
    tester.view.viewInsets = const FakeViewPadding(bottom: 300);
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('mobile-menu-button')));
    await tester.pumpAndSettle();
    final chat = find.byType(ChatView);
    final composer = find.byKey(const ValueKey('chat-mobile-composer'));
    expect(tester.getSize(chat).height, 844);
    expect(MediaQuery.viewInsetsOf(tester.element(chat)).bottom, 0);
    expect(MobileKeyboardScope.ignoreInsetsOf(tester.element(chat)), isTrue);
    expect(
      tester.getBottomLeft(composer).dy,
      closeTo(764 * .88 + 844 * .06, 1),
    );
    final before = tester.getRect(composer);
    tester.view.viewInsets = const FakeViewPadding(bottom: 360);
    await tester.pumpAndSettle();
    expect(tester.getSize(chat).height, 844);
    expect(tester.getRect(composer), before);
    expect(tester.takeException(), isNull);
  });

  testWidgets('键盘出现时隐藏底部导航并缩短内容区域', (tester) async {
    await _pumpShell(tester, actualChat: true);
    final composer = find.byKey(const ValueKey('chat-mobile-composer'));
    final chat = tester.widget<ChatView>(find.byType(ChatView));
    expect(find.byKey(const ValueKey('mobile-tab-bar')), findsOneWidget);
    expect(tester.getBottomLeft(composer).dy, closeTo(764, 1));
    await tester.tap(find.byKey(const ValueKey('chat-input')));
    await tester.pump();
    chat.scrollController.jumpTo(
      chat.scrollController.position.maxScrollExtent,
    );
    await tester.pump();
    tester.view.viewInsets = const FakeViewPadding(bottom: 300);
    await tester.pumpAndSettle();
    expect(find.byKey(const ValueKey('mobile-tab-bar')), findsNothing);
    expect(tester.getSize(find.byType(ChatView)).height, closeTo(544, 1));
    expect(tester.getBottomLeft(composer).dy, closeTo(536, 1));
    expect(MediaQuery.viewInsetsOf(tester.element(composer)).bottom, 300);
    expect(
      chat.scrollController.offset,
      closeTo(chat.scrollController.position.maxScrollExtent, 1),
    );
    expect(tester.takeException(), isNull);
  });

  testWidgets('从左边缘右滑打开菜单，在主屏左滑关闭菜单', (tester) async {
    await _pumpShell(tester);
    await tester.dragFrom(const Offset(4, 400), const Offset(170, 0));
    await tester.pumpAndSettle();
    expect(find.text('选项卡'), findsOneWidget);
    await tester.drag(
      find.byKey(const ValueKey('mobile-menu-scrim')),
      const Offset(-110, 0),
    );
    await tester.pumpAndSettle();
    expect(find.text('选项卡'), findsNothing);
  });

  testWidgets('短屏菜单可滚动且搜索始终可见', (tester) async {
    await _pumpShell(tester, height: 400);
    await tester.tap(find.byKey(const ValueKey('mobile-menu-button')));
    await tester.pumpAndSettle();
    expect(tester.takeException(), isNull);
    final search = find.byKey(const ValueKey('menu-search'));
    expect(search, findsOneWidget);
    expect(tester.getBottomLeft(search).dy, lessThanOrEqualTo(400));
    await tester.ensureVisible(find.byKey(const ValueKey('menu-tab-3')));
    await tester.tap(find.byKey(const ValueKey('menu-tab-3')));
    await tester.pumpAndSettle();
    expect(find.byType(MissionsView), findsOneWidget);
    expect(tester.takeException(), isNull);
  });
}

class _ShellHarness extends StatefulWidget {
  const _ShellHarness({
    this.focusNode,
    this.actualChat = false,
    this.sideChat = false,
    this.completedSessionIds = const {},
  });

  final FocusNode? focusNode;
  final bool actualChat;
  final bool sideChat;
  final Set<String> completedSessionIds;

  @override
  State<_ShellHarness> createState() => _ShellHarnessState();
}

class _ShellHarnessState extends State<_ShellHarness> {
  var tab = 0;
  late String _sessionId = widget.sideChat ? 'side' : 'main';
  final _input = TextEditingController();
  final _scroll = ScrollController();
  double _tabBarHeight = 0;

  @override
  void dispose() {
    _input.dispose();
    _scroll.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => MobileShell(
    tab: tab,
    title: tab == 0
        ? (_sessionId == 'side' ? '旅行计划' : 'Luma')
        : mobileTabLabels[tab],
    sessions: widget.sideChat
        ? const [
            {'id': 'main', 'kind': 'main'},
            {'id': 'side', 'kind': 'side', 'title': '旅行计划'},
          ]
        : const [],
    sessionId: _sessionId,
    mainSessionId: 'main',
    completedSessionIds: widget.completedSessionIds,
    onTabChanged: (index) => setState(() => tab = index),
    onMainChat: () => setState(() {
      tab = 0;
      _sessionId = 'main';
    }),
    onNewSession: () {},
    onSettings: () {},
    onSearch: () {},
    onNotifications: () {},
    onSelectSession: (_) {},
    onDeleteSession: (_) async => false,
    onRenameSession: (_) async => false,
    onTabBarHeightChanged: (height) {
      if (_tabBarHeight != height) setState(() => _tabBarHeight = height);
    },
    child: tab == 0 && widget.actualChat
        ? ChatView(
            messages: List.generate(
              30,
              (index) => {
                'id': 'message-$index',
                'role': index.isEven ? 'user' : 'assistant',
                'content': '测试消息 $index',
              },
            ),
            tasks: const [],
            memories: const [],
            sending: false,
            input: _input,
            scrollController: _scroll,
            bottomOverlayHeight: _tabBarHeight,
            onRefresh: () async {},
            onSend: () {},
            onWidgetEvent: (id, action, value) async {},
          )
        : tab == 3
        ? MissionsView(
            tasks: const [],
            approvals: const [],
            runtimeJobs: const [],
            onDecideApproval: (id, approve) async {},
          )
        : Column(
            children: [
              const SizedBox(height: 140),
              if (tab == 0)
                TextField(
                  key: const ValueKey('shell-input'),
                  focusNode: widget.focusNode,
                ),
              Expanded(
                child: Center(
                  child: GestureDetector(
                    key: const ValueKey('shell-message'),
                    onTap: () {},
                    child: Text('页面 $tab'),
                  ),
                ),
              ),
            ],
          ),
  );
}
