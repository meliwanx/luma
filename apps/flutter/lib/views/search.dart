import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../api.dart';
import '../brand.dart';
import '../glass.dart';
import '../session_utils.dart';
import '../theme.dart';

/// Keeps search requests outside text input bursts and cancels on teardown.
class SearchDebouncer {
  SearchDebouncer(
    this.onQuery, {
    this.delay = const Duration(milliseconds: 300),
  });

  final ValueChanged<String> onQuery;
  final Duration delay;
  Timer? _timer;
  bool _disposed = false;

  void schedule(String value) {
    _timer?.cancel();
    if (_disposed) return;
    final query = value.trim();
    if (query.isEmpty) return;
    _timer = Timer(delay, () => onQuery(query));
  }

  void dispose() {
    _disposed = true;
    _timer?.cancel();
  }
}

class SearchView extends StatefulWidget {
  const SearchView({
    super.key,
    required this.api,
    required this.sessions,
    required this.onSelectSession,
  });

  final AssistantApi api;
  final List<Map<String, dynamic>> sessions;
  final ValueChanged<String> onSelectSession;

  @override
  State<SearchView> createState() => _SearchViewState();
}

class _SearchViewState extends State<SearchView> {
  final _controller = TextEditingController();
  late final SearchDebouncer _debouncer = SearchDebouncer(_search);
  String _query = '';
  SearchResults? _results;
  String? _error;
  bool _loading = false;
  int _requestVersion = 0;

  @override
  void dispose() {
    _requestVersion++;
    _debouncer.dispose();
    _controller.dispose();
    super.dispose();
  }

  void _changeQuery(String value) {
    final query = value.trim();
    // Invalidate an in-flight response immediately, before the next timer.
    _requestVersion++;
    setState(() {
      _query = query;
      _results = null;
      _error = null;
      _loading = query.isNotEmpty;
    });
    _debouncer.schedule(query);
  }

  Future<void> _search(String query) async {
    final version = _requestVersion;
    try {
      final results = await widget.api.search(query);
      if (!mounted || version != _requestVersion || query != _query) return;
      setState(() {
        _results = results;
        _loading = false;
      });
    } catch (error) {
      if (!mounted || version != _requestVersion || query != _query) return;
      setState(() {
        _loading = false;
        _error = error is AssistantApiException ? error.message : '搜索暂时不可用，请重试';
      });
    }
  }

  void _openSession(String id) {
    HapticFeedback.selectionClick();
    FocusScope.of(context).unfocus();
    Navigator.of(context).pop();
    widget.onSelectSession(id);
  }

  @override
  Widget build(BuildContext context) {
    final colors = context.muse;
    return Scaffold(
      backgroundColor: colors.bg,
      body: GestureDetector(
        onTap: () => FocusScope.of(context).unfocus(),
        behavior: HitTestBehavior.translucent,
        child: SafeArea(
          child: Column(
            children: [
              Padding(
                padding: const EdgeInsets.fromLTRB(16, 12, 16, 12),
                child: Row(
                  children: [
                    GlassSurface(
                      shape: GlassShape.circle,
                      child: IconButton(
                        tooltip: '返回',
                        constraints: const BoxConstraints.tightFor(
                          width: 48,
                          height: 48,
                        ),
                        icon: const Icon(Icons.arrow_back_rounded),
                        onPressed: () => Navigator.of(context).pop(),
                      ),
                    ),
                    const SizedBox(width: 12),
                    Expanded(
                      child: GlassSurface(
                        shape: GlassShape.capsule,
                        padding: const EdgeInsets.only(left: 16, right: 4),
                        child: SizedBox(
                          height: 52,
                          child: Row(
                            children: [
                              Icon(
                                Icons.search_rounded,
                                size: 20,
                                color: colors.muted,
                              ),
                              const SizedBox(width: 8),
                              Expanded(
                                child: TextField(
                                  key: const ValueKey('search-input'),
                                  controller: _controller,
                                  autofocus: true,
                                  maxLength: 100,
                                  textInputAction: TextInputAction.search,
                                  onChanged: _changeQuery,
                                  decoration: const InputDecoration(
                                    hintText: '搜索',
                                    counterText: '',
                                    border: InputBorder.none,
                                    isDense: true,
                                  ),
                                ),
                              ),
                              if (_controller.text.isNotEmpty)
                                IconButton(
                                  tooltip: '清除搜索',
                                  constraints: const BoxConstraints.tightFor(
                                    width: 44,
                                    height: 44,
                                  ),
                                  onPressed: () {
                                    _controller.clear();
                                    _changeQuery('');
                                  },
                                  icon: const Icon(
                                    Icons.close_rounded,
                                    size: 20,
                                  ),
                                ),
                            ],
                          ),
                        ),
                      ),
                    ),
                  ],
                ),
              ),
              Expanded(child: _body(context)),
            ],
          ),
        ),
      ),
    );
  }

