import 'dart:async';

import 'package:flutter/material.dart';

import '../api.dart';
import '../glass.dart';
import '../markdown.dart';
import '../theme.dart';
import 'content_helpers.dart';

class IdeasView extends StatefulWidget {
  const IdeasView({
    super.key,
    required this.api,
    required this.onStart,
    required this.onSelectSession,
    this.contentPadding,
    this.showHeader = true,
  });

  final AssistantApi api;
  final Future<void> Function(String sessionId, String prompt) onStart;
  final ValueChanged<String> onSelectSession;
  final EdgeInsetsGeometry? contentPadding;
  final bool showHeader;

  @override
  State<IdeasView> createState() => _IdeasViewState();
}

class _IdeasViewState extends State<IdeasView> {
  List<Map<String, dynamic>> _featured = [];
  List<Map<String, dynamic>> _groups = [];
  Timer? _poll;
  var _pollCount = 0;
  var _request = 0;
  var _loading = true;
  var _refreshing = false;
  var _generating = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void didUpdateWidget(covariant IdeasView oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.api != widget.api) _load();
  }

  @override
  void dispose() {
    _poll?.cancel();
    _request++;
    super.dispose();
  }

  Future<void> _load({bool resetPoll = true}) async {
    _poll?.cancel();
    if (resetPoll) _pollCount = 0;
    final request = ++_request;
    try {
      final payload = await widget.api.ideas();
      if (!mounted || request != _request) return;
      setState(() {
        _featured = contentMaps(payload['featured']);
        _groups = contentMaps(payload['groups']);
        _generating = payload['generating'] == true;
        _loading = false;
        _error = null;
      });
      if (_generating && _pollCount < 6) {
        _poll = Timer(const Duration(seconds: 5), () {
          _pollCount++;
          _load(resetPoll: false);
        });
      }
    } catch (_) {
      if (!mounted || request != _request) return;
      setState(() {
        _loading = false;
        _generating = false;
        _error = '点子暂时不可用，下拉重试';
      });
    }
  }

  Future<void> _refreshIdeas() async {
    if (_refreshing) return;
    _poll?.cancel();
    _request++;
    setState(() => _refreshing = true);
    try {
      await widget.api.refreshIdeas();
      if (!mounted) return;
      await _load();
    } catch (error) {
      if (mounted) {
        _notice(
          error is AssistantApiException && error.statusCode == 429
              ? '今天的换一批次数已用完，请明天再试'
              : '暂时无法换一批，请稍后重试',
        );
      }
    } finally {
      if (mounted) setState(() => _refreshing = false);
    }
  }

  void _notice(String text) =>
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(text)));

  Future<void> _feedback(Map<String, dynamic> idea, String action) async {
    await widget.api.ideaFeedback('${idea['id']}', action);
    if (!mounted) return;
    if (action == 'not_interested') {
      setState(() {
        _featured.removeWhere((item) => item['id'] == idea['id']);
        _groups = _groups
            .map(
              (group) => {
                ...group,
                'items': contentMaps(group['items'])
                    .where((item) => item['id'] != idea['id'])
                    .toList(),
              },
            )
            .toList();
      });
    }
    _notice(action == 'more_like' ? '已记下，会为你推荐更多类似点子' : '已减少这类推荐');
  }

  Future<void> _rowFeedback(Map<String, dynamic> idea, String action) async {
    try {
      await _feedback(idea, action);
    } catch (_) {
      if (mounted) _notice('反馈未保存，请重试');
    }
  }

  Future<void> _showIdea(Map<String, dynamic> idea) =>
      showModalBottomSheet<void>(
        context: context,
        isScrollControlled: true,
        useSafeArea: true,
        backgroundColor: Colors.transparent,
        builder: (sheetContext) => FractionallySizedBox(
          heightFactor: .88,
          child: _IdeaDetails(
            idea: idea,
            onFeedback: (action) => _feedback(idea, action),
            onSource: (id) {
              Navigator.of(sheetContext).pop();
              widget.onSelectSession(id);
            },
            onStart: () async {
              final result = await widget.api.startIdea('${idea['id']}');
              final sessionId = result['session_id'];
              final prompt = result['prompt'];
              if (sessionId is! String ||
                  sessionId.isEmpty ||
                  prompt is! String ||
                  prompt.trim().isEmpty) {
                throw const AssistantApiException('当前服务暂不支持启动点子');
              }
              if (!mounted || !sheetContext.mounted) return;
              Navigator.of(sheetContext).pop();
              try {
                await widget.onStart(sessionId, prompt);
              } catch (_) {
                if (mounted) _notice('点子已创建，发送失败，请在会话中重试');
              }
            },
          ),
        ),
      );

  @override
  Widget build(BuildContext context) => LayoutBuilder(
    builder: (context, constraints) {
      final narrow = constraints.maxWidth < MuseMetrics.mobileBreakpoint;
      return RefreshIndicator(
        onRefresh: _load,
        child: ListView(
          physics: const AlwaysScrollableScrollPhysics(),
          padding:
              widget.contentPadding ??
              EdgeInsets.fromLTRB(
                narrow ? 16 : 24,
                narrow ? 24 : 72,
                narrow ? 16 : 24,
                24,
              ),
          children: [
            Center(
              child: ConstrainedBox(
                constraints: const BoxConstraints(
                  maxWidth: MuseMetrics.readingColumn,
                ),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    Row(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Expanded(
                          child: widget.showHeader
                              ? Column(
                                  crossAxisAlignment: CrossAxisAlignment.start,
                                  children: [
                                    const Text(
                                      '点子',
                                      style: TextStyle(
                                        fontSize: 24,
                                        fontWeight: FontWeight.w600,
                                      ),
                                    ),
                                    const SizedBox(height: 8),
                                    Text(
                                      '从你的对话中发现灵感，一起把它变成成果。',
                                      style: TextStyle(
                                        color: context.muse.muted,
                                        fontSize: 14,
                                        height: 1.5,
                                      ),
                                    ),
                                  ],
                                )
                              : const SizedBox.shrink(),
                        ),
                        TextButton.icon(
                          key: const ValueKey('ideas-refresh'),
                          onPressed: _refreshing ? null : _refreshIdeas,
                          icon: const Icon(Icons.refresh_rounded, size: 18),
                          label: Text(_refreshing ? '正在换一批' : '换一批'),
                        ),
                      ],
                    ),
                    const SizedBox(height: 24),
                    if (_generating)
                      Padding(
                        padding: const EdgeInsets.only(bottom: 16),
                        child: Text(
                          _pollCount >= 6 ? '还在生成点子，稍后下拉刷新查看。' : '正在为你寻找新点子…',
                          key: const ValueKey('ideas-generating'),
                          style: TextStyle(color: context.muse.muted),
                        ),
                      ),
                    if (_error != null)
                      Text(
                        _error!,
                        style: TextStyle(color: context.muse.muted),
                      ),
                    if (_loading)
                      const Center(child: CircularProgressIndicator()),
                    if (!_loading &&
                        _featured.isEmpty &&
                        _groups.isEmpty &&
                        _error == null)
                      Padding(
                        padding: const EdgeInsets.symmetric(vertical: 40),
                        child: Column(
                          children: [
                            Icon(
                              Icons.lightbulb_outline_rounded,
                              size: 36,
                              color: context.muse.faint,
                            ),
                            const SizedBox(height: 12),
                            const Text('还没有点子'),
                            const SizedBox(height: 8),
                            Text(
                              '和 Luma 聊聊你想做的事，灵感会在这里出现。',
                              style: TextStyle(color: context.muse.muted),
                            ),
                          ],
                        ),
                      ),
                    if (_featured.isNotEmpty) ...[
                      _sectionTitle('为你推荐'),
                      for (final idea in _featured) _ideaRow(idea),
                    ],
                    for (final group in _groups)
                      if (contentMaps(group['items']).isNotEmpty) ...[
                        _sectionTitle('${group['name'] ?? '更多点子'}'),
                        for (final idea in contentMaps(group['items']))
                          _ideaRow(idea),
                      ],
                  ],
                ),
              ),
            ),
          ],
        ),
      );
    },
  );

  Widget _sectionTitle(String title) => Padding(
    padding: const EdgeInsets.only(top: 12, bottom: 10),
    child: Text(
      title,
      style: TextStyle(
        color: context.muse.muted,
        fontSize: 13,
        fontWeight: FontWeight.w600,
      ),
    ),
  );

  Widget _ideaRow(Map<String, dynamic> idea) => Material(
    type: MaterialType.transparency,
    child: InkWell(
      key: ValueKey('idea-${idea['id']}'),
      borderRadius: BorderRadius.circular(12),
      onTap: () => _showIdea(idea),
      child: Container(
        decoration: BoxDecoration(
          border: Border(bottom: BorderSide(color: context.muse.line)),
        ),
        padding: const EdgeInsets.symmetric(vertical: 16),
        child: Row(
          children: [
            Container(
              width: 42,
              height: 42,
              decoration: BoxDecoration(
                color: context.muse.chip,
                borderRadius: BorderRadius.circular(12),
              ),
              child: Icon(
                contentIcon(idea['icon']),
                size: 22,
                color: context.muse.text,
              ),
            ),
            const SizedBox(width: 14),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    '${idea['title'] ?? ''}',
                    style: const TextStyle(
                      fontSize: 15,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                  const SizedBox(height: 5),
                  Text(
                    '${idea['summary'] ?? ''}',
                    style: TextStyle(
                      color: context.muse.muted,
                      fontSize: 13,
                      height: 1.5,
                    ),
                  ),
                ],
              ),
            ),
            _IdeaMenu(onSelected: (action) => _rowFeedback(idea, action)),
          ],
        ),
      ),
    ),
  );
}

