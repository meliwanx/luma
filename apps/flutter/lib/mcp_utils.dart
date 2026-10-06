/// Detect secret-bearing JSON conservatively, including unfinished drafts.
/// Values remain only in the request body until the server returns references.
bool containsSecretJson(String text) {
  if (!text.contains('{')) return false;
  final decoded = text.replaceAllMapped(
    RegExp(r'\\u([0-9a-f]{4})', caseSensitive: false),
    (match) => String.fromCharCode(int.parse(match[1]!, radix: 16)),
  );
  return RegExp(
    r'"(?:headers|env|authorization|token|api[-_]?key|password|secret|access[-_]?token|access[-_]?key|client[-_]?secret)"\s*:',
    caseSensitive: false,
  ).hasMatch(decoded);
}

String optimisticChatContent(String text) =>
    containsSecretJson(text) ? '[含敏感信息的消息：等待服务端加密保存]' : text;

/// Keep the stored references intact for tools, and mask only their display.
String displaySecretReferences(String text) => text.replaceAll(
  RegExp(r'\{\{secret:sec_[A-Za-z0-9_-]{16}\}\}'),
  '••••••（已加密保存）',
);