  Widget _body(BuildContext context) {
    if (_query.isEmpty) {
      return _list(
        context,
        sections: [
          _heading(context, '最近访问'),
          if (widget.sessions.isEmpty) _empty(context, '还没有对话'),
          for (final session in widget.sessions) _sessionRow(context, session),
        ],
      );
    }
    if (_loading) {
      return const Center(child: CircularProgressIndicator(strokeWidth: 2));
    }
    if (_error != null) {
      return Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Text(_error!, textAlign: TextAlign.center),
              const SizedBox(height: 12),
              TextButton(
                onPressed: () {
                  _requestVersion++;
                  setState(() {
                    _loading = true;
                    _error = null;
                  });
                  _search(_query);
                },
                child: const Text('重试'),
              ),
            ],
          ),
        ),
      );
    }
    final results = _results;
    return _list(
      context,
      sections: [
        _heading(context, '对话'),
        if (results == null || results.sessions.isEmpty)
          _empty(context, '没有匹配的对话'),
        if (results != null)
          for (final session in results.sessions) _sessionRow(context, session),
        const SizedBox(height: 18),
        _heading(context, '消息'),
        if (results == null || results.messages.isEmpty)
          _empty(context, '没有匹配的消息'),
        if (results != null)
          for (final message in results.messages) _messageRow(context, message),
      ],
    );
  }

  Widget _list(BuildContext context, {required List<Widget> sections}) {
    return ListView(
      keyboardDismissBehavior: ScrollViewKeyboardDismissBehavior.onDrag,
      padding: const EdgeInsets.fromLTRB(16, 8, 16, 24),
      children: sections,
    );
  }

  Widget _heading(BuildContext context, String text) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(8, 8, 8, 12),
      child: Text(
        text,
        style: TextStyle(
          color: context.muse.muted,
          fontSize: 13,
          fontWeight: FontWeight.w600,
        ),
      ),
    );
  }

  Widget _empty(BuildContext context, String text) => Padding(
    padding: const EdgeInsets.all(16),
    child: Text(text, style: TextStyle(color: context.muse.muted)),
  );

  Widget _sessionRow(BuildContext context, Map<String, dynamic> session) {
    final id = session['id'];
    final timestamp = DateTime.tryParse(
      '${session['last_message_at'] ?? session['updated_at'] ?? ''}',
    );
    return _resultRow(
      context,
      id: id is String ? id : null,
      icon: Icons.chat_bubble_outline_rounded,
      title: '${session['title'] ?? context.brand.name}',
      subtitle: '${session['last_message_preview'] ?? ''}',
      trailing: timestamp == null ? null : relativeTime(timestamp),
    );
  }

  Widget _messageRow(BuildContext context, Map<String, dynamic> message) {
    final id = message['session_id'];
    return _resultRow(
      context,
      id: id is String ? id : null,
      icon: Icons.notes_rounded,
      title: '${message['session_title'] ?? context.brand.name}',
      subtitle: '${message['snippet'] ?? ''}',
    );
  }

  Widget _resultRow(
    BuildContext context, {
    required String? id,
    required IconData icon,
    required String title,
    required String subtitle,
    String? trailing,
  }) {
    final colors = context.muse;
    return Padding(
      padding: const EdgeInsets.only(bottom: 8),
      child: Material(
        color: colors.bubble,
        borderRadius: BorderRadius.circular(20),
        child: InkWell(
          borderRadius: BorderRadius.circular(20),
          onTap: id == null || id.isEmpty ? null : () => _openSession(id),
          child: Padding(
            padding: const EdgeInsets.all(16),
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Icon(icon, size: 20, color: colors.muted),
                const SizedBox(width: 12),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      _highlight(context, title, maxLines: 1),
                      if (subtitle.isNotEmpty) ...[
                        const SizedBox(height: 6),
                        _highlight(context, subtitle, muted: true, maxLines: 3),
                      ],
                    ],
                  ),
                ),
                if (trailing != null) ...[
                  const SizedBox(width: 8),
                  Text(
                    trailing,
                    style: TextStyle(color: colors.muted, fontSize: 11),
                  ),
                ],
              ],
            ),
          ),
        ),
      ),
    );
  }

  Widget _highlight(
    BuildContext context,
    String text, {
    bool muted = false,
    required int maxLines,
  }) {
    final colors = context.muse;
    final spans = <TextSpan>[];
    var start = 0;
    if (_query.isNotEmpty) {
      final matches = RegExp(
        RegExp.escape(_query),
        caseSensitive: false,
      ).allMatches(text);
      for (final match in matches) {
        if (match.start > start) {
          spans.add(TextSpan(text: text.substring(start, match.start)));
        }
        spans.add(
          TextSpan(
            text: text.substring(match.start, match.end),
            style: TextStyle(
              color: colors.accent,
              fontWeight: FontWeight.w600,
              backgroundColor: colors.accent.withValues(alpha: 0.14),
            ),
          ),
        );
        start = match.end;
      }
    }
    if (start < text.length) spans.add(TextSpan(text: text.substring(start)));
    return Text.rich(
      TextSpan(
        style: TextStyle(
          color: muted ? colors.muted : colors.text,
          fontSize: muted ? 13 : 15,
          height: 1.4,
        ),
        children: spans,
      ),
      maxLines: maxLines,
      overflow: TextOverflow.ellipsis,
    );
  }
}
