import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:luma_client/glass.dart';
import 'package:luma_client/theme.dart';
import 'package:luma_client/views/chat.dart';

void _mobileViewport(WidgetTester tester) {
  tester.view.physicalSize = const Size(390, 844);
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
}

Widget _app({
  required TextEditingController input,
  required ScrollController scroll,
  List<Map<String, dynamic>> messages = const [],
  bool sending = false,
  VoidCallback? onSend,
  VoidCallback? onStop,
  VoidCallback? onAttach,
  List<Map<String, dynamic>> attachments = const [],
  ValueChanged<String>? onRemoveAttachment,
  Future<void> Function(Map<String, dynamic> file)? onFile,
}) => MaterialApp(
  theme: lumaTheme,
  home: Scaffold(
    body: ChatView(
      messages: messages,
      tasks: const [],
      memories: const [],
      sending: sending,
      input: input,
      scrollController: scroll,
      onRefresh: () async {},
      onSend: onSend ?? () {},
      onStop: onStop,
      onAttach: onAttach,
      attachments: attachments,
      onRemoveAttachment: onRemoveAttachment,
      onFile: onFile,
      onWidgetEvent: (id, action, value) async {},
    ),
  ),
);

List<Map<String, dynamic>> _history() => List.generate(
  40,
  (index) => <String, dynamic>{
    'id': 'message-$index',
    'role': index.isEven ? 'user' : 'assistant',
    'content': '消息 $index\n这里是一段用于滚动的内容',
  },
);

