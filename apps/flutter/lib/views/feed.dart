import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../api.dart';
import '../glass.dart';
import '../markdown.dart';
import '../theme.dart';
import 'content_helpers.dart';

String? feedSourceUrl(dynamic value) {
  if (value is! String) return null;
  final uri = Uri.tryParse(value.trim());
  if (uri == null ||
      !['http', 'https'].contains(uri.scheme.toLowerCase()) ||
      !uri.hasAuthority ||
      uri.host.isEmpty ||
      uri.userInfo.isNotEmpty) {
    return null;
  }
  return uri.toString();
}

class FeedView extends StatefulWidget {
  const FeedView({
    super.key,
    required this.api,
    required this.onDiscuss,
    this.contentPadding,
    this.showHeader = true,
  });

  final AssistantApi api;
  final Future<void> Function(String sessionId) onDiscuss;
  final EdgeInsetsGeometry? contentPadding;
  final bool showHeader;

  @override
  State<FeedView> createState() => _FeedViewState();
}

class _FeedViewState extends State<FeedView> {
  final _scroll = ScrollController();
  final _discussing = <String>{};
  final _feedbackPending = <String>{};
  List<Map<String, dynamic>> _posts = [];
  String? _cursor;
  String? _error;
  var _request = 0;
  var _loading = true;
  var _loadingMore = false;
  var _reloading = false;
  var _refreshing = false;
  var _editingInstructions = false;
  var _moreError = false;

  @override
  void initState() {
    super.initState();
    _scroll.addListener(_onScroll);
    _reload();
  }

