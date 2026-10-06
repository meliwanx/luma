import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:luma_client/markdown.dart';

Iterable<MarkdownInline> _inlineTree(List<MarkdownInline> parts) sync* {
  for (final part in parts) {
    yield part;
    yield* _inlineTree(part.children);
  }
}

Iterable<TextSpan> _spans(InlineSpan span) sync* {
  if (span is TextSpan) {
    yield span;
    for (final child in span.children ?? <InlineSpan>[]) {
      yield* _spans(child);
    }
  }
}

Future<void> _mount(WidgetTester tester, String text, {double fontSize = 16}) =>
    tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: Align(
            alignment: Alignment.topLeft,
            child: SizedBox(
              width: 300,
              child: LumaMarkdown(
                data: text,
                style: TextStyle(fontSize: fontSize, color: Colors.black),
              ),
            ),
          ),
        ),
      ),
    );

void main() {
  test('GFM table parses headings, cells, and per-column alignment', () {
    final table = parseMarkdown('''| 周期 | 状态 | 数量 |
| :--- | :---: | ---: |
| 本周 | **完成** | 12 |
| 下周 | 等待 | 3 |''').single;

    expect(table.kind, MarkdownBlockKind.table);
    expect(table.alignments, [
      TextAlign.left,
      TextAlign.center,
      TextAlign.right,
    ]);
    expect(table.rows, [
      ['周期', '状态', '数量'],
      ['本周', '**完成**', '12'],
      ['下周', '等待', '3'],
    ]);
  });

  test('table accepts omitted leading and trailing pipes', () {
    final table = parseMarkdown('''周期 | 状态
--- | ---:
本周 | 完成''').single;

    expect(table.kind, MarkdownBlockKind.table);
    expect(table.rows.last, ['本周', '完成']);
    expect(table.alignments, [TextAlign.left, TextAlign.right]);
  });

  test('escaped pipes and code pipes stay inside their table cells', () {
    final table = parseMarkdown(r'''| 说明 | 示例 |
| --- | --- |
| a\|b | `x|y` |''').single;

    expect(table.rows.last, [r'a\|b', '`x|y`']);
    expect(parseMarkdownInline(table.rows.last.first).single.plainText, 'a|b');
  });

  test('incomplete table rows are padded while streaming', () {
    final table = parseMarkdown('''| A | B | C |
| --- | --- | --- |
| 已收到 |''').single;

    expect(table.rows.last, ['已收到', '', '']);
    const stream = '| A | B |\n| --- | --- |\n| 正在传输 | 下一列';
    for (var length = 0; length <= stream.length; length++) {
      expect(() => parseMarkdown(stream.substring(0, length)), returnsNormally);
    }
  });

  test('unclosed fenced code preserves remaining streamed text', () {
    final code = parseMarkdown('```python\nprint("你好")\nunfinished').single;

    expect(code.kind, MarkdownBlockKind.code);
    expect(code.language, 'python');
    expect(code.text, 'print("你好")\nunfinished');
    expect(parseMarkdown('~~~\nx\n~~~').single.text, 'x');
  });

  test('Chinese bold has correct boundary before full-width punctuation', () {
    final parts = parseMarkdownInline('**口径说明**（取数时沿用）');

    expect(parts.first.kind, MarkdownInlineKind.bold);
    expect(parts.first.plainText, '口径说明');
    expect(parts.last.kind, MarkdownInlineKind.text);
    expect(parts.last.plainText, '（取数时沿用）');
  });

  test('snake_case and internal double underscores are not italic', () {
    final parts = parseMarkdownInline(
      'snake_case_and_more foo__bar__baz _斜体_ *强调*',
    );
    final italics = _inlineTree(parts)
        .where((part) => part.kind == MarkdownInlineKind.italic)
        .map((part) => part.plainText);

    expect(italics, ['斜体', '强调']);
    expect(
      parts.first.plainText,
      startsWith('snake_case_and_more foo__bar__baz'),
    );
  });

  test('inline code, strike, and nested emphasis retain content', () {
    final parts = parseMarkdownInline('`snake_case` ~~旧口径~~ **新 *口径***');
    final nodes = _inlineTree(parts).toList();

    expect(
      nodes.any(
        (part) =>
            part.kind == MarkdownInlineKind.code && part.text == 'snake_case',
      ),
      isTrue,
    );
    expect(
      nodes.any(
        (part) =>
            part.kind == MarkdownInlineKind.strike && part.plainText == '旧口径',
      ),
      isTrue,
    );
    expect(nodes.any((part) => part.kind == MarkdownInlineKind.bold), isTrue);
    expect(
      nodes.any(
        (part) =>
            part.kind == MarkdownInlineKind.italic && part.plainText == '口径',
      ),
      isTrue,
    );
  });

  test('only absolute http and https links become interactive', () {
    final parts = parseMarkdownInline('''[危险](javascript:alert(1))
[文件](file:///tmp/a) [相对](/admin) [数据](data:text/plain,x)
[允许](https://example.com/docs) [也允许](HTTP://example.com)''');
    final links = _inlineTree(parts)
        .where((part) => part.kind == MarkdownInlineKind.link)
        .toList();

    expect(links.map((part) => part.plainText), ['允许', '也允许']);
    expect(
      links.every(
        (part) => [
          'http',
          'https',
        ].contains(Uri.parse(part.url!).scheme.toLowerCase()),
      ),
      isTrue,
    );
    expect(parts.map((part) => part.plainText).join(), contains('危险'));
  });

  test('headings, ordered and nested lists, quotes, and rules parse', () {
    final blocks = parseMarkdown('''## 标题
1. 第一项
  - 子项
2. 第二项
> 引用
> **重点**
---''');

    expect(blocks.map((block) => block.kind), [
      MarkdownBlockKind.heading,
      MarkdownBlockKind.listItem,
      MarkdownBlockKind.listItem,
      MarkdownBlockKind.listItem,
      MarkdownBlockKind.quote,
      MarkdownBlockKind.rule,
    ]);
    expect(blocks[0].level, 2);
    expect(blocks[1].marker, '1.');
    expect(blocks[2].level, 1);
    expect(blocks[2].marker, '•');
    expect(blocks[4].text, '引用\n**重点**');
  });

  testWidgets(
    'table renders native cells, header fill, and horizontal scrolling',
    (tester) async {
      await _mount(tester, '''| A | B | C | D |
| :--- | :---: | ---: | --- |
| **粗体** | 中间 | 10 | `code` |''', fontSize: 18);

      final table = tester.widget<Table>(find.byType(Table));
      expect(table.children.first.decoration, isNotNull);
      expect(table.border, isNotNull);
      expect(table.children.every((row) => row.children.length == 4), isTrue);
      final scroller = tester.widget<SingleChildScrollView>(
        find.byType(SingleChildScrollView),
      );
      expect(scroller.scrollDirection, Axis.horizontal);
      final bold = tester.widget<Text>(find.text('粗体'));
      expect(bold.style!.fontSize, 18);
      expect(
        _spans(bold.textSpan!)
            .any((span) => span.style?.fontWeight == FontWeight.w700),
        isTrue,
      );
      final centered = tester.widget<Text>(find.text('中间'));
      expect(centered.textAlign, TextAlign.center);
      expect(tester.widget<Text>(find.text('10')).textAlign, TextAlign.right);
      await tester.drag(
        find.byType(SingleChildScrollView),
        const Offset(-80, 0),
      );
      await tester.pumpAndSettle();
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets('plain multiline paragraphs remain one text widget', (
    tester,
  ) async {
    const text = '第一行消息\n第二行消息';
    await _mount(tester, text, fontSize: 14);

    expect(find.text(text), findsOneWidget);
    expect(tester.widget<Text>(find.text(text)).style!.fontSize, 14);
  });

  testWidgets('unsafe link label has no tap recognizer', (tester) async {
    await _mount(tester, '[危险](javascript:alert(1))');

    final text = tester.widget<Text>(find.text('危险'));
    expect(
      _spans(text.textSpan!).every((span) => span.recognizer == null),
      isTrue,
    );
  });

  testWidgets('safe link copies its destination when tapped', (tester) async {
    String? copied;
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(SystemChannels.platform, (call) async {
          if (call.method == 'Clipboard.setData') {
            copied = (call.arguments as Map)['text'] as String;
          }
          return null;
        });
    addTearDown(
      () => TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
          .setMockMethodCallHandler(SystemChannels.platform, null),
    );
    await _mount(tester, '[参考文档](https://example.com/docs)');

    final text = tester.widget<Text>(find.text('参考文档'));
    expect(
      _spans(text.textSpan!)
          .any((span) => span.recognizer is TapGestureRecognizer),
      isTrue,
    );
    await tester.tap(find.text('参考文档'));
    await tester.pump();
    expect(copied, 'https://example.com/docs');
  });

  testWidgets('code block supports long-press copy and horizontal scrolling', (
    tester,
  ) async {
    String? copied;
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(SystemChannels.platform, (call) async {
          if (call.method == 'Clipboard.setData') {
            copied = (call.arguments as Map)['text'] as String;
          }
          return null;
        });
    addTearDown(
      () => TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
          .setMockMethodCallHandler(SystemChannels.platform, null),
    );
    const code =
        'a very long line of code that can scroll beyond the available width';
    await _mount(tester, '```text\n$code');

    expect(find.bySemanticsLabel(RegExp('代码块，长按复制')), findsOneWidget);
    expect(tester.widget<Text>(find.text(code)).style!.fontFamily, 'monospace');
    expect(
      tester
          .widget<SingleChildScrollView>(find.byType(SingleChildScrollView))
          .scrollDirection,
      Axis.horizontal,
    );
    await tester.longPressAt(
      tester.getTopLeft(find.text(code)) + const Offset(30, 10),
    );
    await tester.pump();
    expect(copied, code);
    expect(tester.takeException(), isNull);
  });

  testWidgets(
    'partial fences and partial table cells render without exceptions',
    (tester) async {
      for (final text in [
        '| 标题 |',
        '| A | B |\n| --- |',
        '| A | B |\n| --- | --- |\n| x |',
        '```',
        '```dart\nprint(',
        '**未完成',
      ]) {
        await _mount(tester, text);
        expect(tester.takeException(), isNull);
      }
    },
  );
}
