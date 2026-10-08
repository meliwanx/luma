import 'package:flutter/foundation.dart';

import 'api.dart';

const defaultBrowserLiveHostSuffixes = ['.tencentags.com'];

final _suffixPattern = RegExp(r'^\.[a-z0-9.-]+$');

List<String> normalizeBrowserLiveHostSuffixes(Object? raw) {
  final values = raw is List
      ? raw
      : raw is String
      ? raw.split(',')
      : const <Object>[];
  final seen = <String>{};
  final next = <String>[];
  for (final item in values) {
    if (item is! String) continue;
    var text = item.trim().toLowerCase();
    while (text.endsWith('.')) {
      text = text.substring(0, text.length - 1);
    }
    if (text.isEmpty) continue;
    if (!text.startsWith('.')) text = '.$text';
    if (text.contains('/') ||
        text.contains('@') ||
        text.contains(' ') ||
        text.contains(':') ||
        text.contains('..') ||
        text.contains('\\')) {
      continue;
    }
    if (!_suffixPattern.hasMatch(text) || !seen.add(text)) continue;
    next.add(text);
  }
  return next;
}

typedef BrowserLiveHostLoader = Future<Map<String, dynamic>> Function();

class BrowserLiveHosts extends ChangeNotifier {
  BrowserLiveHosts({this.load});

  static final BrowserLiveHosts instance = BrowserLiveHosts(
    load: _fetchClientConfig,
  );

  final BrowserLiveHostLoader? load;
  List<String> _suffixes = List<String>.from(defaultBrowserLiveHostSuffixes);

  List<String> get suffixes => List<String>.unmodifiable(_suffixes);

  void apply(Object? raw) {
    final parsed = normalizeBrowserLiveHostSuffixes(raw);
    _suffixes = parsed.isEmpty
        ? List<String>.from(defaultBrowserLiveHostSuffixes)
        : parsed;
    notifyListeners();
  }

  /// A failed or incomplete response restores the public default suffix.
  Future<void> refresh() async {
    final fetch = load;
    if (fetch == null) return;
    try {
      final payload = await fetch();
      apply(payload['browser_live_host_suffixes']);
    } catch (_) {
      apply(defaultBrowserLiveHostSuffixes);
    }
  }
}

Future<Map<String, dynamic>> _fetchClientConfig() async {
  if (!AssistantApi.hasConfiguredBase) {
    throw const AssistantApiException('未配置服务器地址');
  }
  final api = AssistantApi();
  try {
    return await api.clientConfig();
  } finally {
    api.close();
  }
}

const _browserProgress = {
  'browser.open': '正在打开网页…',
  'browser.read': '正在读取页面…',
  'browser.screenshot': '正在截取页面…',
  'browser.click': '正在点击页面…',
  'browser.type': '正在填写页面…',
  'browser.scroll': '正在滚动页面…',
  'browser.submit': '正在提交页面…',
  'browser.live': '正在打开实时画面…',
};

String? browserProgressLabel(String tool) =>
    tool.startsWith('browser.') ? _browserProgress[tool] ?? '正在操作浏览器…' : null;

bool isBrowserLiveUrl(String value, [List<String>? suffixes]) {
  final uri = Uri.tryParse(value);
  if (uri == null) return false;
  final host = uri.host.toLowerCase();
  final allowed = suffixes ?? BrowserLiveHosts.instance.suffixes;
  final trusted = allowed.any(
    (suffix) =>
        suffix.isNotEmpty &&
        host.endsWith(suffix) &&
        host.length > suffix.length,
  );
  return uri.scheme == 'https' &&
      trusted &&
      uri.userInfo.isEmpty &&
      (!uri.hasPort || uri.port == 443);
}

bool isImageFile(String value) => const {
  'image/png',
  'image/jpeg',
  'image/gif',
  'image/webp',
  'image/avif',
}.contains(value.split(';').first.trim().toLowerCase());

/// These URLs are short-lived credentials: keep the cards in client memory
/// when durable tool metadata replaces the other streaming progress rows.
List<Map<String, dynamic>> liveBrowserStates(Object? calls) {
  if (calls is! List) return const [];
  return [
    for (final call in calls)
      if (call is Map &&
          (call['kind'] == 'browser_live' ||
              (call['data'] is Map && call['data']['kind'] == 'browser_live')))
        Map<String, dynamic>.from(call),
  ];
}