  @override
  void didUpdateWidget(covariant FeedView oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.api != widget.api) _reload();
  }

  @override
  void dispose() {
    _request++;
    _scroll.dispose();
    super.dispose();
  }

  void _onScroll() {
    if (_scroll.hasClients && _scroll.position.extentAfter < 240) _loadMore();
  }

  String? _nextCursor(dynamic value) =>
      value is String && value.isNotEmpty ? value : null;

  Future<void> _reload() async {
    final request = ++_request;
    setState(() {
      _reloading = true;
      _loadingMore = false;
    });
    try {
      final payload = await widget.api.feed();
      if (!mounted || request != _request) return;
      setState(() {
        _posts = contentMaps(payload['items']);
        _cursor = _nextCursor(payload['next_cursor']);
        _error = null;
        _moreError = false;
      });
    } catch (_) {
      if (mounted && request == _request) {
        setState(() {
          _error = '动态暂时不可用，下拉重试';
          _moreError = false;
        });
      }
    } finally {
      if (mounted && request == _request) {
        setState(() {
          _loading = false;
          _reloading = false;
        });
      }
    }
  }

  Future<void> _loadMore() async {
    if (_loadingMore || _reloading || _cursor == null) return;
    final cursor = _cursor;
    final request = ++_request;
    setState(() => _loadingMore = true);
    try {
      final payload = await widget.api.feed(cursor: cursor);
      if (!mounted || request != _request) return;
      final ids = _posts.map((post) => post['id']).toSet();
      setState(() {
        _posts.addAll(
          contentMaps(payload['items']).where((post) => ids.add(post['id'])),
        );
        final next = _nextCursor(payload['next_cursor']);
        _cursor = next == cursor ? null : next;
        _error = null;
        _moreError = false;
      });
    } catch (_) {
      if (mounted && request == _request) {
        setState(() {
          _error = '更多动态暂时不可用，点击重试';
          _moreError = true;
        });
      }
    } finally {
      if (mounted && request == _request) setState(() => _loadingMore = false);
    }
  }

  void _notice(String text) =>
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(text)));

  Future<void> _refreshFeed() async {
    if (_refreshing) return;
    setState(() => _refreshing = true);
    try {
      await widget.api.refreshFeed();
      if (!mounted) return;
      _notice('正在整理新动态，稍后下拉刷新查看');
      await _reload();
    } catch (error) {
      if (mounted) {
        _notice(
          error is AssistantApiException && error.statusCode == 429
              ? '今天的刷新次数已用完，请明天再试'
              : '暂时无法刷新动态，请稍后重试',
        );
      }
    } finally {
      if (mounted) setState(() => _refreshing = false);
    }
  }

  Future<void> _editInstructions() async {
    if (_editingInstructions) return;
    setState(() => _editingInstructions = true);
    try {
      final prefs = await widget.api.proactivePrefs();
      if (!mounted) return;
      if (prefs.isEmpty) {
        _notice('当前服务暂不支持动态说明');
        return;
      }
      await showModalBottomSheet<void>(
        context: context,
        isScrollControlled: true,
        useSafeArea: true,
        backgroundColor: Colors.transparent,
        builder: (_) => _FeedInstructions(
          instructions: '${prefs['feed_instructions'] ?? ''}',
          onSave: (text) async {
            final updated = await widget.api.updateProactivePrefs({
              ...prefs,
              'feed_instructions': text,
            });
            if (updated.isEmpty) {
              throw const AssistantApiException(
                '当前服务暂不支持保存动态说明',
                statusCode: 404,
              );
            }
          },
        ),
      );
    } catch (_) {
      if (mounted) _notice('动态说明暂时不可用，请稍后重试');
    } finally {
      if (mounted) setState(() => _editingInstructions = false);
    }
  }

  Future<void> _feedback(Map<String, dynamic> post, String action) async {
    final id = '${post['id']}';
    if (_feedbackPending.contains(id)) return;
    setState(() => _feedbackPending.add(id));
    try {
      await widget.api.feedFeedback(id, action);
      if (!mounted) return;
      setState(() {
        if (action == 'not_interested') {
          _posts.removeWhere((item) => item['id'] == post['id']);
        } else {
          _posts = _posts
              .map(
                (item) => item['id'] == post['id']
                    ? {...item, 'liked': action == 'like'}
                    : item,
              )
              .toList();
        }
      });
    } catch (_) {
      if (mounted) _notice('反馈未保存，请重试');
    } finally {
      if (mounted) setState(() => _feedbackPending.remove(id));
    }
  }

  Future<void> _discuss(Map<String, dynamic> post) async {
    final id = '${post['id']}';
    if (_discussing.contains(id)) return;
    setState(() => _discussing.add(id));
    try {
      final result = await widget.api.discussFeedPost(id);
      final sessionId = result['session_id'];
      if (sessionId is! String || sessionId.isEmpty) {
        throw const AssistantApiException('当前服务暂不支持讨论动态');
      }
      if (!mounted) return;
      await widget.onDiscuss(sessionId);
    } catch (_) {
      if (mounted) _notice('暂时无法打开讨论，请重试');
    } finally {
      if (mounted) setState(() => _discussing.remove(id));
    }
  }

  Future<void> _menu(Map<String, dynamic> post, String action) async {
    if (action == 'why') {
      await showDialog<void>(
        context: context,
        builder: (context) => AlertDialog(
          title: const Text('为什么推给我'),
          content: Text('${post['reason'] ?? '围绕你关心的话题，为你整理了这篇动态。'}'),
          actions: [
            TextButton(
              onPressed: () => Navigator.of(context).pop(),
              child: const Text('知道了'),
            ),
          ],
        ),
      );
    } else if (action == 'not_interested') {
      await _feedback(post, action);
    } else if (action == 'delete') {
      final confirmed = await showDialog<bool>(
        context: context,
        builder: (context) => AlertDialog(
          title: const Text('删除动态？'),
          content: const Text('删除后，这篇动态将不再出现在列表中。'),
          actions: [
            TextButton(
              onPressed: () => Navigator.of(context).pop(false),
              child: const Text('取消'),
            ),
            TextButton(
              onPressed: () => Navigator.of(context).pop(true),
              child: const Text('删除'),
            ),
          ],
        ),
      );
      if (confirmed != true || !mounted) return;
      try {
        await widget.api.deleteFeedPost('${post['id']}');
        if (mounted) {
          setState(
            () => _posts.removeWhere((item) => item['id'] == post['id']),
          );
        }
      } catch (_) {
        if (mounted) _notice('删除未完成，请重试');
      }
    }
  }

  Future<void> _copySource(String url) async {
    await Clipboard.setData(ClipboardData(text: url));
    if (mounted) _notice('链接已复制');
  }

  @override
  Widget build(BuildContext context) => LayoutBuilder(
    builder: (context, constraints) {
      final narrow = constraints.maxWidth < MuseMetrics.mobileBreakpoint;
      return RefreshIndicator(
        onRefresh: _reload,
        child: ListView(
          key: const ValueKey('feed-list'),
          controller: _scroll,
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
                      children: [
                        Expanded(
                          child: widget.showHeader
                              ? const Text(
                                  '动态',
                                  style: TextStyle(
                                    fontSize: 24,
                                    fontWeight: FontWeight.w600,
                                  ),
                                )
                              : const SizedBox.shrink(),
                        ),
                        IconButton(
                          key: const ValueKey('feed-instructions'),
                          tooltip: '动态说明',
                          onPressed: _editingInstructions
                              ? null
                              : _editInstructions,
                          icon: const Icon(Icons.tune_rounded),
                        ),
                        IconButton(
                          key: const ValueKey('feed-refresh'),
                          tooltip: '刷新',
                          onPressed: _refreshing ? null : _refreshFeed,
                          icon: const Icon(Icons.refresh_rounded),
                        ),
                      ],
                    ),
                    if (widget.showHeader) ...[
                      const SizedBox(height: 8),
                      Text(
                        '围绕你关心的话题，发现值得了解的新鲜事。',
                        style: TextStyle(
                          color: context.muse.muted,
                          fontSize: 14,
                          height: 1.5,
                        ),
                      ),
                    ],
                    const SizedBox(height: 24),
                    if (_loading)
                      const Center(child: CircularProgressIndicator()),
                    if (!_loading && _posts.isEmpty && _error == null)
                      Padding(
                        padding: const EdgeInsets.symmetric(vertical: 40),
                        child: Column(
                          children: [
                            Icon(
                              Icons.auto_awesome_outlined,
                              size: 36,
                              color: context.muse.faint,
                            ),
                            const SizedBox(height: 12),
                            const Text('还没有动态'),
                            const SizedBox(height: 8),
                            Text(
                              'Luma 会围绕你关心的话题整理资讯。',
                              style: TextStyle(color: context.muse.muted),
                            ),
                          ],
                        ),
                      ),
                    for (final post in _posts) _postCard(post),
                    if (_error != null)
                      TextButton(
                        onPressed: _moreError ? _loadMore : _reload,
                        child: Text(_error!),
                      ),
                    if (_loadingMore)
                      const Padding(
                        padding: EdgeInsets.all(16),
                        child: Center(child: CircularProgressIndicator()),
                      )
                    else if (_cursor != null)
                      TextButton(
                        key: const ValueKey('feed-load-more'),
                        onPressed: _reloading ? null : _loadMore,
                        child: const Text('加载更多'),
                      ),
                  ],
                ),
              ),
            ),
          ],
        ),
      );
    },
  );

  Widget _postCard(Map<String, dynamic> post) {
    final id = '${post['id']}';
    final sources = contentMaps(post['sources'])
        .where((source) => feedSourceUrl(source['url']) != null)
        .toList();
    final liked = post['liked'] == true;
    return Container(
      key: ValueKey('feed-post-$id'),
      margin: const EdgeInsets.only(bottom: 16),
      padding: const EdgeInsets.all(20),
      decoration: BoxDecoration(
        color: context.muse.rail,
        border: Border.all(color: context.muse.line),
        borderRadius: BorderRadius.circular(MuseMetrics.cardRadius),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Icon(
                contentIcon(post['icon']),
                size: 24,
                color: context.muse.text,
              ),
              const SizedBox(width: 12),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      '${post['title'] ?? ''}',
                      style: const TextStyle(
                        fontSize: 16,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                    const SizedBox(height: 5),
                    Text(
                      contentRelativeTime(post['created_at']),
                      style: TextStyle(color: context.muse.muted, fontSize: 12),
                    ),
                  ],
                ),
              ),
              PopupMenuButton<String>(
                tooltip: '动态选项',
                icon: const Icon(Icons.more_horiz_rounded),
                onSelected: (action) => _menu(post, action),
                itemBuilder: (_) => const [
                  PopupMenuItem(value: 'why', child: Text('为什么推给我')),
                  PopupMenuItem(value: 'not_interested', child: Text('不感兴趣')),
                  PopupMenuItem(value: 'delete', child: Text('删除')),
                ],
              ),
            ],
          ),
          const SizedBox(height: 16),
          LumaMarkdown(
            data: '${post['body_markdown'] ?? ''}',
            style: TextStyle(
              color: context.muse.text,
              fontSize: 14,
              height: 1.6,
            ),
            linkColor: context.muse.link,
            selectable: true,
          ),
          if (sources.isNotEmpty) ...[
            const SizedBox(height: 12),
            Wrap(
              spacing: 8,
              runSpacing: 4,
              children: [
                for (var index = 0; index < sources.length; index++)
                  TextButton.icon(
                    key: ValueKey('feed-source-$id-$index'),
                    onPressed: () =>
                        _copySource(feedSourceUrl(sources[index]['url'])!),
                    icon: const Icon(Icons.link_rounded, size: 16),
                    label: Text('${sources[index]['title'] ?? '来源'}'),
                  ),
              ],
            ),
          ],
          const SizedBox(height: 8),
          Row(
            children: [
              IconButton(
                key: ValueKey('feed-like-$id'),
                tooltip: liked ? '取消喜欢' : '喜欢',
                onPressed: _feedbackPending.contains(id)
                    ? null
                    : () => _feedback(post, liked ? 'unlike' : 'like'),
                icon: Icon(
                  liked
                      ? Icons.favorite_rounded
                      : Icons.favorite_border_rounded,
                  color: liked ? context.muse.danger : context.muse.muted,
                ),
              ),
              TextButton.icon(
                key: ValueKey('feed-discuss-$id'),
                onPressed: _discussing.contains(id)
                    ? null
                    : () => _discuss(post),
                icon: const Icon(Icons.chat_bubble_outline_rounded, size: 18),
                label: Text(_discussing.contains(id) ? '正在打开…' : '讨论'),
              ),
            ],
          ),
        ],
      ),
    );
  }
}

