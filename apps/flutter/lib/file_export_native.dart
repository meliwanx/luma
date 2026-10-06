import 'package:flutter/services.dart';

const _channel = MethodChannel('luma/files');

Future<bool> exportFile(Uint8List bytes, String filename) async =>
    await _channel.invokeMethod<bool>('export', {
      'filename': filename,
      'bytes': bytes,
    }) ??
    false;
