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

bool isBrowserLiveUrl(String value) {
  final uri = Uri.tryParse(value);
  return uri != null &&
      uri.scheme == 'https' &&
      uri.host.toLowerCase().endsWith('.tencentags.com') &&
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
