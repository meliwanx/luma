import 'package:flutter/material.dart';

import '../api.dart';
import '../theme.dart';

class MemoryView extends StatefulWidget {
  const MemoryView({
    super.key,
    required this.memories,
    this.api,
    this.ideasOnly = false,
    this.showFiles = false,
    this.contentPadding,
    this.showHeader = true,
    this.onRefresh,
    this.onFile,
  });

  final List<Map<String, dynamic>> memories;
  final AssistantApi? api;
  final bool ideasOnly;
  final bool showFiles;
  final EdgeInsetsGeometry? contentPadding;
  final bool showHeader;
  final Future<void> Function()? onRefresh;
  final Future<void> Function(Map<String, dynamic>)? onFile;

  @override
  State<MemoryView> createState() => _MemoryViewState();
}

class _MemoryViewState extends State<MemoryView> {
  late List<Map<String, dynamic>> _memories;
  List<Map<String, dynamic>> _files = [];
  String? _filesError;
  int _filesRequest = 0;
  final Map<String, int> _pinRequests = {};

  @override
  void initState() {
    super.initState();
    _memories = _copyMemories(_filteredMemories);
    _loadFiles();
  }

  @override
  void didUpdateWidget(covariant MemoryView oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!identical(oldWidget.memories, widget.memories) ||
        oldWidget.ideasOnly != widget.ideasOnly) {
      _memories = _copyMemories(_filteredMemories);
    }
    if (oldWidget.ideasOnly != widget.ideasOnly ||
        oldWidget.showFiles != widget.showFiles ||
        oldWidget.api != widget.api) {
      _loadFiles();
    }
  }

  List<Map<String, dynamic>> get _filteredMemories =>
      widget.ideasOnly ? filterIdeaMemories(widget.memories) : widget.memories;

  Future<void> _loadFiles() async {
    final request = ++_filesRequest;
    if (!widget.showFiles || widget.ideasOnly || widget.api == null) {
      if (_files.isNotEmpty || _filesError != null) {
        setState(() {
          _files = [];
          _filesError = null;
        });
      }
      return;
    }
    final currentApi = widget.api;
    try {
      final files = await currentApi!.listFiles();
      if (!mounted ||
          request != _filesRequest ||
          !widget.showFiles ||
          widget.ideasOnly ||
          currentApi != widget.api) {
        return;
      }
      setState(() {
        _files = files;
        _filesError = null;
      });
    } catch (error) {
      if (!mounted ||
          request != _filesRequest ||
          !widget.showFiles ||
          widget.ideasOnly ||
          currentApi != widget.api) {
        return;
      }
      setState(() => _filesError = '文件列表暂时不可用，下拉重试');
    }
  }

  Future<void> _refresh() async {
    try {
      await widget.onRefresh?.call();
      if (widget.api != null) {
        final memories = await widget.api!.listMemories();
        if (!mounted) return;
        setState(
          () => _memories = _copyMemories(
            widget.ideasOnly ? filterIdeaMemories(memories) : memories,
          ),
        );
      }
      await _loadFiles();
    } catch (error) {
      if (mounted) _showError(error, '记忆刷新失败');
    }
  }

  @override
  Widget build(BuildContext context) {
    return LayoutBuilder(
      builder: (context, constraints) {
        final isPhone = constraints.maxWidth < MuseMetrics.mobileBreakpoint;
        final horizontalPadding = isPhone ? 16.0 : 24.0;

        final list = ListView(
          physics: const AlwaysScrollableScrollPhysics(),
          keyboardDismissBehavior: ScrollViewKeyboardDismissBehavior.onDrag,
          padding:
              widget.contentPadding ??
              EdgeInsets.fromLTRB(
                horizontalPadding,
                isPhone ? 24 : 72,
                horizontalPadding,
                24,
              ),
          children: [
            Center(
              child: ConstrainedBox(
                constraints: const BoxConstraints(
                  maxWidth: MuseMetrics.readingColumn,
                ),
                child: SizedBox(
                  width: double.infinity,
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      if (widget.showHeader) ...[
                        _MemoryHeader(ideasOnly: widget.ideasOnly),
                        const SizedBox(height: 28),
                      ],
                      if (_memories.isEmpty)
                        _EmptyMemories(ideasOnly: widget.ideasOnly)
                      else
                        LayoutBuilder(
                          builder: (context, gridConstraints) {
                            final width = gridConstraints.maxWidth;
                            final columns = width < 640
                                ? 1
                                : ((width + 12) / (220 + 12))
                                      .floor()
                                      .clamp(1, 4)
                                      .toInt();
                            final tileWidth =
                                (width - (columns - 1) * 12) / columns;

                            return Wrap(
                              spacing: 12,
                              runSpacing: 12,
                              children: [
                                for (
                                  var index = 0;
                                  index < _memories.length;
                                  index++
                                )
                                  _memoryTile(index, tileWidth),
                              ],
                            );
                          },
                        ),
                      if (widget.showFiles &&
                          !widget.ideasOnly &&
                          (_files.isNotEmpty || _filesError != null)) ...[
                        const SizedBox(height: 28),
                        const Text(
                          '文件',
                          style: TextStyle(
                            fontSize: 16,
                            fontWeight: FontWeight.w600,
                          ),
                        ),
                        const SizedBox(height: 12),
                        if (_filesError != null)
                          Text(
                            _filesError!,
                            style: TextStyle(color: context.muse.muted),
                          ),
                        for (final file in _files)
                          Material(
                            type: MaterialType.transparency,
                            child: ListTile(
                              contentPadding: EdgeInsets.zero,
                              leading: const Icon(
                                Icons.insert_drive_file_outlined,
                              ),
                              title: Text(
                                '${file['filename'] ?? '文件'}',
                                maxLines: 1,
                                overflow: TextOverflow.ellipsis,
                              ),
                              subtitle: Text(_fileSize(file['size_bytes'])),
                              onTap: widget.onFile == null
                                  ? null
                                  : () => widget.onFile!({
                                      ...file,
                                      'file_id': file['id'],
                                    }),
                            ),
                          ),
                      ],
                    ],
                  ),
                ),
              ),
            ),
          ],
        );
        return RefreshIndicator(onRefresh: _refresh, child: list);
      },
    );
  }

  Widget _memoryTile(int index, double width) => SizedBox(
    width: width,
    child: _MemoryCard(
      memory: _memories[index],
      onConfirm: _confirmMemory,
      onPinnedChanged: (value) => _setPinned(index, value),
    ),
  );

  Future<void> _setPinned(int index, bool pinned) async {
    if (index < 0 || index >= _memories.length) return;
    final previous = _memories[index];
    final id = '${previous['id']}';
    final request = (_pinRequests[id] ?? 0) + 1;
    _pinRequests[id] = request;
    final optimistic = Map<String, dynamic>.from(previous)..['pinned'] = pinned;
    setState(() => _memories[index] = optimistic);
    if (widget.api == null) return;
    try {
      final updated = await widget.api!.updateMemory(id, pinned: pinned);
      if (!mounted || _pinRequests[id] != request) return;
      final currentIndex = _memories.indexWhere(
        (item) => '${item['id']}' == id,
      );
      if (currentIndex >= 0) setState(() => _memories[currentIndex] = updated);
    } catch (error) {
      if (!mounted || _pinRequests[id] != request) return;
      final currentIndex = _memories.indexWhere(
        (item) => '${item['id']}' == id,
      );
      if (currentIndex >= 0) setState(() => _memories[currentIndex] = previous);
      _showError(error, '置顶状态更新失败');
    }
  }

  Future<void> _confirmMemory(Map<String, dynamic> memory) async {
    if (widget.api == null) return;
    final category = await showDialog<String>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: const Text('确认这条推断'),
        content: const Text('确认后可以作为事实或偏好保存。'),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(dialogContext).pop('fact'),
            child: const Text('事实'),
          ),
          FilledButton(
            onPressed: () => Navigator.of(dialogContext).pop('preference'),
            child: const Text('偏好'),
          ),
        ],
      ),
    );
    if (category == null || !mounted) return;
    try {
      final updated = await widget.api!.confirmMemory(
        '${memory['id']}',
        category: category,
      );
      if (!mounted) return;
      final index = _memories.indexWhere((item) => item['id'] == memory['id']);
      if (index >= 0) setState(() => _memories[index] = updated);
    } catch (error) {
      if (mounted) _showError(error, '确认记忆失败');
    }
  }

  void _showError(Object error, String fallback) {
    final text = error is AssistantApiException ? error.message : fallback;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(text)));
  }
}

