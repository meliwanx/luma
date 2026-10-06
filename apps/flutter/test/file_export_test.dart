import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:luma_client/file_export.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test('文件名保留中文，去掉路径和控制字符', () {
    expect(exportFilename('../目录/报告.html'), '报告.html');
    expect(exportFilename(r'C:\folder\产物.csv'), '产物.csv');
    expect(exportFilename('a\u0000b?.txt'), 'a_b_.txt');
    expect(exportFilename('..'), '文件');
  });

  test('HTML 文件按字节送到系统导出通道，不渲染为网页', () async {
    const channel = MethodChannel('luma/files');
    MethodCall? exported;
    final messenger =
        TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;
    messenger.setMockMethodCallHandler(channel, (call) async {
      exported = call;
      return true;
    });
    addTearDown(() => messenger.setMockMethodCallHandler(channel, null));
    final bytes = Uint8List.fromList('<script>alert(1)</script>'.codeUnits);
    expect(await exportFile(bytes, '../产物.html'), isTrue);
    expect(exported!.method, 'export');
    expect(exported!.arguments['filename'], '产物.html');
    expect(exported!.arguments['bytes'], bytes);
    expect(exported!.arguments.containsKey('token'), isFalse);
  });
}
