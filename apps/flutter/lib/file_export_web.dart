import 'dart:js_interop';
import 'dart:typed_data';

@JS('Blob')
extension type _Blob._(JSObject _) implements JSObject {
  external factory _Blob(JSArray<JSAny?> parts);
}

extension type _Anchor._(JSObject _) implements JSObject {
  external set href(JSString value);
  external set download(JSString value);
  external void click();
}

@JS('document.createElement')
external _Anchor _createElement(JSString tag);

@JS('URL.createObjectURL')
external JSString _createObjectUrl(_Blob blob);

@JS('URL.revokeObjectURL')
external void _revokeObjectUrl(JSString url);

Future<bool> exportFile(Uint8List bytes, String filename) async {
  final url = _createObjectUrl(_Blob(<JSAny?>[bytes.toJS].toJS));
  try {
    _createElement('a'.toJS)
      ..href = url
      ..download = filename.toJS
      ..click();
    await Future<void>.delayed(const Duration(seconds: 1));
    return true;
  } finally {
    _revokeObjectUrl(url);
  }
}