class _MemoryHeader extends StatelessWidget {
  const _MemoryHeader({required this.ideasOnly});

  final bool ideasOnly;

  @override
  Widget build(BuildContext context) {
    final colors = context.muse;

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          ideasOnly ? '点子' : '记忆',
          style: TextStyle(
            color: colors.text,
            fontSize: 24,
            fontWeight: FontWeight.w600,
          ),
        ),
        const SizedBox(height: 8),
        ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 520),
          child: Text(
            ideasOnly ? '把灵感随手告诉 Luma' : '你可以随时查看、编辑或删除 Luma 记住的内容。',
            style: TextStyle(color: colors.muted, fontSize: 14, height: 1.5),
          ),
        ),
      ],
    );
  }
}

class _MemoryCard extends StatelessWidget {
  const _MemoryCard({
    required this.memory,
    required this.onConfirm,
    required this.onPinnedChanged,
  });

  final Map<String, dynamic> memory;
  final ValueChanged<Map<String, dynamic>> onConfirm;
  final ValueChanged<bool> onPinnedChanged;

  @override
  Widget build(BuildContext context) {
    final colors = context.muse;
    final category = memory['category'] ?? memory['kind'] ?? 'memory';
    final inferred = _isInferred(category);
    final metadata = memory['metadata'] is Map
        ? Map<String, dynamic>.from(memory['metadata'] as Map)
        : const <String, dynamic>{};
    final confirmed =
        metadata['confirmed'] == true ||
        '${metadata['confirmed']}'.toLowerCase() == 'true';
    final pinned = memory['pinned'] == true;

    return Container(
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: colors.bubble,
        borderRadius: BorderRadius.circular(MuseMetrics.cardRadius),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Expanded(
                child: Text(
                  _categoryLabel('$category'),
                  style: TextStyle(
                    color: inferred ? colors.warn : colors.muted,
                    fontSize: 12,
                    fontWeight: FontWeight.w500,
                  ),
                ),
              ),
              if (inferred)
                Container(
                  padding: const EdgeInsets.symmetric(
                    horizontal: 7,
                    vertical: 3,
                  ),
                  decoration: BoxDecoration(
                    color: colors.warn.withValues(alpha: 0.12),
                    borderRadius: BorderRadius.circular(10),
                  ),
                  child: Text(
                    confirmed ? '已确认' : '推断',
                    style: TextStyle(
                      color: colors.warn,
                      fontSize: 11,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                ),
            ],
          ),
          const SizedBox(height: 10),
          Text(
            '${memory['content'] ?? ''}',
            style: TextStyle(color: colors.text, fontSize: 14, height: 1.55),
          ),
          const SizedBox(height: 12),
          Row(
            children: [
              Icon(
                pinned ? Icons.push_pin : Icons.push_pin_outlined,
                size: 15,
                color: pinned ? colors.accent : colors.faint,
              ),
              const SizedBox(width: 4),
              Expanded(
                child: Text(
                  '置顶',
                  style: TextStyle(color: colors.muted, fontSize: 12),
                ),
              ),
              SizedBox(
                height: 44,
                child: Switch(
                  value: pinned,
                  onChanged: onPinnedChanged,
                  materialTapTargetSize: MaterialTapTargetSize.padded,
                ),
              ),
            ],
          ),
          if (inferred && !confirmed) ...[
            const SizedBox(height: 8),
            SizedBox(
              width: double.infinity,
              child: OutlinedButton(
                onPressed: () => onConfirm(memory),
                style: OutlinedButton.styleFrom(
                  minimumSize: const Size.fromHeight(44),
                  foregroundColor: colors.text,
                  side: BorderSide(color: colors.line),
                  shape: const StadiumBorder(),
                ),
                child: const Text('确认推断'),
              ),
            ),
          ],
        ],
      ),
    );
  }
}

