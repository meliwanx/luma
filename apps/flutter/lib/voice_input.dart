import 'dart:async';

import 'package:flutter/foundation.dart';

import 'api.dart';
import 'voice_recorder.dart';

enum VoiceInputState { idle, starting, recording, transcribing, cancelling }

typedef VoiceTranscriber = Future<Map<String, dynamic>> Function(
  Object audio, {
  String? sessionId,
  bool raw,
});

/// Own one recording at a time, including a release while permission is open.
class VoiceInputController extends ChangeNotifier {
  VoiceInputController({
    required this.recorder,
    required this.transcribe,
    required this.onText,
    required this.onError,
  });

  final VoiceRecorder recorder;
  final VoiceTranscriber transcribe;
  final ValueChanged<String> onText;
  final ValueChanged<String> onError;

  VoiceInputState state = VoiceInputState.idle;
  bool raw = false;
  int seconds = 0;
  final List<double> levels = List.filled(20, 0, growable: true);
  String? _sessionId;
  Timer? _clock;
  Timer? _limit;
  StreamSubscription<double>? _amplitude;
  Future<void>? _starting;
  Future<void>? _cancelling;
  VoiceRecording? _recording;
  bool _finishRequested = false;
  bool _cancelRequested = false;
  bool _disposed = false;

  bool get busy => state != VoiceInputState.idle;

  Future<void> start({bool raw = false, String? sessionId}) {
    if (_disposed || busy) return Future.value();
    this.raw = raw;
    _sessionId = sessionId;
    seconds = 0;
    levels.fillRange(0, levels.length, 0);
    _finishRequested = false;
    _cancelRequested = false;
    _setState(VoiceInputState.starting);
    return _starting = _start();
  }

  Future<void> _start() async {
    try {
      if (!await recorder.hasPermission()) {
        if (!_disposed && !_cancelRequested) {
          onError('需要麦克风权限才能语音输入，请在系统设置中开启');
        }
        _setState(VoiceInputState.idle);
        return;
      }
      if (_disposed || _cancelRequested) {
        _setState(VoiceInputState.idle);
        return;
      }
      await recorder.start();
      if (_disposed || _cancelRequested) {
        await _discard();
        _setState(VoiceInputState.idle);
        return;
      }
      _setState(VoiceInputState.recording);
      _amplitude = recorder.amplitudes.listen((level) {
        if (_disposed || state != VoiceInputState.recording) return;
        levels.removeAt(0);
        levels.add(level.isFinite ? level.clamp(0.0, 1.0) : 0);
        notifyListeners();
      }, onError: (Object _) {});
      _clock = Timer.periodic(const Duration(seconds: 1), (timer) {
        seconds = timer.tick.clamp(0, 120);
        if (!_disposed) notifyListeners();
      });
      _limit = Timer(const Duration(seconds: 120), finish);
      if (_finishRequested) await finish();
    } on VoiceRecorderException catch (error) {
      await _discard();
      if (!_disposed && !_cancelRequested) onError(error.detail);
      _setState(VoiceInputState.idle);
    } catch (_) {
      await _discard();
      if (!_disposed && !_cancelRequested) onError('录音失败，请稍后重试');
      _setState(VoiceInputState.idle);
    }
  }

  Future<void> finish() {
    if (_disposed || _cancelRequested) return Future.value();
    if (state == VoiceInputState.starting) {
      _finishRequested = true;
      return Future.value();
    }
    if (state != VoiceInputState.recording) return Future.value();
    _stopUpdates();
    _setState(VoiceInputState.transcribing);
    return _finish();
  }

  Future<void> _finish() async {
    VoiceRecording? recording;
    try {
      recording = await recorder.stop();
      _recording = recording;
      if (_disposed) return;
      if (recording == null) {
        throw const AssistantApiException('没听清，请靠近麦克风再说一次');
      }
      final result = await transcribe(
        recording.audio,
        sessionId: _sessionId,
        raw: raw,
      );
      if (_disposed) return;
      final text = result['text'];
      if (text is! String || text.trim().isEmpty) {
        throw const AssistantApiException('没听清，请靠近麦克风再说一次');
      }
      onText(text);
    } on AssistantApiException catch (error) {
      if (!_disposed) onError(error.detail);
    } catch (_) {
      if (!_disposed) onError('语音识别失败，请稍后重试');
    } finally {
      try {
        if (recording != null) await recording.delete();
      } catch (_) {
        if (!_disposed) onError('录音文件清理失败，请稍后重试');
      }
      await _discard();
      if (identical(_recording, recording)) _recording = null;
      _setState(VoiceInputState.idle);
    }
  }

  Future<void> cancel() {
    if (_disposed || !busy || state == VoiceInputState.transcribing) {
      return Future.value();
    }
    _cancelRequested = true;
    _stopUpdates();
    if (state == VoiceInputState.starting) return _starting ?? Future.value();
    if (state == VoiceInputState.cancelling) {
      return _cancelling ?? Future.value();
    }
    _setState(VoiceInputState.cancelling);
    return _cancelling = _cancel();
  }

  Future<void> _cancel() async {
    await _discard();
    _setState(VoiceInputState.idle);
  }

  Future<void> _discard() async {
    try {
      await recorder.cancel();
    } catch (_) {
      // A platform interruption may have already stopped the microphone.
    }
  }

  void _setState(VoiceInputState value) {
    if (_disposed) return;
    state = value;
    notifyListeners();
  }

  void _stopUpdates() {
    _clock?.cancel();
    _limit?.cancel();
    _clock = null;
    _limit = null;
    final amplitude = _amplitude;
    _amplitude = null;
    if (amplitude != null) unawaited(amplitude.cancel());
  }

  @override
  void dispose() {
    _disposed = true;
    _cancelRequested = true;
    _stopUpdates();
    unawaited(_release());
    super.dispose();
  }

  Future<void> _release() async {
    try {
      await recorder.dispose();
    } catch (_) {}
    try {
      await _recording?.delete();
    } catch (_) {}
  }
}
