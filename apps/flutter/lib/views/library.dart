import 'dart:async';
import 'dart:typed_data';

import 'package:flutter/material.dart';

import '../api.dart';
import '../glass.dart';
import '../markdown.dart';
import '../theme.dart';
import 'content_helpers.dart';
import 'file_thumbnail.dart';

const _types = <String, String>{
  'all': '全部',
  'document': '文档',
  'sheet': '表格',
  'web': '网页',
  'image': '图片',
  'code': '代码',
  'archive': '压缩包',
  'other': '其他',
};

IconData _typeIcon(String type) => switch (type) {
  'document' => Icons.description_outlined,
  'sheet' => Icons.table_chart_outlined,
  'web' => Icons.language_outlined,
  'image' => Icons.image_outlined,
  'code' => Icons.code_rounded,
  'archive' => Icons.folder_zip_outlined,
  _ => Icons.insert_drive_file_outlined,
};

class LibraryView extends StatefulWidget {
  const LibraryView({
    super.key,
    required this.api,
    required this.onFile,
    required this.onSelectSession,
    this.contentPadding,
    this.showHeader = true,
  });

  final AssistantApi api;
  final Future<void> Function(Map<String, dynamic>) onFile;
  final ValueChanged<String> onSelectSession;
  final EdgeInsetsGeometry? contentPadding;
  final bool showHeader;

  @override
  State<LibraryView> createState() => _LibraryViewState();
}

class _LibraryViewState extends State<LibraryView> {
  final _search = TextEditingController();
  final _scroll = ScrollController();
  Timer? _searchTimer;
  List<Map<String, dynamic>> _items = [];
  Map<String, dynamic> _counts = {};
  String _type = 'all';
  String _sort = 'recent';
  String _query = '';
  String? _nextCursor;
  String? _error;
  bool _loading = true;
  bool _loadingMore = false;
  int _request = 0;

  @override
  void initState() {
    super.initState();
    _scroll.addListener(_loadMoreIfNeeded);
    _load();
  }

