import 'dart:typed_data';

import 'file_export_native.dart'
    if (dart.library.js_interop) 'file_export_web.dart'
    as platform;

String exportFilename(String filename) {
  final name = filename
      .split(RegExp(r'[/\\]'))
      .last
      .replaceAll(RegExp(r'[<>:"|?*\x00-\x1f]'), '_')
      .trim();
  return name.isEmpty || name == '.' || name == '..' ? '文件' : name;
}

/// Exports authenticated bytes; file content never becomes a webpage.
Future<bool> exportFile(Uint8List bytes, String filename) =>
    platform.exportFile(bytes, exportFilename(filename));
