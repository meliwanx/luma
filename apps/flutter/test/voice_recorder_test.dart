import 'dart:async';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:luma_client/voice_recorder.dart';
import 'package:record/record.dart';

class _RecordPlatform extends RecordPlatform {
  final states = StreamController<RecordState>.broadcast();
  RecordConfig? config;
  String? path;
  Completer<void>? starting;
  bool allowed = true;
  bool startFails = false;
  bool stopFails = false;
  bool recording = false;
  int created = 0;
  int cancelled = 0;
  double amplitude = -30;

  @override
  Future<void> create(String recorderId) async {
    created++;
  }

  @override
  Stream<RecordState> onStateChanged(String recorderId) => states.stream;

  @override
  Future<bool> hasPermission(String recorderId, {bool request = true}) async {
    expect(request, isTrue);
    return allowed;
  }

  @override
  Future<void> start(
    String recorderId,
    RecordConfig config, {
    required String path,
  }) async {
    this.config = config;
    this.path = path;
    await File(path).writeAsBytes([1, 2, 3]);
    if (startFails) throw StateError('start failed');
    await starting?.future;
    recording = true;
    states.add(RecordState.record);
  }

  @override
  Future<String?> stop(String recorderId) async {
    if (stopFails) throw StateError('stop failed');
    recording = false;
    states.add(RecordState.stop);
    return path;
  }

  @override
  Future<void> cancel(String recorderId) async {
    cancelled++;
    recording = false;
    states.add(RecordState.stop);
  }

  @override
  Future<bool> isRecording(String recorderId) async => recording;

  @override
  Future<Amplitude> getAmplitude(String recorderId) async {
    return Amplitude(current: amplitude, max: amplitude);
  }

  @override
  Future<void> dispose(String recorderId) async {
    recording = false;
  }

  @override
  dynamic noSuchMethod(Invocation invocation) => super.noSuchMethod(invocation);
}

void main() {
  late RecordPlatform originalPlatform;
  late _RecordPlatform platform;
  late VoiceRecorder recorder;

  setUp(() {
    originalPlatform = RecordPlatform.instance;
    platform = _RecordPlatform();
    RecordPlatform.instance = platform;
    recorder = createVoiceRecorder();
  });

  tearDown(() async {
    await recorder.dispose();
    await platform.states.close();
    RecordPlatform.instance = originalPlatform;
  });

  test('recorder stays lazy until microphone access is requested', () async {
    expect(platform.created, 0);
    await recorder.dispose();
    expect(platform.created, 0);
  });

  test('permission requests are delegated to record', () async {
    platform.allowed = false;
    expect(await recorder.hasPermission(), isFalse);
    expect(platform.created, 1);
  });

  test(
    'records mono 16 kHz wav and deletes the uploaded temporary file',
    () async {
      await recorder.start();
      expect(platform.config?.encoder, AudioEncoder.wav);
      expect(platform.config?.sampleRate, 16000);
      expect(platform.config?.numChannels, 1);

      final recording = await recorder.stop();
      final file = recording!.audio as File;
      expect(await file.readAsBytes(), [1, 2, 3]);
      expect(await file.parent.exists(), isTrue);
      await recording.delete();
      await recording.delete();
      expect(await file.parent.exists(), isFalse);
    },
  );

  test('cancel removes an unfinished recording', () async {
    await recorder.start();
    final directory = File(platform.path!).parent;
    await recorder.cancel();
    expect(platform.cancelled, 1);
    expect(await directory.exists(), isFalse);
    expect(await recorder.stop(), isNull);
  });

  test('failed starts clean up their partially written audio', () async {
    platform.startFails = true;
    await expectLater(recorder.start(), throwsStateError);
    expect(platform.cancelled, 1);
    expect(await File(platform.path!).parent.exists(), isFalse);
  });

  test(
    'failed stops cancel the microphone and remove temporary audio',
    () async {
      await recorder.start();
      platform.stopFails = true;
      await expectLater(recorder.stop(), throwsStateError);
      expect(platform.cancelled, 1);
      expect(await File(platform.path!).parent.exists(), isFalse);
      expect(platform.recording, isFalse);
    },
  );

  test(
    'dispose removes stopped audio that has not yet been uploaded',
    () async {
      await recorder.start();
      final recording = await recorder.stop();
      final file = recording!.audio as File;
      await recorder.dispose();
      expect(await file.parent.exists(), isFalse);
      await recording.delete();
    },
  );

  test('cancellation waits for an in-flight start', () async {
    platform.starting = Completer<void>();
    final start = recorder.start();
    while (platform.path == null) {
      await Future<void>.delayed(Duration.zero);
    }
    final cancel = recorder.cancel();
    expect(platform.cancelled, 0);
    platform.starting!.complete();
    await start;
    await cancel;
    expect(platform.cancelled, 1);
    expect(platform.recording, isFalse);
    expect(await File(platform.path!).parent.exists(), isFalse);
  });

  test('amplitudes are normalized and bounded for the waveform', () async {
    final values = <double>[];
    final subscription = recorder.amplitudes.listen(values.add);
    await recorder.start();
    for (final amplitude in [-30.0, -100.0, 20.0, double.nan]) {
      platform.amplitude = amplitude;
      await Future<void>.delayed(const Duration(milliseconds: 160));
    }
    await subscription.cancel();
    expect(values, containsAll([0.5, 0.0, 1.0]));
    expect(values.every((value) => value >= 0 && value <= 1), isTrue);
  });
}
