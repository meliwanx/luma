import 'package:flutter/foundation.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:luma_client/api.dart';

void main() {
  test('debug builds default to the local API when no define is supplied', () {
    const configured = String.fromEnvironment('API_BASE_URL');
    if (kDebugMode && configured.isEmpty) {
      expect(AssistantApi.base, 'http://localhost:8000');
    }
    expect(AssistantApi.configurationMessage, contains('API_BASE_URL'));
  });
}
