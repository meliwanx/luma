/// Parts of an assistant message that have a rendering meaning.
enum MessageContentPartType { text, widget, loading }

class MessageContentPart {
  const MessageContentPart.text(this.value)
    : type = MessageContentPartType.text;

  const MessageContentPart.widget(this.value)
    : type = MessageContentPartType.widget;

  const MessageContentPart.loading()
    : type = MessageContentPartType.loading,
      value = '';

  final MessageContentPartType type;
  final String value;
}

class PseudoMarkupSpan {
  const PseudoMarkupSpan(this.start, this.end, {required this.closed});

  final int start;
  final int end;
  final bool closed;
}

final _strongOpen = RegExp(
  r'''<\s*(?:tool_call\b[^>]*|function_calls\b[^>]*|function\s*=\s*luma[^>]*|invoke\s+name\s*=\s*["']?\s*luma[^>]*)\s*>''',
  caseSensitive: false,
);
final _weakOpen = RegExp(
  r'''<\s*(?:parameter\b[^>]*|invoke\b[^>]*|function\s*=[^>]*|tool_call\b[^>]*|function_calls\b[^>]*)\s*>''',
  caseSensitive: false,
);
final _closeTag = RegExp(
  r'</\s*(?:parameter|invoke|function_calls|tool_call|function)\s*>',
  caseSensitive: false,
);
final _widgetMarker = RegExp(r'\[\[widget:([A-Za-z0-9_-]+)\]\]');

/// Finds fake tool-call spans outside ordinary Markdown code fences.
///
/// A strong opener is treated as a fake call only when the content after it
/// has the expected JSON shape. An incomplete call extends to the end of the
/// message, which is what streaming rendering needs while it is still being
/// produced.
List<PseudoMarkupSpan> findPseudoMarkupSpans(String content) {
  final spans = <PseudoMarkupSpan>[];
  var cursor = 0;
  while (cursor < content.length) {
    if (_startsFence(content, cursor)) {
      final fence = _fenceEnd(content, cursor);
      cursor = fence.end;
      continue;
    }
    final inlineCodeEnd = _inlineCodeEnd(content, cursor);
    if (inlineCodeEnd != null) {
      cursor = inlineCodeEnd;
      continue;
    }
    final opener = _strongOpen.matchAsPrefix(content, cursor);
    if (opener != null) {
      final end = _pseudoEnd(content, opener.end);
      if (end != null) {
        spans.add(PseudoMarkupSpan(cursor, end.end, closed: end.closed));
        cursor = end.end;
        continue;
      }
    }
    if (content[cursor] == '<' &&
        _isIncompleteStrongPrefix(content.substring(cursor))) {
      spans.add(PseudoMarkupSpan(cursor, content.length, closed: false));
      break;
    }
    cursor++;
  }
  return spans;
}

/// Splits assistant content into text, widget markers and loading placeholders.
///
/// Ordinary fenced code is kept as text byte-for-byte. luma-ui fences and fake
/// tool calls are hidden in final messages and shown as a loading placeholder
/// while streaming.
List<MessageContentPart> parseMessageContent(
  String content, {
  required bool streaming,
}) {
  final parts = <MessageContentPart>[];
  final text = StringBuffer();
  final pseudoSpans = findPseudoMarkupSpans(content);
  var pseudoIndex = 0;

  void flushText() {
    final value = text.toString();
    text.clear();
    if (value.isEmpty || value.trim().isEmpty) return;

    final markers = _widgetMarker
        .allMatches(value)
        .where((marker) => !_isInInlineCode(value, marker.start))
        .toList();
    if (markers.isEmpty) {
      _addText(parts, value);
      return;
    }

    var cursor = 0;
    for (final marker in markers) {
      _addText(parts, value.substring(cursor, marker.start));
      parts.add(MessageContentPart.widget(marker.group(1)!));
      cursor = marker.end;
    }
    _addText(parts, value.substring(cursor));
  }

  var cursor = 0;
  while (cursor < content.length) {
    if (_startsFence(content, cursor)) {
      final fence = _fenceEnd(content, cursor);
      if (fence.lumaUi) {
        flushText();
        if (streaming) parts.add(const MessageContentPart.loading());
      } else {
        flushText();
        parts.add(
          MessageContentPart.text(content.substring(cursor, fence.end)),
        );
      }
      cursor = fence.end;
      continue;
    }

    final inlineCodeEnd = _inlineCodeEnd(content, cursor);
    if (inlineCodeEnd != null) {
      text.write(content.substring(cursor, inlineCodeEnd));
      cursor = inlineCodeEnd;
      continue;
    }

    if (pseudoIndex < pseudoSpans.length &&
        pseudoSpans[pseudoIndex].start == cursor) {
      final end = pseudoSpans[pseudoIndex++];
      flushText();
      if (streaming) parts.add(const MessageContentPart.loading());
      cursor = end.end;
      continue;
    }

    text.write(content[cursor]);
    cursor++;
  }
  flushText();
  return _mergeAdjacentText(parts);
}

void _addText(List<MessageContentPart> parts, String value) {
  if (value.isEmpty || value.trim().isEmpty) return;
  parts.add(MessageContentPart.text(value));
}

