import 'package:flutter_test/flutter_test.dart';
import 'package:luma_client/pseudo_markup.dart';

void main() {
  test('checklist pseudo call is a loading placeholder while streaming', () {
    const content = '''给你看一个清单组件：<tool_call><function=luma-ui>
{"type":"checklist","title":"我的一天","items":[]}
</parameter>
</invoke>
</function_calls>
后面的话''';

    final parts = parseMessageContent(content, streaming: true);

    expect(parts.map((part) => part.type), [
      MessageContentPartType.text,
      MessageContentPartType.loading,
      MessageContentPartType.text,
    ]);
    expect(parts[0].value, '给你看一个清单组件：');
    expect(parts[2].value, '\n后面的话');
  });

  test('final checklist pseudo call is removed', () {
    const content = '''给你看一个清单组件：<tool_call><function=luma-ui>
{"type":"checklist","title":"我的一天","items":[]}
</parameter>
</invoke>
</function_calls>
后面的话''';

    final parts = parseMessageContent(content, streaming: false);

    expect(parts.map((part) => part.type), [MessageContentPartType.text]);
    expect(parts.single.value, '给你看一个清单组件：\n后面的话');
  });

  test('incomplete tool opener at the end is hidden while streaming', () {
    final parts = parseMessageContent('正文\n<tool_', streaming: true);

    expect(parts.map((part) => part.type), [
      MessageContentPartType.text,
      MessageContentPartType.loading,
    ]);
    expect(parts.first.value, '正文\n');
  });

  test('final incomplete pseudo call is removed', () {
    final parts = parseMessageContent(
      '正文<tool_call>{"type":"checklist"}',
      streaming: false,
    );

    expect(parts.map((part) => part.type), [MessageContentPartType.text]);
    expect(parts.single.value, '正文');
  });

  test('ordinary code fences preserve pseudo tags', () {
    const content = '''说明：
```
<tool_call>这里是示例</tool_call>
[[widget:x]]
```
结束''';

    final parts = parseMessageContent(content, streaming: false);

    expect(
      parts.every((part) => part.type == MessageContentPartType.text),
      isTrue,
    );
    expect(parts.map((part) => part.value).join(), content);
  });

  test('pseudo call and widget marker keep only the widget component', () {
    const content = '''<tool_call>{"type":"checklist"}</tool_call>
[[widget:x]]''';

    final parts = parseMessageContent(content, streaming: false);

    expect(parts.map((part) => part.type), [MessageContentPartType.widget]);
    expect(parts.single.value, 'x');
  });

  test('A: strong tag discussed in inline code is preserved finally and while streaming', () {
    const content = '模型有时会输出 `<tool_call>` 这种标签，这是格式错误。下面是正确写法。';

    for (final streaming in [false, true]) {
      final parts = parseMessageContent(content, streaming: streaming);

      expect(parts.map((part) => part.type), [MessageContentPartType.text]);
      expect(parts.single.value, content);
    }
  });

  test('B: strong tag followed by prose is preserved', () {
    const content = '说明：<function_calls> 不是工具。后面还有很多正文。';

    for (final streaming in [false, true]) {
      final parts = parseMessageContent(content, streaming: streaming);

      expect(parts.map((part) => part.type), [MessageContentPartType.text]);
      expect(parts.single.value, content);
    }
  });

  test('C: balanced JSON pseudo call is removed or replaced by loading', () {
    const content = '''看这个：<tool_call>
<parameter name="spec">{"type":"checklist","title":"t","items":[{"id":"a","label":"x"}]}</parameter>
</tool_call>
后面的话''';

    final finalParts = parseMessageContent(content, streaming: false);
    expect(finalParts.map((part) => part.type), [MessageContentPartType.text]);
    expect(finalParts.single.value, '看这个：\n后面的话');

    final streamingParts = parseMessageContent(content, streaming: true);
    expect(streamingParts.map((part) => part.type), [
      MessageContentPartType.text,
      MessageContentPartType.loading,
      MessageContentPartType.text,
    ]);
    expect(streamingParts[0].value, '看这个：');
    expect(streamingParts[2].value, '\n后面的话');
  });

  test('D: streaming weak tags at the end show loading', () {
    final parts = parseMessageContent(
      '好的：<tool_call><function=luma-ui>\n',
      streaming: true,
    );

    expect(parts.map((part) => part.type), [
      MessageContentPartType.text,
      MessageContentPartType.loading,
    ]);
    expect(parts[0].value, '好的：');
  });

  test('E: streaming truncated JSON shows loading', () {
    final parts = parseMessageContent(
      '好的：<tool_call><function=luma-ui>\n{"type":"choice","ti',
      streaming: true,
    );

    expect(parts.map((part) => part.type), [
      MessageContentPartType.text,
      MessageContentPartType.loading,
    ]);
    expect(parts[0].value, '好的：');
  });

  test(
    'F: inline explanation and a later real pseudo call are independent',
    () {
      const content =
          '解释 `<tool_call>` 是错误格式。真正的：<tool_call>{"type":"choice"}后文';

      final finalParts = parseMessageContent(content, streaming: false);

      expect(finalParts.map((part) => part.type), [MessageContentPartType.text]);
      expect(
        finalParts.single.value,
        '解释 `<tool_call>` 是错误格式。真正的：后文',
      );

      final streamingParts = parseMessageContent(content, streaming: true);
      expect(streamingParts.map((part) => part.type), [
        MessageContentPartType.text,
        MessageContentPartType.loading,
        MessageContentPartType.text,
      ]);
      expect(streamingParts[0].value, '解释 `<tool_call>` 是错误格式。真正的：');
      expect(streamingParts[2].value, '后文');
    },
  );
}
