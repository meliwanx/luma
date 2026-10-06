import 'package:flutter/material.dart';
import 'package:webview_flutter/webview_flutter.dart';

class FileResultView extends StatefulWidget {
  const FileResultView({
    super.key,
    required this.title,
    required this.uri,
    required this.token,
  });

  final String title;
  final Uri uri;
  final String? token;

  @override
  State<FileResultView> createState() => _FileResultViewState();
}

class _FileResultViewState extends State<FileResultView> {
  late final WebViewController _controller;

  @override
  void initState() {
    super.initState();
    _controller = WebViewController()
      ..setJavaScriptMode(JavaScriptMode.disabled)
      ..loadRequest(
        widget.uri,
        headers: {
          if (widget.token != null && widget.token!.isNotEmpty)
            'Authorization': 'Bearer ${widget.token}',
        },
      );
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(title: Text(widget.title)),
    body: WebViewWidget(controller: _controller),
  );
}
