import 'dart:async';
import 'dart:io';

import 'package:record/record.dart';

import 'voice_recorder.dart';

VoiceRecorder createVoiceRecorder() => _FileVoiceRecorder();

class _FileVoiceRecorder implements VoiceRecorder {
  final _amplitudes = StreamController<double>.broadcast();
  final _directories = <Directory>{};
  AudioRecorder? _recorder;
  StreamSubscription<Amplitude>? _amplitudeSubscription;
  Directory? _directory;
  Future<void> _pending = Future.value();
  bool _disposed = false;

  AudioRecorder get _native => _recorder ??= AudioRecorder();

  @override
  Stream<double> get amplitudes => _amplitudes.stream;

  // Keep cancellation/disposal behind an in-flight native start operation.
  Future<T> _enqueue<T>(Future<T> Function() operation) {
    final next = _pending.then((_) => operation());
    _pending = next.then<void>((_) {}, onError: (Object _, StackTrace _) {});
    return next;
  }

  void _checkActive() {
    if (_disposed) throw StateError('录音器已关闭');
  }

  @override
  Future<bool> hasPermission() => _enqueue(() async {
    _checkActive();
    return _native.hasPermission();
  });

  @override
  Future<void> start() => _enqueue(() async {
    _checkActive();
    if (_directory != null) throw StateError('正在录音');
    final directory = await Directory.systemTemp.createTemp('luma-voice-');
    _directory = directory;
    _directories.add(directory);
    try {
      await _native.start(
        const RecordConfig(
          encoder: AudioEncoder.wav,
          sampleRate: 16000,
          numChannels: 1,
        ),
        path: '${directory.path}${Platform.pathSeparator}audio.wav',
      );
      _amplitudeSubscription = _native
          .onAmplitudeChanged(const Duration(milliseconds: 100))
          .listen(
            (amplitude) {
              final current = amplitude.current;
              _amplitudes.add(
                current.isFinite ? ((current + 60) / 60).clamp(0.0, 1.0) : 0,
              );
            },
            onError: (Object error, StackTrace stackTrace) {
              _amplitudes.addError(error, stackTrace);
            },
          );
    } catch (_) {
      try {
        await _native.cancel();
      } catch (_) {}
      _directory = null;
      await _deleteDirectory(directory);
      rethrow;
    }
  });

  @override
  Future<VoiceRecording?> stop() => _enqueue(() async {
    final directory = _directory;
    if (directory == null) return null;
    _directory = null;
    try {
      final path = await _native.stop();
      if (path == null) {
        await _deleteDirectory(directory);
        return null;
      }
      final file = File(path);
      if (!await file.exists()) {
        await _deleteDirectory(directory);
        return null;
      }
      return VoiceRecording(file, onDelete: () => _deleteDirectory(directory));
    } catch (_) {
      try {
        await _native.cancel();
      } catch (_) {}
      await _deleteDirectory(directory);
      rethrow;
    } finally {
      await _stopAmplitude();
    }
  });

  @override
  Future<void> cancel() => _enqueue(_cancel);

  Future<void> _cancel() async {
    final directory = _directory;
    _directory = null;
    try {
      if (directory != null) await _native.cancel();
    } finally {
      await _stopAmplitude();
      if (directory != null) await _deleteDirectory(directory);
    }
  }

  Future<void> _stopAmplitude() async {
    await _amplitudeSubscription?.cancel();
    _amplitudeSubscription = null;
  }

  Future<void> _deleteDirectory(Directory directory) async {
    if (await directory.exists()) await directory.delete(recursive: true);
    _directories.remove(directory);
  }

  @override
  Future<void> dispose() {
    if (_disposed) return _pending;
    _disposed = true;
    return _enqueue(() async {
      try {
        await _cancel();
      } finally {
        try {
          await _recorder?.dispose();
        } finally {
          for (final directory in _directories.toList()) {
            await _deleteDirectory(directory);
          }
          await _amplitudes.close();
        }
      }
    });
  }
}
