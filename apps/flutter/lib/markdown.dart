import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

enum MarkdownBlockKind {
  paragraph,
  heading,
  listItem,
  quote,
  rule,
  code,
  table,
}

@immutable
class MarkdownBlock {
  const MarkdownBlock({
    required this.kind,
    this.text = '',
    this.level = 0,
    this.marker = '',
    this.language = '',
    this.rows = const [],
    this.alignments = const [],
  });

  final MarkdownBlockKind kind;
  final String text;
  final int level;
  final String marker;
  final String language;
  final List<List<String>> rows;
  final List<TextAlign> alignments;
}

enum MarkdownInlineKind { text, bold, italic, strike, code, link }

@immutable
class MarkdownInline {
  const MarkdownInline({
    required this.kind,
    required this.text,
    this.children = const [],
    this.url,
  });

  final MarkdownInlineKind kind;
  final String text;
  final List<MarkdownInline> children;
  final String? url;

  String get plainText =>
      children.isEmpty ? text : children.map((child) => child.plainText).join();
}

final _fence = RegExp(r'^ {0,3}(`{3,}|~{3,})(.*)$');
final _heading = RegExp(r'^ {0,3}(#{1,6})\s+(.+)$');
final _list = RegExp(r'^(\s*)([-+*]|\d+[.)])\s+(.+)$');
final _quote = RegExp(r'^ {0,3}>\s?(.*)$');
final _rule = RegExp(r'^ {0,3}((?:-\s*){3,}|(?:\*\s*){3,}|(?:_\s*){3,})$');
final _tableSeparator = RegExp(r'^:?-{3,}:?$');
final _word = RegExp(r'[A-Za-z0-9_\u4e00-\u9fff]');

/// Parses only Markdown. Widget markers and secret masking stay with the
/// message-content parser before text is passed to this renderer.
List<MarkdownBlock> parseMarkdown(String data) {
  final lines = data
      .replaceAll('\r\n', '\n')
      .replaceAll('\r', '\n')
      .split('\n');
  final blocks = <MarkdownBlock>[];
  var index = 0;
  while (index < lines.length) {
    final line = lines[index];
    if (line.trim().isEmpty) {
      index++;
      continue;
    }
    final fence = _fence.firstMatch(line);
    if (fence != null) {
      final marker = fence.group(1)!;
      final content = <String>[];
      index++;
      while (index < lines.length && !_closesFence(lines[index], marker)) {
        content.add(lines[index++]);
      }
      if (index < lines.length) index++;
      blocks.add(
        MarkdownBlock(
          kind: MarkdownBlockKind.code,
          text: content.join('\n'),
          language: fence.group(2)!.trim(),
        ),
      );
      continue;
    }
    final table = _tableAlignments(lines, index);
    if (table != null) {
      final header = _tableCells(line);
      final rows = <List<String>>[header];
      index += 2;
      while (index < lines.length &&
          lines[index].trim().isNotEmpty &&
          _hasTablePipe(lines[index]) &&
          !_isBlockStart(lines, index)) {
        final cells = _tableCells(lines[index++]);
        rows.add(
          List.generate(
            header.length,
            (column) => column < cells.length ? cells[column] : '',
          ),
        );
      }
      blocks.add(
        MarkdownBlock(
          kind: MarkdownBlockKind.table,
          rows: rows,
          alignments: table,
        ),
      );
      continue;
    }
    final heading = _heading.firstMatch(line);
    if (heading != null) {
      blocks.add(
        MarkdownBlock(
          kind: MarkdownBlockKind.heading,
          text: heading.group(2)!.replaceFirst(RegExp(r'\s+#+\s*$'), ''),
          level: heading.group(1)!.length,
        ),
      );
      index++;
      continue;
    }
    if (_rule.hasMatch(line)) {
      blocks.add(const MarkdownBlock(kind: MarkdownBlockKind.rule));
      index++;
      continue;
    }
    final quote = _quote.firstMatch(line);
    if (quote != null) {
      final content = <String>[];
      while (index < lines.length) {
        final quoted = _quote.firstMatch(lines[index]);
        if (quoted == null) break;
        content.add(quoted.group(1)!);
        index++;
      }
      blocks.add(
        MarkdownBlock(kind: MarkdownBlockKind.quote, text: content.join('\n')),
      );
      continue;
    }
    final item = _list.firstMatch(line);
    if (item != null) {
      final indent = item.group(1)!.replaceAll('\t', '  ').length;
      final marker = item.group(2)!;
      final content = <String>[item.group(3)!];
      index++;
      while (index < lines.length &&
          lines[index].trim().isNotEmpty &&
          !_isBlockStart(lines, index) &&
          lines[index].startsWith(List.filled(indent + 2, ' ').join())) {
        content.add(lines[index++].trimLeft());
      }
      blocks.add(
        MarkdownBlock(
          kind: MarkdownBlockKind.listItem,
          text: content.join('\n'),
          level: indent >= 2 ? 1 : 0,
          marker: RegExp(r'^\d').hasMatch(marker)
              ? '${marker.substring(0, marker.length - 1)}.'
              : '•',
        ),
      );
      continue;
    }
    final content = <String>[line];
    index++;
    while (index < lines.length &&
        lines[index].trim().isNotEmpty &&
        !_isBlockStart(lines, index)) {
      content.add(lines[index++]);
    }
    blocks.add(
      MarkdownBlock(
        kind: MarkdownBlockKind.paragraph,
        text: content.join('\n'),
      ),
    );
  }
  return blocks;
}

