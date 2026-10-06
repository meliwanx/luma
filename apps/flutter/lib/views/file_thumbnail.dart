import 'dart:typed_data';

import 'package:flutter/material.dart';

import '../api.dart';

class FileThumbnail extends StatefulWidget {
  const FileThumbnail({
    super.key,
    required this.api,
    required this.fileId,
    required this.filename,
    this.height = 180,
    this.fit = BoxFit.contain,
    this.padding = const EdgeInsets.fromLTRB(10, 0, 10, 10),
  });

  final AssistantApi api;
  final String fileId;
  final String filename;
  final double? height;
  final BoxFit fit;
  final EdgeInsetsGeometry padding;

  @override
  State<FileThumbnail> createState() => _FileThumbnailState();
}

class _FileThumbnailState extends State<FileThumbnail> {
  late Future<Uint8List> _bytes;

  @override
  void initState() {
    super.initState();
    _bytes = widget.api.downloadFileBytes(widget.fileId);
  }

  @override
  void didUpdateWidget(FileThumbnail oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.fileId != widget.fileId || oldWidget.api != widget.api) {
      _bytes = widget.api.downloadFileBytes(widget.fileId);
    }
  }

  @override
  Widget build(BuildContext context) => FutureBuilder<Uint8List>(
    future: _bytes,
    builder: (context, snapshot) {
      final bytes = snapshot.data;
      if (bytes == null || bytes.isEmpty) return const SizedBox.shrink();
      return Padding(
        padding: widget.padding,
        child: ClipRRect(
          borderRadius: BorderRadius.circular(8),
          child: Image.memory(
            bytes,
            height: widget.height,
            width: widget.fit == BoxFit.cover ? double.infinity : null,
            fit: widget.fit,
            semanticLabel: widget.filename,
            errorBuilder: (_, _, _) => const SizedBox.shrink(),
          ),
        ),
      );
    },
  );
}
