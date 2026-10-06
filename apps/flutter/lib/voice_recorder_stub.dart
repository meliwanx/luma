import 'voice_recorder.dart';

VoiceRecorder createVoiceRecorder() => _UnsupportedVoiceRecorder();

class _UnsupportedVoiceRecorder implements VoiceRecorder {
  @override
  Stream<double> get amplitudes => const Stream.empty();

  @override
  Future<bool> hasPermission() async =>
      throw const VoiceRecorderException('当前平台暂不支持语音输入');

  @override
  Future<void> start() async =>
      throw const VoiceRecorderException('当前平台暂不支持语音输入');

  @override
  Future<VoiceRecording?> stop() async => null;

  @override
  Future<void> cancel() async {}

  @override
  Future<void> dispose() async {}
}
