import 'dart:async';

import 'package:flutter/material.dart';
import 'package:webview_flutter/webview_flutter.dart';

import '../browser_tools.dart';

class BrowserLiveCard extends StatefulWidget {
  const BrowserLiveCard({super.key, required this.url, this.expiresIn});

  final String url;
  final int? expiresIn;

  @override
  State<BrowserLiveCard> createState() => _BrowserLiveCardState();
}

class _BrowserLiveCardState extends State<BrowserLiveCard> {
  Timer? _expiry;
  bool _expired = false;

  @override
  void initState() {
    super.initState();
    _setExpiry();
  }

  @override
  void didUpdateWidget(BrowserLiveCard oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.url != widget.url ||
        oldWidget.expiresIn != widget.expiresIn) {
      _setExpiry();
    }
  }

  void _setExpiry() {
    _expiry?.cancel();
    _expired = false;
    final seconds = widget.expiresIn;
    if (seconds != null && seconds > 0) {
      _expiry = Timer(Duration(seconds: seconds), () {
        if (mounted) setState(() => _expired = true);
      });
    }
  }

  @override
  void dispose() {
    _expiry?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => ListenableBuilder(
    listenable: BrowserLiveHosts.instance,
    builder: (context, _) => Card(
      margin: const EdgeInsets.only(bottom: 8),
      child: ListTile(
        leading: const Icon(Icons.public),
        title: const Text('浏览器实时画面'),
        subtitle: _expired ? const Text('实时画面已过期，请重新打开') : null,
        trailing: TextButton(
          onPressed: !_expired && isBrowserLiveUrl(widget.url)
              ? () => Navigator.of(context).push(
                  MaterialPageRoute<void>(
                    fullscreenDialog: true,
                    builder: (_) => BrowserLiveView(url: widget.url),
                  ),
                )
              : null,
          child: const Text('观看实时画面'),
        ),
      ),
    ),
  );
}

class BrowserLiveView extends StatefulWidget {
  const BrowserLiveView({super.key, required this.url});

  final String url;

  @override
  State<BrowserLiveView> createState() => _BrowserLiveViewState();
}

class _BrowserLiveViewState extends State<BrowserLiveView> {
  WebViewController? _controller;
  bool _failed = false;

  @override
  void initState() {
    super.initState();
    if (!isBrowserLiveUrl(widget.url)) return;
    final controller = WebViewController()
      ..setJavaScriptMode(JavaScriptMode.unrestricted);
    _controller = controller;
    unawaited(
      _load(controller).catchError((Object _) {
        if (mounted) setState(() => _failed = true);
      }),
    );
  }

  Future<void> _load(WebViewController controller) async {
    await controller.setNavigationDelegate(
      NavigationDelegate(
        onNavigationRequest: (request) => isBrowserLiveUrl(request.url)
            ? NavigationDecision.navigate
            : NavigationDecision.prevent,
      ),
    );
    if (!mounted) return;
    await controller.loadRequest(Uri.parse(widget.url));
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(
      title: const Text('浏览器实时画面'),
      leading: IconButton(
        icon: const Icon(Icons.close),
        tooltip: '关闭',
        onPressed: () => Navigator.of(context).pop(),
      ),
    ),
    body: _controller == null || _failed
        ? const Center(child: Text('实时画面地址无效'))
        : WebViewWidget(controller: _controller!),
  );
}