/// Adjacent text fragments have no rendering distinction. Keep a single
/// fragment so one sentence is rendered as one message bubble. Widgets and
/// loading placeholders remain boundaries between text fragments.
List<MessageContentPart> _mergeAdjacentText(List<MessageContentPart> parts) {
  final merged = <MessageContentPart>[];
  for (final part in parts) {
    if (part.type == MessageContentPartType.text && part.value.trim().isEmpty) {
      continue;
    }
    if (part.type == MessageContentPartType.text &&
        merged.isNotEmpty &&
        merged.last.type == MessageContentPartType.text) {
      final previous = merged.removeLast();
      merged.add(MessageContentPart.text(previous.value + part.value));
    } else {
      merged.add(part);
    }
  }
  return merged;
}

bool _startsFence(String content, int start) {
  if (start + 2 >= content.length || !content.startsWith('```', start)) {
    return false;
  }
  final previousNewline =
      start == 0 ? -1 : content.lastIndexOf('\n', start - 1);
  final lineStart = previousNewline + 1;
  for (var cursor = lineStart; cursor < start; cursor++) {
    if (content[cursor] != ' ' && content[cursor] != '\t') return false;
  }
  return true;
}

_FenceEnd _fenceEnd(String content, int start) {
  final infoEnd = _lineEnd(content, start + 3);
  final info = content.substring(start + 3, infoEnd).trim().toLowerCase();
  final lumaUi = RegExp(r'^luma-ui(?:\s|$)').hasMatch(info);
  final close = content.indexOf('```', infoEnd);
  return _FenceEnd(close < 0 ? content.length : close + 3, lumaUi);
}

int _lineEnd(String content, int start) {
  final newline = content.indexOf('\n', start);
  return newline < 0 ? content.length : newline;
}

_PseudoEnd? _pseudoEnd(String content, int start) {
  var cursor = _skipWhitespace(content, start);
  while (true) {
    final weakOpen = _weakOpen.matchAsPrefix(content, cursor);
    if (weakOpen == null) break;
    cursor = _skipWhitespace(content, weakOpen.end);
  }

  // A strong marker followed by ordinary prose is just prose. This check is
  // what prevents explanatory uses of tags from consuming the rest of a
  // message.
  if (cursor >= content.length) return _PseudoEnd(content.length, false);
  if (content[cursor] != '{') return null;

  final jsonEnd = _balancedJsonObjectEnd(content, cursor);
  if (jsonEnd == null) return _PseudoEnd(content.length, false);

  var end = jsonEnd;
  while (true) {
    final next = _closeTag.matchAsPrefix(
      content,
      _skipWhitespace(content, end),
    );
    if (next == null) break;
    end = next.end;
  }
  return _PseudoEnd(end, true);
}

int _skipWhitespace(String content, int start) {
  var cursor = start;
  while (cursor < content.length && content[cursor].trim().isEmpty) {
    cursor++;
  }
  return cursor;
}

/// Returns the end of a balanced JSON object starting at [start]. The parser
/// only needs JSON's brace/string rules here; decoding is deliberately left to
/// the widget layer.
int? _balancedJsonObjectEnd(String content, int start) {
  if (start >= content.length || content[start] != '{') return null;

  var depth = 0;
  var inString = false;
  var escaped = false;
  for (var cursor = start; cursor < content.length; cursor++) {
    final character = content[cursor];
    if (inString) {
      if (escaped) {
        escaped = false;
      } else if (character == '\\') {
        escaped = true;
      } else if (character == '"') {
        inString = false;
      }
      continue;
    }

    if (character == '"') {
      inString = true;
    } else if (character == '{') {
      depth++;
    } else if (character == '}') {
      depth--;
      if (depth == 0) return cursor + 1;
    }
  }
  return null;
}

/// Finds a single-backtick inline-code span on one line. Triple-backtick
/// fences are handled before this helper by the caller.
int? _inlineCodeEnd(String content, int start) {
  if (start >= content.length || content[start] != '`') return null;
  if ((start > 0 && content[start - 1] == '`') ||
      (start + 1 < content.length && content[start + 1] == '`')) {
    return null;
  }

  final lineEnd = _lineEnd(content, start + 1);
  final close = content.indexOf('`', start + 1);
  if (close < 0 || close >= lineEnd) return null;
  if ((close > start + 1 && content[close - 1] == '`') ||
      (close + 1 < content.length && content[close + 1] == '`')) {
    return null;
  }
  return close + 1;
}

bool _isInInlineCode(String content, int index) {
  var cursor = 0;
  while (cursor < index) {
    final opening = content.indexOf('`', cursor);
    if (opening < 0 || opening >= index) return false;
    final end = _inlineCodeEnd(content, opening);
    if (end == null) {
      cursor = opening + 1;
    } else {
      if (index < end) return true;
      cursor = end;
    }
  }
  return false;
}

bool _isIncompleteStrongPrefix(String suffix) {
  if (suffix.isEmpty || suffix.contains('>')) return false;
  final lower = suffix.toLowerCase();
  if (RegExp(r'^<\s*tool(?:[_a-z]*)?\s*$').hasMatch(lower)) {
    return true;
  }
  if (RegExp(
    r'^<\s*function(?:[_a-z]*|\s*=\s*(?:luma[\w-]*)?)?\s*$',
  ).hasMatch(lower)) {
    return true;
  }
  return RegExp(
    r'''^<\s*invoke\s+name\s*=\s*["']?luma[\w-]*\s*$''',
  ).hasMatch(lower);
}

class _FenceEnd {
  const _FenceEnd(this.end, this.lumaUi);

  final int end;
  final bool lumaUi;
}

class _PseudoEnd {
  const _PseudoEnd(this.end, this.closed);

  final int end;
  final bool closed;
}