class _EmptyMemories extends StatelessWidget {
  const _EmptyMemories({required this.ideasOnly});

  final bool ideasOnly;

  @override
  Widget build(BuildContext context) {
    final colors = context.muse;

    return SizedBox(
      width: double.infinity,
      child: Padding(
        padding: const EdgeInsets.symmetric(vertical: 48),
        child: Column(
          children: [
            Text(
              ideasOnly ? '还没有点子' : '还没有记忆',
              style: TextStyle(
                color: colors.text,
                fontSize: 15,
                fontWeight: FontWeight.w600,
              ),
            ),
            const SizedBox(height: 6),
            Text(
              ideasOnly ? '把灵感随手告诉 Luma' : '当 Luma 记住新的内容时，它们会显示在这里。',
              textAlign: TextAlign.center,
              style: TextStyle(color: colors.muted, fontSize: 13, height: 1.5),
            ),
          ],
        ),
      ),
    );
  }
}

String _categoryLabel(String category) {
  switch (category) {
    case 'preference':
      return '偏好';
    case 'fact':
      return '事实';
    case 'general':
      return '通用';
    case 'goal':
      return '目标';
    case 'idea':
    case 'ideas':
    case '点子':
      return '点子';
    case 'inferred':
    case '推断':
      return '推断';
    default:
      return category;
  }
}

bool _isInferred(dynamic category) {
  final normalized = '$category'.trim().toLowerCase();
  return normalized == 'inferred' || normalized == '推断';
}

List<Map<String, dynamic>> _copyMemories(List<Map<String, dynamic>> source) =>
    source.map((item) => Map<String, dynamic>.from(item)).toList();

List<Map<String, dynamic>> filterIdeaMemories(
  List<Map<String, dynamic>> source,
) => source.where((item) {
  final category = '${item['category'] ?? item['kind'] ?? ''}'
      .trim()
      .toLowerCase();
  return category == 'idea' || category == 'ideas' || category == '点子';
}).toList();

String _fileSize(Object? value) {
  final bytes = value is num ? value.toDouble() : 0.0;
  if (bytes >= 1024 * 1024) {
    return '${(bytes / (1024 * 1024)).toStringAsFixed(1)} MB';
  }
  if (bytes >= 1024) return '${(bytes / 1024).toStringAsFixed(1)} KB';
  return '${bytes.toInt()} B';
}