class _FeedInstructions extends StatefulWidget {
  const _FeedInstructions({required this.instructions, required this.onSave});

  final String instructions;
  final Future<void> Function(String text) onSave;

  @override
  State<_FeedInstructions> createState() => _FeedInstructionsState();
}

class _FeedInstructionsState extends State<_FeedInstructions> {
  late final TextEditingController _text;
  var _saving = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    _text = TextEditingController(text: widget.instructions);
  }

  @override
  void dispose() {
    _text.dispose();
    super.dispose();
  }

  Future<void> _save() async {
    if (_saving) return;
    setState(() {
      _saving = true;
      _error = null;
    });
    try {
      await widget.onSave(_text.text.trim());
      if (mounted) Navigator.of(context).pop();
    } catch (error) {
      if (mounted) {
        setState(
          () =>
              _error = error is AssistantApiException && error.statusCode == 404
              ? '当前服务暂不支持保存动态说明'
              : '保存未完成，请重试',
        );
      }
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  @override
  Widget build(BuildContext context) => Padding(
    padding: EdgeInsets.only(bottom: MediaQuery.viewInsetsOf(context).bottom),
    child: GlassSurface(
      padding: const EdgeInsets.all(24),
      child: SafeArea(
        top: false,
        child: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              const Text(
                '动态说明',
                style: TextStyle(fontSize: 20, fontWeight: FontWeight.w600),
              ),
              const SizedBox(height: 10),
              Text(
                '告诉 Luma 你希望看到哪些资讯，以及整理方式。',
                style: TextStyle(color: context.muse.muted),
              ),
              const SizedBox(height: 16),
              TextField(
                key: const ValueKey('feed-instructions-text'),
                controller: _text,
                enabled: !_saving,
                minLines: 4,
                maxLines: 8,
                maxLength: 2000,
                decoration: const InputDecoration(
                  hintText: '例如：关注 AI 工具和产品设计，给我简洁的要点和来源。',
                  border: OutlineInputBorder(),
                ),
              ),
              if (_error != null)
                Text(_error!, style: TextStyle(color: context.muse.danger)),
              const SizedBox(height: 12),
              FilledButton(
                key: const ValueKey('feed-instructions-save'),
                onPressed: _saving ? null : _save,
                child: Text(_saving ? '正在保存…' : '保存'),
              ),
            ],
          ),
        ),
      ),
    ),
  );
}