  @override
  void didUpdateWidget(covariant LibraryView oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.api != widget.api) _load();
  }

  @override
  void dispose() {
    _request++;
    _searchTimer?.cancel();
    _search.dispose();
    _scroll.dispose();
    super.dispose();
  }

  void _loadMoreIfNeeded() {
    if (_scroll.position.extentAfter < 250 &&
        !_loading &&
        !_loadingMore &&
        _nextCursor != null &&
        _error == null) {
      _load(append: true);
    }
  }

  Future<void> _load({bool append = false}) async {
    _searchTimer?.cancel();
    final request = ++_request;
    final cursor = append ? _nextCursor : null;
    setState(() {
      if (append) {
        _loadingMore = true;
      } else {
        _loading = true;
        _loadingMore = false;
      }
      _error = null;
    });
    try {
      final result = await widget.api.library(
        type: _type,
        q: _query,
        sort: _sort,
        cursor: cursor,
      );
      if (!mounted || request != _request) return;
      final items = contentMaps(result['items']);
      setState(() {
        if (append) {
          final ids = _items.map((item) => item['id']).toSet();
          _items.addAll(items.where((item) => !ids.contains(item['id'])));
        } else {
          _items = items;
        }
        _counts = result['counts'] is Map
            ? Map<String, dynamic>.from(result['counts'] as Map)
            : {};
        final next = result['next_cursor'];
        _nextCursor = next is String && next.isNotEmpty ? next : null;
        _loading = false;
        _loadingMore = false;
      });
    } catch (error) {
      if (!mounted || request != _request) return;
      setState(() {
        _loading = false;
        _loadingMore = false;
        _error = error is AssistantApiException
            ? error.message
            : '资源库暂时不可用，请重试';
      });
    }
  }

  void _changeQuery(String value) {
    _searchTimer?.cancel();
    _request++;
    setState(() {
      _query = value.trim();
      _loading = true;
      _loadingMore = false;
      _error = null;
    });
    _searchTimer = Timer(const Duration(milliseconds: 300), _load);
  }

  Future<void> _open(Map<String, dynamic> item) async {
    await Navigator.of(context).push<void>(
      MaterialPageRoute(
        builder: (_) => LibraryPreviewView(
          api: widget.api,
          item: item,
          onFile: widget.onFile,
          onSelectSession: widget.onSelectSession,
        ),
      ),
    );
    if (mounted) await _load();
  }

  @override
  Widget build(BuildContext context) {
    final pinned = _items.where((item) => item['pinned'] == true).toList();
    final recent = _items.where((item) => item['pinned'] != true).toList();
    return LayoutBuilder(
      builder: (context, constraints) {
        final mobile = constraints.maxWidth < MuseMetrics.mobileBreakpoint;
        return RefreshIndicator(
          onRefresh: _load,
          child: ListView(
            controller: _scroll,
            physics: const AlwaysScrollableScrollPhysics(),
            keyboardDismissBehavior: ScrollViewKeyboardDismissBehavior.onDrag,
            padding:
                widget.contentPadding ??
                EdgeInsets.fromLTRB(
                  mobile ? 16 : 24,
                  mobile ? 24 : 72,
                  mobile ? 16 : 24,
                  24,
                ),
            children: [
              Center(
                child: ConstrainedBox(
                  constraints: const BoxConstraints(
                    maxWidth: MuseMetrics.readingColumn,
                  ),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      if (widget.showHeader) ...[
                        const Text(
                          '资源库',
                          style: TextStyle(
                            fontSize: 28,
                            fontWeight: FontWeight.w600,
                          ),
                        ),
                        const SizedBox(height: 8),
                        Text(
                          '你的文件和 Luma 产出，都在这里。',
                          style: TextStyle(color: context.muse.muted),
                        ),
                        const SizedBox(height: 24),
                      ],
                      GlassSurface(
                        shape: GlassShape.capsule,
                        child: TextField(
                          controller: _search,
                          onChanged: _changeQuery,
                          decoration: const InputDecoration(
                            hintText: '搜索资源',
                            prefixIcon: Icon(Icons.search_rounded),
                            border: InputBorder.none,
                            enabledBorder: InputBorder.none,
                            focusedBorder: InputBorder.none,
                            contentPadding: EdgeInsets.symmetric(
                              horizontal: 16,
                              vertical: 14,
                            ),
                          ),
                        ),
                      ),
                      const SizedBox(height: 16),
                      Row(
                        children: [
                          Expanded(
                            child: SingleChildScrollView(
                              scrollDirection: Axis.horizontal,
                              child: Row(
                                children: [
                                  for (final type in _types.entries)
                                    Padding(
                                      padding: const EdgeInsets.only(right: 8),
                                      child: ChoiceChip(
                                        key: ValueKey(
                                          'library-type-${type.key}',
                                        ),
                                        label: Text(
                                          '${type.value} ${_counts[type.key] ?? 0}',
                                        ),
                                        selected: _type == type.key,
                                        onSelected: (_) {
                                          setState(() => _type = type.key);
                                          _load();
                                        },
                                      ),
                                    ),
                                ],
                              ),
                            ),
                          ),
                          PopupMenuButton<String>(
                            tooltip: '排序',
                            initialValue: _sort,
                            icon: const Icon(Icons.sort_rounded),
                            onSelected: (value) {
                              setState(() => _sort = value);
                              _load();
                            },
                            itemBuilder: (_) => const [
                              PopupMenuItem(
                                value: 'recent',
                                child: Text('最近打开'),
                              ),
                              PopupMenuItem(
                                value: 'created',
                                child: Text('创建时间'),
                              ),
                              PopupMenuItem(value: 'title', child: Text('标题')),
                            ],
                          ),
                        ],
                      ),
                      const SizedBox(height: 20),
                      if (_loading)
                        const Center(
                          child: Padding(
                            padding: EdgeInsets.all(24),
                            child: CircularProgressIndicator(),
                          ),
                        )
                      else if (_error != null && _items.isEmpty)
                        _status(_error!)
                      else if (_items.isEmpty)
                        _status(
                          _query.isEmpty
                              ? '还没有资源，上传文件或让 Luma 帮你创作吧。'
                              : '没有找到匹配的资源',
                        )
                      else ...[
                        if (pinned.isNotEmpty) _section('置顶', pinned),
                        if (recent.isNotEmpty) _section('最近', recent),
                      ],
                      if (!_loading && _error != null && _items.isNotEmpty)
                        _status(_error!),
                      if (_loadingMore)
                        const Center(child: CircularProgressIndicator()),
                      if (!_loading && !_loadingMore && _nextCursor != null)
                        Center(
                          child: TextButton(
                            onPressed: () => _load(append: true),
                            child: const Text('加载更多'),
                          ),
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
  }

  Widget _status(String message) => Padding(
    padding: const EdgeInsets.symmetric(vertical: 36),
    child: Center(
      child: Column(
        children: [
          Text(
            message,
            textAlign: TextAlign.center,
            style: TextStyle(color: context.muse.muted),
          ),
          if (_error != null)
            TextButton(onPressed: _load, child: const Text('重试')),
        ],
      ),
    ),
  );

  Widget _section(String title, List<Map<String, dynamic>> items) => Padding(
    padding: const EdgeInsets.only(bottom: 24),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          title,
          style: const TextStyle(fontSize: 16, fontWeight: FontWeight.w600),
        ),
        const SizedBox(height: 12),
        GridView.builder(
          shrinkWrap: true,
          physics: const NeverScrollableScrollPhysics(),
          gridDelegate: const SliverGridDelegateWithFixedCrossAxisCount(
            crossAxisCount: 2,
            crossAxisSpacing: 12,
            mainAxisSpacing: 12,
            mainAxisExtent: 224,
          ),
          itemCount: items.length,
          itemBuilder: (_, index) => _card(items[index]),
        ),
      ],
    ),
  );

  Widget _card(Map<String, dynamic> item) {
    final type = '${item['type'] ?? 'other'}';
    final source = '${item['session_title'] ?? ''}'.trim();
    return Material(
      color: context.muse.rail,
      borderRadius: BorderRadius.circular(16),
      clipBehavior: Clip.antiAlias,
      child: InkWell(
        key: ValueKey('library-item-${item['id']}'),
        onTap: () => _open(item),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            SizedBox(
              height: 116,
              width: double.infinity,
              child: type == 'image'
                  ? FileThumbnail(
                      api: widget.api,
                      fileId: '${item['id']}',
                      filename: '${item['filename'] ?? ''}',
                      height: 116,
                      padding: EdgeInsets.zero,
                      fit: BoxFit.cover,
                    )
                  : Center(
                      child: Icon(
                        _typeIcon(type),
                        size: 40,
                        color: context.muse.muted,
                      ),
                    ),
            ),
            Padding(
              padding: const EdgeInsets.all(12),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    '${item['title'] ?? item['filename'] ?? '文件'}',
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(fontWeight: FontWeight.w600),
                  ),
                  const SizedBox(height: 6),
                  Text(
                    '${_types[type] ?? '其他'} · ${contentRelativeTime(item['last_opened_at'] ?? item['created_at'])}',
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(fontSize: 12, color: context.muse.muted),
                  ),
                  if (item['session_id'] != null && source.isNotEmpty) ...[
                    const SizedBox(height: 6),
                    Text(
                      '来自 $source',
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(fontSize: 12, color: context.muse.muted),
                    ),
                  ],
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class LibraryPreviewView extends StatefulWidget {
  const LibraryPreviewView({
    super.key,
    required this.api,
    required this.item,
    required this.onFile,
    required this.onSelectSession,
  });

  final AssistantApi api;
  final Map<String, dynamic> item;
  final Future<void> Function(Map<String, dynamic>) onFile;
  final ValueChanged<String> onSelectSession;

  @override
  State<LibraryPreviewView> createState() => _LibraryPreviewViewState();
}

class _LibraryPreviewViewState extends State<LibraryPreviewView> {
  late Map<String, dynamic> _item;
  late Future<Map<String, dynamic>> _preview;
  Future<Uint8List>? _image;
  bool _mutating = false;

  String get _id => '${_item['id']}';

  @override
  void initState() {
    super.initState();
    _item = Map<String, dynamic>.from(widget.item);
    _preview = widget.api.libraryPreview(_id);
  }

  Future<void> _action(String value) async {
    if (_mutating) return;
    if (value == 'session') {
      final id = '${_item['session_id'] ?? ''}';
      if (id.isNotEmpty) {
        Navigator.of(context).pop();
        widget.onSelectSession(id);
      }
      return;
    }
    String? title;
    if (value == 'rename') {
      title = await showDialog<String>(
        context: context,
        builder: (_) => _RenameResourceDialog(
          title: '${_item['title'] ?? _item['filename'] ?? ''}',
        ),
      );
      if (title == null || !mounted) return;
    }
    if (value == 'delete') {
      final confirm = await showDialog<bool>(
        context: context,
        builder: (context) => AlertDialog(
          title: const Text('删除资源？'),
          content: const Text('删除后将无法在资源库中访问此文件。'),
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
      if (confirm != true || !mounted) return;
    }
    if (!mounted) return;
    setState(() => _mutating = true);
    try {
      if (value == 'download') {
        await widget.onFile({..._item, 'file_id': _id});
      } else if (value == 'delete') {
        await widget.api.deleteFile(_id);
        if (mounted) Navigator.of(context).pop();
      } else {
        final updated = await widget.api.updateLibrary(
          _id,
          title: title,
          pinned: value == 'pin' ? _item['pinned'] != true : null,
        );
        if (mounted) setState(() => _item = {..._item, ...updated});
      }
    } catch (error) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            content: Text(
              error is AssistantApiException ? error.message : '操作失败，请重试',
            ),
          ),
        );
      }
    } finally {
      if (mounted) setState(() => _mutating = false);
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    backgroundColor: context.muse.bg,
    appBar: AppBar(
      title: Text('${_item['title'] ?? _item['filename'] ?? '文件预览'}'),
      actions: [
        PopupMenuButton<String>(
          tooltip: '资源操作',
          enabled: !_mutating,
          onSelected: _action,
          itemBuilder: (_) => [
            const PopupMenuItem(value: 'download', child: Text('下载或分享')),
            PopupMenuItem(
              value: 'pin',
              child: Text(_item['pinned'] == true ? '取消置顶' : '置顶'),
            ),
            const PopupMenuItem(value: 'rename', child: Text('重命名')),
            if ('${_item['session_id'] ?? ''}'.isNotEmpty)
              const PopupMenuItem(value: 'session', child: Text('跳到对话')),
            const PopupMenuItem(value: 'delete', child: Text('删除')),
          ],
        ),
      ],
    ),
    body: FutureBuilder<Map<String, dynamic>>(
      future: _preview,
      builder: (context, snapshot) {
        if (snapshot.hasError) {
          return Center(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                const Text('预览暂时不可用'),
                TextButton(
                  onPressed: () =>
                      setState(() => _preview = widget.api.libraryPreview(_id)),
                  child: const Text('重试'),
                ),
              ],
            ),
          );
        }
        final preview = snapshot.data;
        if (preview == null) {
          return const Center(child: CircularProgressIndicator());
        }
        final kind = '${preview['kind'] ?? 'none'}';
        if (kind == 'image') {
          _image ??= widget.api.downloadFileBytes(_id);
          return FutureBuilder<Uint8List>(
            future: _image,
            builder: (context, image) {
              if (image.hasError) return const Center(child: Text('图片暂时不可用'));
              if (image.data == null) {
                return const Center(child: CircularProgressIndicator());
              }
              return InteractiveViewer(
                minScale: .5,
                maxScale: 5,
                child: Center(
                  child: Image.memory(
                    image.data!,
                    semanticLabel:
                        '${_item['title'] ?? _item['filename'] ?? '图片'}',
                    errorBuilder: (_, _, _) => const Text('图片暂时不可用'),
                  ),
                ),
              );
            },
          );
        }
        return SingleChildScrollView(
          padding: const EdgeInsets.all(20),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              if (preview['truncated'] == true)
                Padding(
                  padding: const EdgeInsets.only(bottom: 16),
                  child: Text(
                    '仅显示部分内容，下载可查看完整文件。',
                    style: TextStyle(color: context.muse.muted),
                  ),
                ),
              if (kind == 'csv')
                _csv(preview)
              else if (kind == 'markdown')
                LumaMarkdown(
                  data: '${preview['text'] ?? ''}',
                  style: TextStyle(
                    color: context.muse.text,
                    fontSize: 15,
                    height: 1.6,
                  ),
                  linkColor: context.muse.link,
                  selectable: true,
                )
              else if (kind == 'text' || kind == 'html_source') ...[
                if (kind == 'html_source')
                  Padding(
                    padding: const EdgeInsets.only(bottom: 16),
                    child: Text(
                      '安全预览：仅展示 HTML 源码，不执行或渲染网页。',
                      style: TextStyle(color: context.muse.muted),
                    ),
                  ),
                SelectableText(
                  '${preview['text'] ?? ''}',
                  style: TextStyle(
                    fontFamily: 'monospace',
                    fontSize: 13,
                    height: 1.6,
                    color: context.muse.text,
                  ),
                ),
              ] else
                const SizedBox(
                  width: double.infinity,
                  height: 160,
                  child: Center(child: Text('暂不支持预览')),
                ),
            ],
          ),
        );
      },
    ),
  );

  Widget _csv(Map<String, dynamic> preview) {
    final columns = preview['columns'] is List
        ? preview['columns'] as List
        : const [];
    final rows = preview['rows'] is List ? preview['rows'] as List : const [];
    if (columns.isEmpty) return const Text('表格没有内容');
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          '共 ${preview['total_rows'] ?? rows.length} 行',
          style: TextStyle(color: context.muse.muted),
        ),
        const SizedBox(height: 12),
        SingleChildScrollView(
          scrollDirection: Axis.horizontal,
          child: DataTable(
            columns: [
              for (final column in columns) DataColumn(label: Text('$column')),
            ],
            rows: [
              for (final row in rows)
                if (row is List)
                  DataRow(
                    cells: [
                      for (var index = 0; index < columns.length; index++)
                        DataCell(
                          SelectableText(
                            index < row.length ? '${row[index] ?? ''}' : '',
                          ),
                        ),
                    ],
                  ),
            ],
          ),
        ),
      ],
    );
  }
}

class _RenameResourceDialog extends StatefulWidget {
  const _RenameResourceDialog({required this.title});

  final String title;

  @override
  State<_RenameResourceDialog> createState() => _RenameResourceDialogState();
}

class _RenameResourceDialogState extends State<_RenameResourceDialog> {
  late final _controller = TextEditingController(text: widget.title);

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => AlertDialog(
    title: const Text('重命名'),
    content: TextField(
      controller: _controller,
      autofocus: true,
      maxLength: 200,
      decoration: const InputDecoration(labelText: '标题'),
    ),
    actions: [
      TextButton(
        onPressed: () => Navigator.of(context).pop(),
        child: const Text('取消'),
      ),
      TextButton(
        onPressed: () {
          final title = _controller.text.trim();
          if (title.isNotEmpty) Navigator.of(context).pop(title);
        },
        child: const Text('保存'),
      ),
    ],
  );
}