class _IdeaMenu extends StatelessWidget {
  const _IdeaMenu({required this.onSelected, this.enabled = true});

  final ValueChanged<String> onSelected;
  final bool enabled;

  @override
  Widget build(BuildContext context) => PopupMenuButton<String>(
    tooltip: '点子选项',
    enabled: enabled,
    icon: const Icon(Icons.more_horiz_rounded),
    onSelected: onSelected,
    itemBuilder: (_) => const [
      PopupMenuItem(value: 'more_like', child: Text('更多类似')),
      PopupMenuItem(value: 'not_interested', child: Text('不感兴趣')),
    ],
  );
}

class _IdeaDetails extends StatefulWidget {
  const _IdeaDetails({
    required this.idea,
    required this.onStart,
    required this.onSource,
    required this.onFeedback,
  });

  final Map<String, dynamic> idea;
  final Future<void> Function() onStart;
  final ValueChanged<String> onSource;
  final Future<void> Function(String action) onFeedback;

  @override
  State<_IdeaDetails> createState() => _IdeaDetailsState();
}

class _IdeaDetailsState extends State<_IdeaDetails> {
  var _busy = false;
  String? _error;

  Future<void> _run(
    Future<void> Function() action, {
    bool close = false,
  }) async {
    if (_busy) return;
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await action();
      if (mounted && close) Navigator.of(context).pop();
    } catch (_) {
      if (mounted) setState(() => _error = '操作未完成，请稍后重试');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) => GlassSurface(
    padding: const EdgeInsets.fromLTRB(24, 20, 24, 24),
    child: SafeArea(
      top: false,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Center(
            child: Container(
              width: 36,
              height: 4,
              decoration: BoxDecoration(
                color: context.muse.faint,
                borderRadius: BorderRadius.circular(2),
              ),
            ),
          ),
          const SizedBox(height: 16),
          Row(
            children: [
              Expanded(
                child: Text(
                  '${widget.idea['title'] ?? ''}',
                  style: const TextStyle(
                    fontSize: 20,
                    fontWeight: FontWeight.w600,
                  ),
                ),
              ),
              _IdeaMenu(
                enabled: !_busy,
                onSelected: (action) =>
                    _run(() => widget.onFeedback(action), close: true),
              ),
            ],
          ),
          const SizedBox(height: 16),
          Expanded(
            child: SingleChildScrollView(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  LumaMarkdown(
                    data: '${widget.idea['plan_markdown'] ?? ''}',
                    style: TextStyle(
                      color: context.muse.text,
                      fontSize: 15,
                      height: 1.6,
                    ),
                    linkColor: context.muse.link,
                    selectable: true,
                  ),
                  if (contentMaps(widget.idea['sources']).isNotEmpty) ...[
                    const SizedBox(height: 24),
                    Text(
                      '灵感来自',
                      style: TextStyle(color: context.muse.muted, fontSize: 13),
                    ),
                    for (final source in contentMaps(widget.idea['sources']))
                      Material(
                        type: MaterialType.transparency,
                        child: ListTile(
                          contentPadding: EdgeInsets.zero,
                          leading: const Icon(
                            Icons.chat_bubble_outline_rounded,
                            size: 20,
                          ),
                          title: Text('${source['session_title'] ?? '对话'}'),
                          onTap:
                              _busy ||
                                  source['session_id'] is! String ||
                                  (source['session_id'] as String).isEmpty
                              ? null
                              : () => widget.onSource(
                                  source['session_id'] as String,
                                ),
                        ),
                      ),
                  ],
                ],
              ),
            ),
          ),
          if (_error != null)
            Padding(
              padding: const EdgeInsets.only(top: 12),
              child: Text(
                _error!,
                style: TextStyle(color: context.muse.danger),
              ),
            ),
          const SizedBox(height: 16),
          FilledButton.icon(
            key: const ValueKey('idea-start'),
            onPressed: _busy ? null : () => _run(widget.onStart),
            icon: const Icon(Icons.arrow_forward_rounded),
            label: Text(_busy ? '正在开始…' : '马上开始'),
          ),
        ],
      ),
    ),
  );
}