bool _closesFence(String line, String marker) {
  final trimmed = line.trim();
  return trimmed.length >= marker.length &&
      trimmed.split('').every((character) => character == marker[0]);
}

bool _isBlockStart(List<String> lines, int index) {
  final line = lines[index];
  return _fence.hasMatch(line) ||
      _heading.hasMatch(line) ||
      _rule.hasMatch(line) ||
      _quote.hasMatch(line) ||
      _list.hasMatch(line) ||
      _tableAlignments(lines, index) != null;
}

List<TextAlign>? _tableAlignments(List<String> lines, int index) {
  if (index + 1 >= lines.length || !_hasTablePipe(lines[index])) return null;
  final header = _tableCells(lines[index]);
  final separator = _tableCells(lines[index + 1]);
  if (header.isEmpty ||
      separator.length != header.length ||
      !separator.every((cell) => _tableSeparator.hasMatch(cell))) {
    return null;
  }
  return separator.map((cell) {
    if (cell.startsWith(':') && cell.endsWith(':')) return TextAlign.center;
    if (cell.endsWith(':')) return TextAlign.right;
    return TextAlign.left;
  }).toList();
}

bool _hasTablePipe(String line) => _splitTableRow(line).length > 1;

List<String> _tableCells(String line) {
  final cells = _splitTableRow(line.trim());
  if (cells.length > 1 && cells.first.trim().isEmpty) cells.removeAt(0);
  if (cells.length > 1 && cells.last.trim().isEmpty) cells.removeLast();
  return cells.map((cell) => cell.trim()).toList();
}

List<String> _splitTableRow(String line) {
  final cells = <String>[];
  final cell = StringBuffer();
  var codeTicks = 0;
  var index = 0;
  while (index < line.length) {
    final character = line[index];
    if (character == '\\' && index + 1 < line.length) {
      cell.write(character);
      cell.write(line[index + 1]);
      index += 2;
      continue;
    }
    if (character == '`') {
      var end = index + 1;
      while (end < line.length && line[end] == '`') {
        end++;
      }
      final count = end - index;
      if (codeTicks == 0) {
        codeTicks = count;
      } else if (codeTicks == count) {
        codeTicks = 0;
      }
      cell.write(line.substring(index, end));
      index = end;
      continue;
    }
    if (character == '|' && codeTicks == 0) {
      cells.add(cell.toString());
      cell.clear();
    } else {
      cell.write(character);
    }
    index++;
  }
  cells.add(cell.toString());
  return cells;
}

List<MarkdownInline> parseMarkdownInline(String data) => _parseInline(data, 0);