void main() {
  testWidgets(
    'attachment button selects files and draft chips can be removed',
    (tester) async {
      _mobileViewport(tester);
      final input = TextEditingController();
      final scroll = ScrollController();
      addTearDown(input.dispose);
      addTearDown(scroll.dispose);
      var picks = 0;
      String? removed;
      const files = <Map<String, dynamic>>[
        {'id': 'draft-file', 'filename': '报告.pdf'},
        {'file_id': 'draft-file-2', 'filename': '记录.txt'},
      ];
      await tester.pumpWidget(
        _app(
          input: input,
          scroll: scroll,
          attachments: files,
          onAttach: () => picks++,
          onRemoveAttachment: (id) => removed = id,
        ),
      );
      await tester.tap(find.byTooltip('附件'));
      await tester.pump();
      expect(picks, 1);
      expect(find.text('报告.pdf'), findsOneWidget);
      expect(find.text('记录.txt'), findsOneWidget);
      expect(
        find.descendant(
          of: find.byKey(const ValueKey('chat-attachment-drafts')),
          matching: find.byType(GlassSurface),
        ),
        findsNWidgets(2),
      );
      final remove = find.byKey(
        const ValueKey('chat-remove-attachment-draft-file'),
      );
      expect(tester.getSize(remove), const Size(44, 44));
      await tester.tap(remove);
      await tester.pump();
      expect(removed, 'draft-file');
      final list = tester.widget<ListView>(
        find.byKey(const ValueKey('chat-message-list')),
      );
      final draftTop = tester
          .getRect(find.byKey(const ValueKey('chat-attachment-drafts')))
          .top;
      expect(
        (list.padding! as EdgeInsets).bottom,
        closeTo(844 - draftTop + 12, 1),
      );

      removed = null;
      await tester.pumpWidget(
        _app(
          input: input,
          scroll: scroll,
          sending: true,
          attachments: files,
          onAttach: () => picks++,
          onRemoveAttachment: (id) => removed = id,
        ),
      );
      expect(
        tester
            .widget<IconButton>(
              find.ancestor(
                of: find.byTooltip('附件'),
                matching: find.byType(IconButton),
              ),
            )
            .onPressed,
        isNull,
      );
      expect(tester.widget<IconButton>(remove).onPressed, isNull);
      expect(picks, 1);
      expect(removed, isNull);
    },
  );

  testWidgets('sent file metadata opens through existing file callback', (
    tester,
  ) async {
    _mobileViewport(tester);
    final input = TextEditingController();
    final scroll = ScrollController();
    addTearDown(input.dispose);
    addTearDown(scroll.dispose);
    Map<String, dynamic>? opened;
    await tester.pumpWidget(
      _app(
        input: input,
        scroll: scroll,
        messages: const [
          {
            'role': 'user',
            'content': '看看这个文件',
            'metadata': {
              'files': [
                {
                  'id': 'sent-file',
                  'filename': '报告.pdf',
                  'media_type': 'application/pdf',
                  'size_bytes': 256,
                  'url': 'https://untrusted.example/file',
                },
              ],
            },
          },
        ],
        onFile: (file) async {
          opened = file;
        },
      ),
    );
    expect(find.text('报告.pdf'), findsOneWidget);
    await tester.tap(find.text('报告.pdf'));
    await tester.pump();
    expect(opened?['file_id'], 'sent-file');
    expect(opened?['filename'], '报告.pdf');
    expect(opened?.containsKey('url'), isFalse);
  });

  testWidgets('mobile composer is glass and blank chat dismisses focus', (
    tester,
  ) async {
    _mobileViewport(tester);
    final input = TextEditingController();
    final scroll = ScrollController();
    addTearDown(input.dispose);
    addTearDown(scroll.dispose);
    await tester.pumpWidget(_app(input: input, scroll: scroll));
    expect(
      find.descendant(
        of: find.byKey(const ValueKey('chat-mobile-composer')),
        matching: find.byType(GlassSurface),
      ),
      findsOneWidget,
    );
    final field = find.byKey(const ValueKey('chat-input'));
    await tester.tap(field);
    await tester.pump();
    expect(tester.widget<TextField>(field).focusNode!.hasFocus, isTrue);
    await tester.tapAt(const Offset(20, 180));
    await tester.pump();
    expect(tester.widget<TextField>(field).focusNode!.hasFocus, isFalse);
    final list = tester.widget<ListView>(
      find.byKey(const ValueKey('chat-message-list')),
    );
    expect(
      list.keyboardDismissBehavior,
      ScrollViewKeyboardDismissBehavior.onDrag,
    );
  });

  testWidgets('send clears text and dismisses mobile composer focus', (
    tester,
  ) async {
    _mobileViewport(tester);
    final input = TextEditingController();
    final scroll = ScrollController();
    addTearDown(input.dispose);
    addTearDown(scroll.dispose);
    var sends = 0;
    await tester.pumpWidget(
      _app(
        input: input,
        scroll: scroll,
        onSend: () {
          sends++;
          input.clear();
        },
      ),
    );
    final field = find.byKey(const ValueKey('chat-input'));
    await tester.tap(field);
    await tester.enterText(field, '你好 Luma');
    await tester.pump();
    await tester.tap(find.byTooltip('发送'));
    await tester.pump();
    expect(sends, 1);
    expect(input.text, isEmpty);
    expect(tester.widget<TextField>(field).focusNode!.hasFocus, isFalse);
    expect(find.byTooltip('语音输入'), findsOneWidget);
  });

  testWidgets('voice is disabled while generating and stop remains available', (
    tester,
  ) async {
    _mobileViewport(tester);
    final input = TextEditingController();
    final scroll = ScrollController();
    addTearDown(input.dispose);
    addTearDown(scroll.dispose);
    await tester.pumpWidget(_app(input: input, scroll: scroll));
    expect(find.byTooltip('语音输入'), findsOneWidget);
    var stopped = false;
    await tester.pumpWidget(
      _app(
        input: input,
        scroll: scroll,
        sending: true,
        onStop: () => stopped = true,
      ),
    );
    expect(
      tester
          .widget<IconButton>(
            find.byKey(const ValueKey('chat-voice-microphone')),
          )
          .onPressed,
      isNull,
    );
    await tester.tap(find.byTooltip('停止'));
    await tester.pump();
    expect(stopped, isTrue);
    expect(find.byIcon(Icons.stop_rounded), findsOneWidget);
  });

  testWidgets('keyboard moves composer above inset and scrolls to latest', (
    tester,
  ) async {
    _mobileViewport(tester);
    final input = TextEditingController();
    final scroll = ScrollController();
    final messages = _history();
    addTearDown(input.dispose);
    addTearDown(scroll.dispose);
    await tester.pumpWidget(
      _app(input: input, scroll: scroll, messages: messages),
    );
    scroll.jumpTo(scroll.position.maxScrollExtent - 20);
    await tester.pump();
    tester.view.viewInsets = const FakeViewPadding(bottom: 300);
    addTearDown(tester.view.resetViewInsets);
    await tester.pump();
    await tester.pump();
    expect(scroll.position.extentAfter, closeTo(0, 1));
    expect(
      tester.getRect(find.byKey(const ValueKey('chat-mobile-composer'))).bottom,
      closeTo(844 - 300 - 8, 1),
    );
  });

  testWidgets('new messages keep reading position until glass button tapped', (
    tester,
  ) async {
    _mobileViewport(tester);
    final input = TextEditingController();
    final scroll = ScrollController();
    final messages = _history();
    addTearDown(input.dispose);
    addTearDown(scroll.dispose);
    await tester.pumpWidget(
      _app(input: input, scroll: scroll, messages: messages),
    );
    scroll.jumpTo(0);
    await tester.pump();
    messages.add({
      'id': 'new-message',
      'role': 'assistant',
      'content': '新到达的回复',
    });
    await tester.pumpWidget(
      _app(input: input, scroll: scroll, messages: messages),
    );
    expect(scroll.offset, 0);
    expect(find.text('↓ 新消息'), findsOneWidget);
    await tester.tap(find.byKey(const ValueKey('chat-new-messages')));
    await tester.pumpAndSettle();
    expect(scroll.position.extentAfter, closeTo(0, 1));
    expect(find.text('↓ 新消息'), findsNothing);
    messages.last['content'] = '新到达的回复\n继续生成的内容';
    await tester.pumpWidget(
      _app(input: input, scroll: scroll, messages: messages),
    );
    await tester.pump();
    expect(scroll.position.extentAfter, closeTo(0, 1));
  });

  testWidgets('long press message offers copy and confirms clipboard', (
    tester,
  ) async {
    _mobileViewport(tester);
    final input = TextEditingController();
    final scroll = ScrollController();
    addTearDown(input.dispose);
    addTearDown(scroll.dispose);
    String? copied;
    tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(
      SystemChannels.platform,
      (call) async {
        if (call.method == 'Clipboard.setData') {
          copied = (call.arguments as Map)['text'] as String;
        }
        return null;
      },
    );
    addTearDown(
      () => tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(
        SystemChannels.platform,
        null,
      ),
    );
    await tester.pumpWidget(
      _app(
        input: input,
        scroll: scroll,
        messages: const [
          {'role': 'user', 'content': '你好'},
          {'role': 'assistant', 'content': '回复内容'},
        ],
      ),
    );
    await tester.longPress(find.text('回复内容'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('复制'));
    await tester.pumpAndSettle();
    expect(copied, '回复内容');
    expect(find.text('已复制'), findsOneWidget);
  });
}
