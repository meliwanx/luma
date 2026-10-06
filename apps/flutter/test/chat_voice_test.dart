import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:luma_client/api.dart';
import 'package:luma_client/glass.dart';
import 'package:luma_client/theme.dart';
import 'package:luma_client/views/chat.dart';
import 'package:luma_client/voice_recorder.dart';

class _FakeRecorder implements VoiceRecorder {
  final _amplitudes = StreamController<double>.broadcast();
  final audio = Uint8List.fromList([82, 73, 70, 70, 1, 2, 3, 4]);
  bool permission = true;
  Completer<void>? startGate;
  var starts = 0;
  var stops = 0;
  var cancels = 0;
  var deletions = 0;
  var disposed = false;

  @override
  Stream<double> get amplitudes => _amplitudes.stream;

  void emitAmplitude(double level) => _amplitudes.add(level);

  @override
  Future<bool> hasPermission() async => permission;

  @override
  Future<void> start() async {
    starts++;
    await startGate?.future;
  }

  @override
  Future<VoiceRecording?> stop() async {
    stops++;
    return VoiceRecording(audio, onDelete: () async => deletions++);
  }

  @override
  Future<void> cancel() async => cancels++;

  @override
  Future<void> dispose() async {
    if (disposed) return;
    disposed = true;
    await _amplitudes.close();
  }
}

class _VoiceCall {
  _VoiceCall(this.audio, this.sessionId, this.raw);

  final Object audio;
  final String? sessionId;
  final bool raw;
}

class _VoiceApi extends AssistantApi {
  final calls = <_VoiceCall>[];
  Completer<Map<String, dynamic>>? pending;
  AssistantApiException? failure;
  Map<String, dynamic> result = {
    'text': '识别内容',
    'transcript': '识别的原话',
    'cleaned': true,
  };

  @override
  Future<Map<String, dynamic>> transcribe(
    Object audio, {
    String? sessionId,
    bool raw = false,
  }) async {
    calls.add(_VoiceCall(audio, sessionId, raw));
    if (pending != null) return pending!.future;
    if (failure != null) throw failure!;
    return result;
  }
}

class _Scenario {
  _Scenario(String text) : input = TextEditingController(text: text);

  final TextEditingController input;
  final scroll = ScrollController();
  final recorder = _FakeRecorder();
  final api = _VoiceApi();
  var sends = 0;
}

const _microphone = ValueKey('chat-voice-microphone');
const _cancel = ValueKey('chat-voice-cancel');
const _finish = ValueKey('chat-voice-finish');
const _waveform = ValueKey('chat-voice-waveform');
const _input = ValueKey('chat-input');

Future<_Scenario> _showChat(
  WidgetTester tester, {
  String text = '',
  double width = 390,
}) async {
  tester.view.physicalSize = Size(width, 844);
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  final scenario = _Scenario(text);
  addTearDown(() async {
    await tester.pumpWidget(const SizedBox());
    await _flush(tester);
    scenario.input.dispose();
    scenario.scroll.dispose();
    scenario.api.close();
    await scenario.recorder.dispose();
  });
  await tester.pumpWidget(
    MaterialApp(
      theme: lumaTheme,
      home: Scaffold(
        body: ChatView(
          messages: const [],
          tasks: const [],
          memories: const [],
          sending: false,
          input: scenario.input,
          scrollController: scenario.scroll,
          onRefresh: () async {},
          onSend: () => scenario.sends++,
          onWidgetEvent: (id, action, value) async {},
          api: scenario.api,
          sessionId: 'current-session',
          voiceRecorder: scenario.recorder,
        ),
      ),
    ),
  );
  return scenario;
}

Future<void> _flush(WidgetTester tester) async {
  await tester.pump();
  await tester.pump();
  await tester.pump();
}

Future<void> _startRecording(WidgetTester tester) async {
  await tester.tap(find.byKey(_microphone));
  await _flush(tester);
  expect(find.text('录音中'), findsOneWidget);
}

