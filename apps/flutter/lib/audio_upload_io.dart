import 'dart:io';

Future<List<int>> readAudioBytes(Object audio) async {
  if (audio is List<int>) return audio;
  if (audio is File) return audio.readAsBytes();
  throw ArgumentError('Audio must be a File or bytes');
}

String audioFilename(Object audio) {
  if (audio is File) return audio.path.replaceAll('\\', '/').split('/').last;
  return 'voice.wav';
}
