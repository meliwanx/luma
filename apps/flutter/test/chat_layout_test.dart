import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:luma_client/theme.dart';
import 'package:luma_client/views/chat.dart';

List<Map<String, dynamic>> _history() => List.generate(
  32,
  (index) => <String, dynamic>{
    'id': 'message-$index',
    'role': index.isEven ? 'user' : 'assistant',
    'content': '消息 $index\n足够长的历史消息，用于验证键盘和浮动输入框之间的滚动布局。',
  },
);

class _Scenario {
  final input = TextEditingController();
  final scroll = ScrollController();
  final messages = _history();
  double keyboard = 0;
  double tabHeight = 96;
  List<Map<String, dynamic>> attachments = const [];
  late StateSetter rebuild;

  void dispose() {
    input.dispose();
    scroll.dispose();
  }
}

Future<void> _mount(WidgetTester tester, _Scenario scenario) async {
  tester.view.physicalSize = const Size(390, 844);
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  addTearDown(scenario.dispose);
  await tester.pumpWidget(
    MaterialApp(
      theme: lumaTheme,
      home: Scaffold(
        resizeToAvoidBottomInset: false,
        body: StatefulBuilder(
          builder: (context, setState) {
            scenario.rebuild = setState;
            return MediaQuery(
              data: MediaQuery.of(context).copyWith(
                viewInsets: EdgeInsets.only(bottom: scenario.keyboard),
                padding: EdgeInsets.only(
                  bottom: scenario.keyboard > 0 ? 0 : 24,
                ),
              ),
              child: Align(
                alignment: Alignment.topCenter,
                child: SizedBox(
                  height: 844 - scenario.keyboard,
                  child: ChatView(
                    messages: scenario.messages,
                    tasks: const [],
                    memories: const [],
                    sending: false,
                    input: scenario.input,
                    scrollController: scenario.scroll,
                    bottomOverlayHeight: scenario.keyboard > 0
                        ? 0
                        : scenario.tabHeight,
                    attachments: scenario.attachments,
                    onRefresh: () async {},
                    onSend: () {
                      setState(() {
                        scenario.keyboard = 0;
                        scenario.messages.add({
                          'id': 'sent-message',
                          'role': 'user',
                          'content': scenario.input.text,
                        });
                        scenario.input.clear();
                      });
                    },
                    onWidgetEvent: (id, action, value) async {},
                  ),
                ),
              ),
            );
          },
        ),
      ),
    ),
  );
  await tester.pumpAndSettle();
}

Rect _composerRect(WidgetTester tester) =>
    tester.getRect(find.byKey(const ValueKey('chat-mobile-composer')));

void _latestAboveComposer(WidgetTester tester, _Scenario scenario) {
  final latest = tester.getRect(
    find.text('${scenario.messages.last['content']}'),
  );
  expect(latest.bottom, lessThan(_composerRect(tester).top));
  expect(scenario.scroll.position.extentAfter, closeTo(0, 1));
}

void main() {
  testWidgets(
    'keyboard appears and disappears with latest message unobscured',
    (tester) async {
      final scenario = _Scenario();
      await _mount(tester, scenario);
      _latestAboveComposer(tester, scenario);
      expect(_composerRect(tester).bottom, closeTo(844 - 24 - 96 - 16, 1));

      scenario.rebuild(() => scenario.keyboard = 300);
      await tester.pumpAndSettle();
      _latestAboveComposer(tester, scenario);
      expect(_composerRect(tester).bottom, closeTo(844 - 300 - 8, 1));

      scenario.rebuild(() => scenario.keyboard = 0);
      await tester.pumpAndSettle();
      _latestAboveComposer(tester, scenario);
      expect(_composerRect(tester).bottom, closeTo(844 - 24 - 96 - 16, 1));
    },
  );

  testWidgets('measured composer and attachment heights reserve actual space', (
    tester,
  ) async {
    final scenario = _Scenario();
    await _mount(tester, scenario);
    scenario.rebuild(() {
      scenario.attachments = const [
        {'id': 'file-1', 'filename': '报告.pdf'},
      ];
      scenario.tabHeight = 112;
    });
    await tester.pumpAndSettle();
    final draft = tester.getRect(
      find.byKey(const ValueKey('chat-attachment-drafts')),
    );
    final list = tester.widget<ListView>(
      find.byKey(const ValueKey('chat-message-list')),
    );
    expect(
      (list.padding! as EdgeInsets).bottom,
      closeTo(844 - draft.top + 12, 1),
    );
    final latest = tester.getRect(
      find.text('${scenario.messages.last['content']}'),
    );
    expect(latest.bottom, lessThan(draft.top));
  });

  testWidgets('send forces latest into view while keyboard closes', (
    tester,
  ) async {
    final scenario = _Scenario();
    scenario.keyboard = 300;
    await _mount(tester, scenario);
    scenario.scroll.jumpTo(0);
    await tester.pump();
    expect(scenario.scroll.position.extentAfter, greaterThan(120));
    scenario.input.text = '刚发送的消息';
    await tester.pump();
    await tester.tap(find.byTooltip('发送'));
    await tester.pumpAndSettle();
    expect(scenario.messages.last['id'], 'sent-message');
    _latestAboveComposer(tester, scenario);
  });

  testWidgets(
    'keyboard changes preserve position while reading older messages',
    (tester) async {
      final scenario = _Scenario();
      await _mount(tester, scenario);
      scenario.scroll.jumpTo(0);
      await tester.pump();
      scenario.rebuild(() => scenario.keyboard = 300);
      await tester.pumpAndSettle();
      expect(scenario.scroll.offset, closeTo(0, 1));
      scenario.rebuild(() => scenario.keyboard = 0);
      await tester.pumpAndSettle();
      expect(scenario.scroll.offset, closeTo(0, 1));
    },
  );

  testWidgets(
    'streamed content growth keeps the latest message above composer',
    (tester) async {
      final scenario = _Scenario();
      await _mount(tester, scenario);
      scenario.rebuild(() {
        scenario.messages.last['content'] =
            '流式回复\n${List.filled(12, '继续生成的内容').join('\n')}';
        scenario.messages.last['status'] = 'streaming';
      });
      await tester.pumpAndSettle();
      _latestAboveComposer(tester, scenario);
    },
  );
}