List<MarkdownInline> _parseInline(String data, int depth) {
  if (depth >= 8) {
    return [MarkdownInline(kind: MarkdownInlineKind.text, text: data)];
  }
  final parts = <MarkdownInline>[];
  final plain = StringBuffer();
  void flush() {
    if (plain.isEmpty) return;
    parts.add(
      MarkdownInline(kind: MarkdownInlineKind.text, text: plain.toString()),
    );
    plain.clear();
  }

  var index = 0;
  while (index < data.length) {
    if (data[index] == '\\' &&
        index + 1 < data.length &&
        r'\`*_{}[]()#+-.!|>~'.contains(data[index + 1])) {
      plain.write(data[index + 1]);
      index += 2;
      continue;
    }
    if (data[index] == '`') {
      var end = index + 1;
      while (end < data.length && data[end] == '`') {
        end++;
      }
      final marker = data.substring(index, end);
      final close = data.indexOf(marker, end);
      if (close >= 0) {
        flush();
        parts.add(
          MarkdownInline(
            kind: MarkdownInlineKind.code,
            text: data.substring(end, close).replaceAll('\n', ' '),
          ),
        );
        index = close + marker.length;
        continue;
      }
      plain.write(marker);
      index = end;
      continue;
    }
    if (data[index] == '[') {
      final labelEnd = _linkLabelEnd(data, index + 1);
      if (labelEnd >= 0 &&
          labelEnd + 1 < data.length &&
          data[labelEnd + 1] == '(') {
        final close = _linkDestinationEnd(data, labelEnd + 2);
        if (close >= 0) {
          final label = data.substring(index + 1, labelEnd);
          final destination = data.substring(labelEnd + 2, close).trim();
          final url = _safeLink(destination);
          flush();
          if (url != null) {
            parts.add(
              MarkdownInline(
                kind: MarkdownInlineKind.link,
                text: label,
                children: _parseInline(label, depth + 1),
                url: url,
              ),
            );
          } else {
            parts.addAll(_parseInline(label, depth + 1));
          }
          index = close + 1;
          continue;
        }
      }
    }
    String? delimiter;
    MarkdownInlineKind? kind;
    for (final candidate in ['**', '__', '~~', '*', '_']) {
      if (!data.startsWith(candidate, index)) continue;
      if (candidate.length == 1 &&
          ((index > 0 && data[index - 1] == candidate) ||
              (index + 1 < data.length && data[index + 1] == candidate))) {
        continue;
      }
      final start = index + candidate.length;
      if (start >= data.length || data[start].trim().isEmpty) continue;
      if (candidate.startsWith('_') &&
          index > 0 &&
          _word.hasMatch(data[index - 1])) {
        continue;
      }
      delimiter = candidate;
      kind = candidate == '~~'
          ? MarkdownInlineKind.strike
          : candidate.length == 2
          ? MarkdownInlineKind.bold
          : MarkdownInlineKind.italic;
      break;
    }
    if (delimiter != null) {
      final start = index + delimiter.length;
      final close = _closingDelimiter(data, start, delimiter);
      if (close > start) {
        final content = data.substring(start, close);
        flush();
        parts.add(
          MarkdownInline(
            kind: kind!,
            text: content,
            children: _parseInline(content, depth + 1),
          ),
        );
        index = close + delimiter.length;
        continue;
      }
    }
    plain.write(data[index++]);
  }
  flush();
  return parts;
}

int _closingDelimiter(String data, int start, String delimiter) {
  var index = start;
  while (index < data.length) {
    if (data[index] == '\\') {
      index += 2;
      continue;
    }
    if (data[index] == '`') {
      var end = index + 1;
      while (end < data.length && data[end] == '`') {
        end++;
      }
      final close = data.indexOf(data.substring(index, end), end);
      if (close >= 0) {
        index = close + end - index;
        continue;
      }
    }
    if (data.startsWith(delimiter, index) &&
        index > start &&
        data[index - 1].trim().isNotEmpty) {
      final end = index + delimiter.length;
      final intrawordUnderscore =
          delimiter.startsWith('_') &&
          end < data.length &&
          _word.hasMatch(data[end]);
      final doubleMarker =
          delimiter.length == 1 &&
          ((index > 0 && data[index - 1] == delimiter) ||
              (end < data.length && data[end] == delimiter));
      if (!intrawordUnderscore && !doubleMarker) {
        // With a triple marker the first star closes nested emphasis, e.g.
        // **新 *口径*** and ***重点***.
        if (delimiter == '**' && data.startsWith('***', index)) {
          return index + 1;
        }
        return index;
      }
    }
    index++;
  }
  return -1;
}

int _linkLabelEnd(String data, int start) {
  for (var index = start; index < data.length; index++) {
    if (data[index] == '\\') {
      index++;
    } else if (data[index] == ']') {
      return index;
    }
  }
  return -1;
}

int _linkDestinationEnd(String data, int start) {
  var nesting = 0;
  for (var index = start; index < data.length; index++) {
    if (data[index] == '\\') {
      index++;
    } else if (data[index] == '(') {
      nesting++;
    } else if (data[index] == ')') {
      if (nesting == 0) return index;
      nesting--;
    }
  }
  return -1;
}

