import 'voice_recorder_stub.dart'
    if (dart.library.io) 'voice_recorder_io.dart'
    as platform;

abstract class VoiceRecorder {
  Future<bool> hasPermission();
  Future<void> start();
  Future<VoiceRecording?> stop();
  Future<void> cancel();
  Future<void> dispose();
  Stream<double> get amplitudes;
}

VoiceRecorder createVoiceRecorder() => platform.createVoiceRecorder();

class VoiceRecording {
  VoiceRecording(this.audio, {this.onDelete});

  final Object audio;
  final Future<void> Function()? onDelete;
  Future<void>? _deletion;

  Future<void> delete() => _deletion ??= onDelete?.call() ?? Future.value();
}

class VoiceRecorderException implements Exception {
  const VoiceRecorderException(this.detail);

  final String detail;

  @override
  String toString() => detail;
}
