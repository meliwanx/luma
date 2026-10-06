Future<List<int>> readAudioBytes(Object audio) async {
  if (audio is List<int>) return audio;
  throw ArgumentError('Audio must be bytes on this platform');
}

String audioFilename(Object audio) => 'voice.wav';