String? _safeLink(String destination) {
  var target = destination;
  if (target.startsWith('<')) {
    final end = target.indexOf('>');
    if (end < 0) return null;
    target = target.substring(1, end);
  } else {
    target = target.split(RegExp(r'\s+')).first;
  }
  final uri = Uri.tryParse(target);
  if (uri == null ||
      !['http', 'https'].contains(uri.scheme.toLowerCase()) ||
      !uri.hasAuthority ||
      uri.host.isEmpty) {
    return null;
  }
  return uri.toString();
}

/// A small, native renderer; model output never becomes HTML or a WebView.
class LumaMarkdown extends StatelessWidget {
  const LumaMarkdown({
    super.key,
    required this.data,
    required this.style,
    this.linkColor,
    this.selectable = false,
  });

  final String data;
  final TextStyle style;
  final Color? linkColor;
  final bool selectable;

  @override
  Widget build(BuildContext context) {
    final blocks = parseMarkdown(data);
    final content = Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        for (var index = 0; index < blocks.length; index++)
          Padding(
            padding: EdgeInsets.only(
              bottom: index == blocks.length - 1
                  ? 0
                  : blocks[index].kind == MarkdownBlockKind.listItem
                  ? 4
                  : 10,
            ),
            child: _block(context, blocks[index]),
          ),
      ],
    );
    return selectable ? SelectionArea(child: content) : content;
  }

  Widget _text(String data, {TextStyle? textStyle, TextAlign? align}) =>
      _MarkdownText(
        data: data,
        style: textStyle ?? style,
        linkColor: linkColor,
        textAlign: align ?? TextAlign.start,
      );

  Widget _block(BuildContext context, MarkdownBlock block) {
    final colors = Theme.of(context).colorScheme;
    switch (block.kind) {
      case MarkdownBlockKind.paragraph:
        return _text(block.text);
      case MarkdownBlockKind.heading:
        final size = (style.fontSize ?? 16) + (7 - block.level) * 1.5;
        return _text(
          block.text,
          textStyle: style.copyWith(
            fontSize: size,
            fontWeight: FontWeight.w700,
            height: 1.35,
          ),
        );
      case MarkdownBlockKind.listItem:
        return Padding(
          padding: EdgeInsets.only(left: block.level * 20.0),
          child: Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            mainAxisSize: MainAxisSize.min,
            children: [
              SizedBox(width: 26, child: Text(block.marker, style: style)),
              Flexible(child: _text(block.text)),
            ],
          ),
        );
      case MarkdownBlockKind.quote:
        return Container(
          decoration: BoxDecoration(
            border: Border(
              left: BorderSide(color: colors.outlineVariant, width: 3),
            ),
          ),
          padding: const EdgeInsets.only(left: 12),
          child: LumaMarkdown(
            data: block.text,
            style: style.copyWith(color: style.color?.withValues(alpha: .8)),
            linkColor: linkColor,
          ),
        );
      case MarkdownBlockKind.rule:
        return Divider(height: 16, color: colors.outlineVariant);
      case MarkdownBlockKind.code:
        return Semantics(
          label: '代码块，长按复制',
          child: GestureDetector(
            onLongPress: () => _copy(context, block.text, '代码已复制'),
            child: Container(
              width: double.infinity,
              padding: const EdgeInsets.all(12),
              decoration: BoxDecoration(
                color: colors.surfaceContainerHighest.withValues(alpha: .65),
                borderRadius: BorderRadius.circular(8),
                border: Border.all(color: colors.outlineVariant, width: .5),
              ),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                mainAxisSize: MainAxisSize.min,
                children: [
                  if (block.language.isNotEmpty)
                    Padding(
                      padding: const EdgeInsets.only(bottom: 6),
                      child: Text(
                        block.language,
                        style: style.copyWith(
                          fontSize: (style.fontSize ?? 16) - 3,
                          color: colors.onSurfaceVariant,
                        ),
                      ),
                    ),
                  SingleChildScrollView(
                    scrollDirection: Axis.horizontal,
                    child: Text(
                      block.text,
                      style: style.copyWith(
                        fontFamily: 'monospace',
                        height: 1.5,
                      ),
                    ),
                  ),
                ],
              ),
            ),
          ),
        );
      case MarkdownBlockKind.table:
        return _table(context, block);
    }
  }

  Widget _table(BuildContext context, MarkdownBlock block) {
    final colors = Theme.of(context).colorScheme;
    final widths = <int, TableColumnWidth>{};
    for (var column = 0; column < block.alignments.length; column++) {
      var width = 90.0;
      for (final row in block.rows) {
        final text = parseMarkdownInline(row[column])
            .map((part) => part.plainText)
            .join();
        final painter = TextPainter(
          text: TextSpan(text: text, style: style),
          textDirection: Directionality.of(context),
        )..layout();
        final measured = (painter.width + 24).clamp(90.0, 240.0).toDouble();
        if (measured > width) width = measured;
        painter.dispose();
      }
      widths[column] = FixedColumnWidth(width);
    }
    return SingleChildScrollView(
      scrollDirection: Axis.horizontal,
      child: Table(
        columnWidths: widths,
        border: TableBorder.all(color: colors.outlineVariant, width: .5),
        defaultVerticalAlignment: TableCellVerticalAlignment.middle,
        children: [
          for (var row = 0; row < block.rows.length; row++)
            TableRow(
              decoration: row == 0
                  ? BoxDecoration(
                      color: colors.surfaceContainerHighest.withValues(
                        alpha: .7,
                      ),
                    )
                  : null,
              children: [
                for (var column = 0; column < block.alignments.length; column++)
                  Padding(
                    padding: const EdgeInsets.symmetric(
                      horizontal: 10,
                      vertical: 8,
                    ),
                    child: _text(
                      block.rows[row][column],
                      textStyle: row == 0
                          ? style.copyWith(fontWeight: FontWeight.w600)
                          : style,
                      align: block.alignments[column],
                    ),
                  ),
              ],
            ),
        ],
      ),
    );
  }
}