void main() {
  testWidgets(
    'recording shows time and waveform, transcribes, and inserts at the caret',
    (tester) async {
      final scenario = await _showChat(tester, text: '前文后文');
      scenario.api.pending = Completer<Map<String, dynamic>>();
      final haptics = <Object?>[];
      tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(
        SystemChannels.platform,
        (call) async {
          if (call.method == 'HapticFeedback.vibrate') {
            haptics.add(call.arguments);
          }
          return null;
        },
      );
      addTearDown(
        () => tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(
          SystemChannels.platform,
          null,
        ),
      );
      await tester.tap(find.byKey(_input));
      scenario.input.selection = const TextSelection.collapsed(offset: 2);
      await tester.pump();
      await _startRecording(tester);

      expect(scenario.recorder.starts, 1);
      expect(find.text('00:00'), findsOneWidget);
      expect(haptics, ['HapticFeedbackType.mediumImpact']);
      expect(
        find.descendant(
          of: find.byKey(const ValueKey('chat-mobile-composer')),
          matching: find.byType(GlassSurface),
        ),
        findsOneWidget,
      );
      final paintFinder = find.descendant(
        of: find.byKey(_waveform),
        matching: find.byType(CustomPaint),
      );
      final previousPainter = tester.widget<CustomPaint>(paintFinder).painter!;
      scenario.recorder.emitAmplitude(0.8);
      await _flush(tester);
      final currentPainter = tester.widget<CustomPaint>(paintFinder).painter!;
      expect(currentPainter.shouldRepaint(previousPainter), isTrue);
      await tester.pump(const Duration(seconds: 3));
      expect(find.text('00:03'), findsOneWidget);

      await tester.tap(find.byKey(_finish));
      await _flush(tester);
      expect(find.text('识别中…'), findsOneWidget);
      expect(find.byType(CircularProgressIndicator), findsOneWidget);
      expect(scenario.input.text, '前文后文');
      expect(scenario.api.calls, hasLength(1));
      final call = scenario.api.calls.single;
      expect(call.audio, same(scenario.recorder.audio));
      expect(call.sessionId, 'current-session');
      expect(call.raw, isFalse);

      scenario.api.pending!.complete(scenario.api.result);
      await _flush(tester);
      expect(scenario.input.text, '前文识别内容后文');
      expect(scenario.input.selection.baseOffset, 6);
      expect(
        tester.widget<TextField>(find.byKey(_input)).focusNode!.hasFocus,
        isTrue,
      );
      expect(scenario.sends, 0);
      expect(scenario.recorder.deletions, 1);
      expect(find.byKey(_microphone), findsOneWidget);
      expect(find.text('识别中…'), findsNothing);
    },
  );

  testWidgets('cancel discards recording without upload or changing draft', (
    tester,
  ) async {
    final scenario = await _showChat(tester, text: '保留草稿');
    await _startRecording(tester);
    await tester.tap(find.byKey(_cancel));
    await _flush(tester);

    expect(scenario.recorder.cancels, 1);
    expect(scenario.recorder.stops, 0);
    expect(scenario.api.calls, isEmpty);
    expect(scenario.input.text, '保留草稿');
    expect(find.byKey(_microphone), findsOneWidget);
    await tester.pump(const Duration(seconds: 121));
    expect(scenario.api.calls, isEmpty);
  });

  testWidgets('permission denial gives system settings guidance', (
    tester,
  ) async {
    final scenario = await _showChat(tester);
    scenario.recorder.permission = false;
    await tester.tap(find.byKey(_microphone));
    await _flush(tester);

    expect(find.text('需要麦克风权限才能语音输入，请在系统设置中开启'), findsOneWidget);
    expect(scenario.recorder.starts, 0);
    expect(scenario.api.calls, isEmpty);
    expect(find.byKey(_microphone), findsOneWidget);
    expect(scenario.sends, 0);
  });

  testWidgets(
    'server detail is shown and failed transcription can be retried',
    (tester) async {
      final scenario = await _showChat(tester, text: '草稿');
      scenario.api.failure = const AssistantApiException(
        '没听清，请靠近麦克风再说一次',
        statusCode: 422,
      );
      await _startRecording(tester);
      await tester.tap(find.byKey(_finish));
      await _flush(tester);

      expect(find.text('没听清，请靠近麦克风再说一次'), findsOneWidget);
      expect(scenario.input.text, '草稿');
      expect(scenario.recorder.deletions, 1);
      expect(find.byKey(_microphone), findsOneWidget);

      scenario.api.failure = null;
      await _startRecording(tester);
      await tester.tap(find.byKey(_finish));
      await _flush(tester);
      expect(scenario.api.calls, hasLength(2));
      expect(scenario.input.text, '草稿识别内容');
      expect(scenario.recorder.deletions, 2);
      expect(scenario.sends, 0);
    },
  );

  testWidgets('long press uses raw mode and releases into transcription', (
    tester,
  ) async {
    final scenario = await _showChat(tester);
    final gesture = await tester.startGesture(
      tester.getCenter(find.byKey(_microphone)),
    );
    await tester.pump(const Duration(milliseconds: 600));
    await _flush(tester);
    expect(find.text('原话模式'), findsOneWidget);
    expect(scenario.recorder.starts, 1);
    expect(scenario.api.calls, isEmpty);

    await gesture.up();
    await _flush(tester);
    expect(scenario.recorder.stops, 1);
    expect(scenario.api.calls.single.raw, isTrue);
    expect(scenario.input.text, '识别内容');
    expect(scenario.sends, 0);
    expect(scenario.recorder.deletions, 1);
  });

  testWidgets('releasing raw hold before native start still finishes once', (
    tester,
  ) async {
    final scenario = await _showChat(tester);
    scenario.recorder.startGate = Completer<void>();
    final gesture = await tester.startGesture(
      tester.getCenter(find.byKey(_microphone)),
    );
    await tester.pump(const Duration(milliseconds: 600));
    await _flush(tester);
    expect(find.text('原话模式 · 准备录音…'), findsOneWidget);
    await gesture.up();
    await _flush(tester);
    expect(scenario.api.calls, isEmpty);

    scenario.recorder.startGate!.complete();
    await _flush(tester);
    expect(scenario.recorder.stops, 1);
    expect(scenario.api.calls.single.raw, isTrue);
    expect(scenario.input.text, '识别内容');
    expect(scenario.recorder.deletions, 1);
  });

  testWidgets('cancelling while native start is pending never uploads', (
    tester,
  ) async {
    final scenario = await _showChat(tester, text: '保留');
    scenario.recorder.startGate = Completer<void>();
    await tester.tap(find.byKey(_microphone));
    await _flush(tester);
    await tester.tap(find.byKey(_cancel));
    await _flush(tester);
    scenario.recorder.startGate!.complete();
    await _flush(tester);

    expect(scenario.recorder.cancels, greaterThanOrEqualTo(1));
    expect(scenario.recorder.stops, 0);
    expect(scenario.api.calls, isEmpty);
    expect(scenario.input.text, '保留');
    expect(find.byKey(_microphone), findsOneWidget);
  });

  testWidgets('pausing the app cancels recording without uploading', (
    tester,
  ) async {
    final scenario = await _showChat(tester, text: '保留草稿');
    addTearDown(
      () => tester.binding.handleAppLifecycleStateChanged(
        AppLifecycleState.resumed,
      ),
    );
    await _startRecording(tester);
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.inactive);
    await _flush(tester);
    expect(find.text('录音中'), findsOneWidget);
    expect(scenario.recorder.cancels, 0);

    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.paused);
    await _flush(tester);
    expect(scenario.recorder.cancels, 1);
    expect(scenario.recorder.stops, 0);
    expect(scenario.api.calls, isEmpty);
    expect(scenario.input.text, '保留草稿');
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
    await _flush(tester);
    expect(find.byKey(_microphone), findsOneWidget);
    await tester.pump(const Duration(seconds: 121));
    expect(scenario.api.calls, isEmpty);
    expect(scenario.sends, 0);
  });

  testWidgets('120 second limit automatically stops and uploads once', (
    tester,
  ) async {
    final scenario = await _showChat(tester);
    scenario.api.pending = Completer<Map<String, dynamic>>();
    await _startRecording(tester);
    await tester.pump(const Duration(seconds: 119));
    expect(find.text('01:59'), findsOneWidget);
    expect(scenario.recorder.stops, 0);
    await tester.pump(const Duration(seconds: 1));
    await _flush(tester);

    expect(scenario.recorder.stops, 1);
    expect(scenario.api.calls, hasLength(1));
    expect(find.text('识别中…'), findsOneWidget);
    await tester.pump(const Duration(seconds: 120));
    expect(scenario.api.calls, hasLength(1));
    scenario.api.pending!.complete(scenario.api.result);
    await _flush(tester);
    expect(scenario.input.text, '识别内容');
    expect(scenario.recorder.deletions, 1);
    expect(scenario.sends, 0);
  });

  testWidgets('wide composer has the same recording and insertion actions', (
    tester,
  ) async {
    final scenario = await _showChat(tester, width: 1200, text: '桌面草稿');
    expect(find.byKey(const ValueKey('chat-mobile-composer')), findsNothing);
    expect(find.byKey(_microphone), findsOneWidget);
    await _startRecording(tester);
    expect(find.byKey(_cancel), findsOneWidget);
    expect(find.byKey(_waveform), findsOneWidget);
    await tester.tap(find.byKey(_finish));
    await _flush(tester);

    expect(scenario.input.text, '桌面草稿识别内容');
    expect(scenario.sends, 0);
    expect(scenario.recorder.deletions, 1);
    expect(
      tester.widget<TextField>(find.byKey(_input)).focusNode!.hasFocus,
      isTrue,
    );
  });

  testWidgets(
    'unmount releases audio before a pending transcription completes',
    (tester) async {
      final scenario = await _showChat(tester, text: '原草稿');
      scenario.api.pending = Completer<Map<String, dynamic>>();
      await _startRecording(tester);
      await tester.tap(find.byKey(_finish));
      await _flush(tester);
      expect(scenario.api.calls, hasLength(1));
      await tester.pumpWidget(const SizedBox());
      await _flush(tester);
      expect(scenario.api.pending!.isCompleted, isFalse);
      expect(scenario.recorder.disposed, isTrue);
      expect(scenario.recorder.deletions, 1);
      expect(scenario.input.text, '原草稿');

      scenario.api.pending!.complete(scenario.api.result);
      await _flush(tester);

      expect(scenario.input.text, '原草稿');
      expect(scenario.sends, 0);
      expect(scenario.recorder.deletions, 1);
      expect(scenario.recorder.disposed, isTrue);
      expect(tester.takeException(), isNull);
    },
  );
}
