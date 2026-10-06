import 'package:flutter/material.dart';

import '../api.dart';
import '../theme.dart';

/// Selects an existing resource without opening a URL or sending a message.
class AttachmentPicker extends StatefulWidget {
  const AttachmentPicker({super.key, required this.api});

  final AssistantApi api;

  @override
  State<AttachmentPicker> createState() => _AttachmentPickerState();
}

class _AttachmentPickerState extends State<AttachmentPicker> {
  List<Map<String, dynamic>> _files = [];
  bool _loading = true;
  String? _error;
  int _requestVersion = 0;

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void didUpdateWidget(covariant AttachmentPicker oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.api != widget.api) _load();
  }

  @override
  void dispose() {
    _requestVersion++;
    super.dispose();
  }

  Future<void> _load() async {
    final version = ++_requestVersion;
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final files = await widget.api.listFiles();
      if (!mounted || version != _requestVersion) return;
      setState(() {
        // A file is actionable only through the server's authenticated id.
        _files = files.where((file) {
          final id = file['id'];
          return id is String && id.trim().isNotEmpty;
        }).toList();
        _loading = false;
      });
    } catch (_) {
      if (!mounted || version != _requestVersion) return;
      setState(() {
        _loading = false;
        _error = '文件列表加载失败，请重试';
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    final colors = context.muse;
    return SizedBox(
      height: MediaQuery.sizeOf(context).height * 0.65,
      child: SafeArea(
        top: false,
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(20, 8, 8, 4),
              child: Row(
                children: [
                  Icon(Icons.attach_file_rounded, color: colors.accent),
                  const SizedBox(width: 8),
                  const Expanded(
                    child: Text(
                      '添加附件',
                      style: TextStyle(
                        fontSize: 20,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                  ),
                  IconButton(
                    tooltip: '关闭附件选择',
                    constraints: const BoxConstraints.tightFor(
                      width: 48,
                      height: 48,
                    ),
                    icon: const Icon(Icons.close_rounded),
                    onPressed: () => Navigator.of(context).pop(),
                  ),
                ],
              ),
            ),
            Padding(
              padding: const EdgeInsets.fromLTRB(20, 0, 20, 16),
              child: Text(
                '选择资源库中的文件',
                style: TextStyle(color: colors.muted, fontSize: 13),
              ),
            ),
            Expanded(child: _body(context)),
          ],
        ),
      ),
    );
  }

  Widget _body(BuildContext context) {
    final colors = context.muse;
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
              OutlinedButton(
                onPressed: _load,
                style: OutlinedButton.styleFrom(
                  minimumSize: const Size(88, 44),
                ),
                child: const Text('重试'),
              ),
            ],
          ),
        ),
      );
    }
    if (_files.isEmpty) {
      return Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(Icons.folder_open_rounded, color: colors.muted, size: 32),
              const SizedBox(height: 16),
              const Text('资源库还没有文件'),
              const SizedBox(height: 8),
              Text(
                '可以先在 Web 端上传，再从这里选择。',
                textAlign: TextAlign.center,
                style: TextStyle(color: colors.muted, height: 1.5),
              ),
            ],
          ),
        ),
      );
    }
    return ListView.separated(
      padding: const EdgeInsets.fromLTRB(16, 0, 16, 16),
      itemCount: _files.length,
      separatorBuilder: (_, _) => const SizedBox(height: 8),
      itemBuilder: (context, index) {
        final file = _files[index];
        final id = file['id'] as String;
        return Material(
          color: colors.bubble,
          borderRadius: BorderRadius.circular(20),
          child: ListTile(
            key: ValueKey('attachment-file-$id'),
            minTileHeight: 72,
            shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(20),
            ),
            contentPadding: const EdgeInsets.symmetric(
              horizontal: 16,
              vertical: 8,
            ),
            leading: Icon(
              Icons.insert_drive_file_outlined,
              color: colors.muted,
            ),
            title: Text(
              '${file['filename'] ?? '文件'}',
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
            ),
            subtitle: Text(_fileSize(file['size_bytes'])),
            trailing: const Icon(Icons.add_rounded),
            onTap: () =>
                Navigator.of(context)
                    .pop<Map<String, dynamic>>(Map<String, dynamic>.from(file)),
          ),
        );
      },
    );
  }
}

String _fileSize(Object? value) {
  final bytes = value is num ? value.toDouble() : 0.0;
  if (bytes >= 1024 * 1024) {
    return '${(bytes / (1024 * 1024)).toStringAsFixed(1)} MB';
  }
  if (bytes >= 1024) return '${(bytes / 1024).toStringAsFixed(1)} KB';
  return '${bytes.toInt()} B';
}