class _MarkdownText extends StatefulWidget {
  const _MarkdownText({
    required this.data,
    required this.style,
    required this.textAlign,
    this.linkColor,
  });

  final String data;
  final TextStyle style;
  final TextAlign textAlign;
  final Color? linkColor;

  @override
  State<_MarkdownText> createState() => _MarkdownTextState();
}

class _MarkdownTextState extends State<_MarkdownText> {
  late List<MarkdownInline> _parts;
  final _recognizers = <MarkdownInline, TapGestureRecognizer>{};

  @override
  void initState() {
    super.initState();
    _parse();
  }

  @override
  void didUpdateWidget(covariant _MarkdownText oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.data != widget.data) {
      _disposeRecognizers();
      _parse();
    }
  }

  void _parse() {
    _parts = parseMarkdownInline(widget.data);
    void register(List<MarkdownInline> parts) {
      for (final part in parts) {
        if (part.kind == MarkdownInlineKind.link && part.url != null) {
          _recognizers[part] = TapGestureRecognizer()
            ..onTap = () => _copy(context, part.url!, '链接已复制');
        }
        register(part.children);
      }
    }

    register(_parts);
  }

  void _disposeRecognizers() {
    for (final recognizer in _recognizers.values) {
      recognizer.dispose();
    }
    _recognizers.clear();
  }

  @override
  void dispose() {
    _disposeRecognizers();
    super.dispose();
  }

  TextSpan _span(
    MarkdownInline part,
    TextStyle style,
    GestureRecognizer? link,
  ) {
    final colors = Theme.of(context).colorScheme;
    var childStyle = style;
    switch (part.kind) {
      case MarkdownInlineKind.bold:
        childStyle = style.copyWith(fontWeight: FontWeight.w700);
      case MarkdownInlineKind.italic:
        childStyle = style.copyWith(fontStyle: FontStyle.italic);
      case MarkdownInlineKind.strike:
        childStyle = style.copyWith(decoration: TextDecoration.lineThrough);
      case MarkdownInlineKind.code:
        childStyle = style.copyWith(
          fontFamily: 'monospace',
          backgroundColor: colors.surfaceContainerHighest.withValues(alpha: .6),
        );
      case MarkdownInlineKind.link:
        childStyle = style.copyWith(
          color: widget.linkColor ?? colors.primary,
          decoration: TextDecoration.underline,
        );
        link = _recognizers[part];
      case MarkdownInlineKind.text:
        break;
    }
    return TextSpan(
      text: part.children.isEmpty ? part.text : null,
      style: childStyle,
      recognizer: part.children.isEmpty ? link : null,
      children: part.children.isEmpty
          ? null
          : [for (final child in part.children) _span(child, childStyle, link)],
    );
  }

  @override
  Widget build(BuildContext context) => Text.rich(
    TextSpan(
      children: [for (final part in _parts) _span(part, widget.style, null)],
    ),
    style: widget.style,
    textAlign: widget.textAlign,
  );
}

Future<void> _copy(BuildContext context, String value, String message) async {
  await Clipboard.setData(ClipboardData(text: value));
  if (!context.mounted) return;
  ScaffoldMessenger.maybeOf(context)?.showSnackBar(
    SnackBar(content: Text(message), duration: const Duration(seconds: 1)),
  );
}
