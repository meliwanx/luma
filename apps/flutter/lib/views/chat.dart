import 'dart:async';
import 'dart:ui' show FlutterView;

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter/rendering.dart' show ScrollDirection;

import '../api.dart';
import '../browser_tools.dart';
import '../glass.dart';
import '../mcp_utils.dart';
import '../markdown.dart';
import '../preferences.dart';
import '../pseudo_markup.dart';
import '../theme.dart';
import '../voice_input.dart';
import '../voice_recorder.dart';
import 'chat_widgets.dart';
import 'browser_live.dart';
import 'file_thumbnail.dart';
import 'mobile_shell.dart' show MobileKeyboardScope;

class ChatView extends StatefulWidget {
  const ChatView({
    super.key,
    required this.messages,
    required this.tasks,
    required this.memories,
    required this.sending,
    required this.input,
    required this.scrollController,
    required this.onRefresh,
    required this.onSend,
    required this.onWidgetEvent,
    this.onFile,
    this.onLoadOlder,
    this.onStop,
    this.onRetry,
    this.onAttach,
    this.attachments = const [],
    this.onRemoveAttachment,
    this.loadingOlder = false,
    this.hasMoreMessages = false,
    this.api,
    this.sessionId,
    this.voiceRecorder,
    this.bottomOverlayHeight = 0,
    this.focusRequest = 0,
  });

  final List<Map<String, dynamic>> messages;
  final List<Map<String, dynamic>> tasks;
  final List<Map<String, dynamic>> memories;
  final bool sending;
  final TextEditingController input;
  final ScrollController scrollController;
  final Future<void> Function() onRefresh;
  final VoidCallback onSend;
  final ChatWidgetEvent onWidgetEvent;

  /// Handle a file result from a tool. The callback is optional so older
  /// callers can still render a read-only file card while adding integration.
  final Future<void> Function(Map<String, dynamic> file)? onFile;
  final Future<void> Function()? onLoadOlder;
  final VoidCallback? onStop;
  final Future<void> Function(Map<String, dynamic> message)? onRetry;
  final VoidCallback? onAttach;
  final List<Map<String, dynamic>> attachments;
  final ValueChanged<String>? onRemoveAttachment;
  final bool loadingOlder;
  final bool hasMoreMessages;
  final AssistantApi? api;
  final String? sessionId;
  final VoiceRecorder? voiceRecorder;

  /// Measured height of the mobile shell tab bar, excluding gaps/safe area.
  final double bottomOverlayHeight;
  final int focusRequest;

  @override
  State<ChatView> createState() => _ChatViewState();
}

class _ChatViewState extends State<ChatView>
    with WidgetsBindingObserver, SingleTickerProviderStateMixin {
  late final FocusNode _composerFocus;
  bool _requestingOlder = false;
  bool _nearBottom = true;
  bool _showNewMessages = false;
  double _keyboardInset = 0;
  FlutterView? _view;
  late int _lastMessageSignature;
  late bool _hadAttachments;
  late final VoiceInputController _voice;
  bool _rawHold = false;
  bool _focusAfterVoice = false;
  int _composerRevision = 0;
  final GlobalKey _composerRegionKey = GlobalKey();
  double _composerHeight = 0;
  bool _composerMeasurementScheduled = false;
  bool _followingLayout = false;
  late final AnimationController _layoutScrollAnimation;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _layoutScrollAnimation =
        AnimationController(
          vsync: this,
          duration: const Duration(milliseconds: 350),
        )..addStatusListener((status) {
          if (status != AnimationStatus.completed || !_followingLayout) return;
          _followingLayout = false;
          _scrollToBottom();
        });
    _composerFocus = FocusNode()..addListener(_focusChanged);
    _voice = VoiceInputController(
      recorder: widget.voiceRecorder ?? createVoiceRecorder(),
      transcribe: (audio, {sessionId, raw = false}) {
        final api = widget.api;
        if (api == null) {
          throw const AssistantApiException('语音输入暂不可用，请稍后重试');
        }
        return api.transcribe(audio, sessionId: sessionId, raw: raw);
      },
      onText: _insertVoiceText,
      onError: (detail) {
        if (mounted) _composerHint(detail);
      },
    )..addListener(_voiceChanged);
    widget.input.addListener(_inputChanged);
    widget.scrollController.addListener(_scrollChanged);
    _lastMessageSignature = _messageSignature();
    _hadAttachments = widget.attachments.isNotEmpty;
    _scrollToBottom();
    if (widget.focusRequest > 0) _requestComposerFocus();
  }

  @override
  void didUpdateWidget(ChatView oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.input != widget.input) {
      oldWidget.input.removeListener(_inputChanged);
      widget.input.addListener(_inputChanged);
    }
    if (oldWidget.scrollController != widget.scrollController) {
      oldWidget.scrollController.removeListener(_scrollChanged);
      widget.scrollController.addListener(_scrollChanged);
    }
    // Keep our own signature because streaming updates mutate message maps.
    // Prepending history leaves the last message unchanged.
    final signature = _messageSignature();
    if (signature != _lastMessageSignature && !_requestingOlder) {
      if (_nearBottom) {
        _scrollToBottom();
      } else {
        _showNewMessages = true;
      }
    }
    _lastMessageSignature = signature;
    final hasAttachments = widget.attachments.isNotEmpty;
    if (hasAttachments != _hadAttachments && _nearBottom) _scrollToBottom();
    _hadAttachments = hasAttachments;
    if (oldWidget.bottomOverlayHeight != widget.bottomOverlayHeight &&
        (_nearBottom || _followingLayout)) {
      _scrollToBottom(settleLayout: true);
    }
    if (oldWidget.focusRequest != widget.focusRequest) {
      _requestComposerFocus();
    }
  }

  void _requestComposerFocus() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted) _composerFocus.requestFocus();
    });
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    _view = View.of(context);
    _syncKeyboardInset();
  }

  @override
  void didChangeMetrics() => _syncKeyboardInset(rebuild: true);

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.paused ||
        state == AppLifecycleState.hidden ||
        state == AppLifecycleState.detached) {
      _cancelVoice();
    }
  }

  void _syncKeyboardInset({bool rebuild = false}) {
    final view = _view;
    if (view == null) return;
    // Scaffold removes body viewInsets when it resizes for the keyboard.
    // Read the window directly so the standalone and shell layouts agree.
    final windowInset = view.viewInsets.bottom / view.devicePixelRatio;
    final mediaInset = MediaQuery.viewInsetsOf(context).bottom;
    final inset = MobileKeyboardScope.ignoreInsetsOf(context)
        ? 0.0
        : windowInset > mediaInset
        ? windowInset
        : mediaInset;
    if (inset == _keyboardInset) return;
    _keyboardInset = inset;
    if (_nearBottom || _followingLayout) {
      _scrollToBottom(settleLayout: true);
    }
    if (rebuild && mounted) setState(() {});
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _layoutScrollAnimation.dispose();
    _voice
      ..removeListener(_voiceChanged)
      ..dispose();
    widget.input.removeListener(_inputChanged);
    widget.scrollController.removeListener(_scrollChanged);
    _composerFocus
      ..removeListener(_focusChanged)
      ..dispose();
    super.dispose();
  }

  void _focusChanged() => setState(() {});

  void _inputChanged() => setState(() {});

  void _voiceChanged() {
    if (!mounted) return;
    setState(() {});
    if (!_voice.busy && _focusAfterVoice) {
      _focusAfterVoice = false;
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted && !_voice.busy) _composerFocus.requestFocus();
      });
    }
  }

  void _insertVoiceText(String text) {
    if (!mounted) return;
    final value = widget.input.value;
    final selection = value.selection;
    final start = selection.isValid
        ? selection.start.clamp(0, value.text.length)
        : value.text.length;
    final end = selection.isValid
        ? selection.end.clamp(start, value.text.length)
        : start;
    widget.input.value = TextEditingValue(
      text: value.text.replaceRange(start, end, text),
      selection: TextSelection.collapsed(offset: start + text.length),
    );
    _focusAfterVoice = true;
  }

  void _startVoice({bool raw = false}) {
    if (widget.sending || _voice.busy) return;
    if (widget.api == null) {
      _rawHold = false;
      _composerHint('语音输入暂不可用，请稍后重试');
      return;
    }
    HapticFeedback.mediumImpact();
    unawaited(_voice.start(raw: raw, sessionId: widget.sessionId));
  }

  void _releaseVoice() {
    if (!_rawHold) return;
    _rawHold = false;
    unawaited(_voice.finish());
  }

  void _cancelVoice() {
    _rawHold = false;
    unawaited(_voice.cancel());
  }

  int _messageSignature() {
    if (widget.messages.isEmpty) return 0;
    final last = widget.messages.last;
    return Object.hash(
      last['id'],
      last['content'],
      last['status'],
      last['streaming'],
      '${last['tool_states']}',
      '${last['metadata']}',
    );
  }

  void _scrollToBottom({bool animated = false, bool settleLayout = false}) {
    _nearBottom = true;
    if (settleLayout) {
      _followingLayout = true;
      // Keyboard and shell transitions can continue after the first layout.
      _layoutScrollAnimation.forward(from: 0);
    }
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted ||
          !widget.scrollController.hasClients ||
          (!_nearBottom && !_followingLayout)) {
        return;
      }
      final extent = widget.scrollController.position.maxScrollExtent;
      if (animated) {
        widget.scrollController
            .animateTo(
              extent,
              duration: const Duration(milliseconds: 240),
              curve: Curves.easeOutCubic,
            )
            .whenComplete(() {
              if (mounted && _nearBottom) _scrollToBottom();
            });
      } else {
        widget.scrollController.jumpTo(extent);
      }
      if (_showNewMessages) setState(() => _showNewMessages = false);
    });
  }

  void _measureComposerAfterLayout() {
    if (_composerMeasurementScheduled) return;
    _composerMeasurementScheduled = true;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      _composerMeasurementScheduled = false;
      if (!mounted) return;
      final box = _composerRegionKey.currentContext?.findRenderObject();
      if (box is! RenderBox || !box.hasSize) return;
      final height = box.size.height;
      if ((height - _composerHeight).abs() < .5) return;
      final follow = _nearBottom || _followingLayout;
      setState(() => _composerHeight = height);
      if (follow) _scrollToBottom();
    });
  }

  double _composerBottom(BuildContext context) {
    if (_keyboardInset > 0 && !MobileKeyboardScope.ignoreInsetsOf(context)) {
      return 8;
    }
    final overlay = widget.bottomOverlayHeight;
    return MediaQuery.paddingOf(context).bottom +
        overlay +
        (overlay > 0 ? 16 : 8);
  }

  void _scrollChanged() {
    if (widget.scrollController.hasClients) {
      _nearBottom = widget.scrollController.position.extentAfter < 120;
      if (!_nearBottom && _followingLayout) {
        _followingLayout = false;
        _layoutScrollAnimation.stop();
      }
      if (_nearBottom && _showNewMessages) {
        setState(() => _showNewMessages = false);
      }
    }
    if (_requestingOlder ||
        !widget.hasMoreMessages ||
        widget.onLoadOlder == null) {
      return;
    }
    if (!widget.scrollController.hasClients ||
        widget.scrollController.position.pixels > 72) {
      return;
    }
    _requestingOlder = true;
    final beforeExtent = widget.scrollController.position.maxScrollExtent;
    final beforeOffset = widget.scrollController.offset;
    widget.onLoadOlder!().whenComplete(() {
      if (!mounted) return;
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted && widget.scrollController.hasClients) {
          final delta =
              widget.scrollController.position.maxScrollExtent - beforeExtent;
          final target = (beforeOffset + delta)
              .clamp(0.0, widget.scrollController.position.maxScrollExtent)
              .toDouble();
          widget.scrollController.jumpTo(target);
        }
        if (mounted) setState(() => _requestingOlder = false);
      });
    });
  }

  bool get _hasConversation => widget.sending || widget.messages.isNotEmpty;

  @override
  Widget build(BuildContext context) {
    final mobile =
        MediaQuery.sizeOf(context).width < MuseMetrics.mobileBreakpoint;
    final composerBottom = _composerBottom(context);
    if (mobile) _measureComposerAfterLayout();
    return GestureDetector(
      behavior: HitTestBehavior.translucent,
      onTap: () => FocusScope.of(context).unfocus(),
      child: mobile
          ? Stack(
              fit: StackFit.expand,
              children: [
                _messageList(context),
                if (_showNewMessages)
                  Positioned(
                    right: 20,
                    bottom: composerBottom + _composerHeight + 12,
                    child: _newMessagesButton(context),
                  ),
                Positioned(
                  left: 16,
                  right: 16,
                  bottom: composerBottom,
                  child: NotificationListener<SizeChangedLayoutNotification>(
                    onNotification: (_) {
                      _measureComposerAfterLayout();
                      return false;
                    },
                    child: SizeChangedLayoutNotifier(
                      child: Column(
                        key: _composerRegionKey,
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          if (widget.attachments.isNotEmpty) ...[
                            _attachmentDrafts(context),
                            const SizedBox(height: 8),
                          ],
                          _mobileComposer(context),
                        ],
                      ),
                    ),
                  ),
                ),
              ],
            )
          : Column(
              children: [
                Expanded(
                  child: Stack(
                    children: [
                      _messageList(context),
                      if (_showNewMessages)
                        Positioned(
                          right: 24,
                          bottom: 12,
                          child: _newMessagesButton(context),
                        ),
                    ],
                  ),
                ),
                if (widget.attachments.isNotEmpty)
                  Padding(
                    padding: const EdgeInsets.fromLTRB(24, 0, 24, 8),
                    child: _attachmentDrafts(context),
                  ),
                _composer(context),
              ],
            ),
    );
  }

  Widget _newMessagesButton(BuildContext context) => GlassSurface(
    shape: GlassShape.capsule,
    child: TextButton(
      key: const ValueKey('chat-new-messages'),
      onPressed: () => _scrollToBottom(animated: true),
      style: TextButton.styleFrom(
        minimumSize: const Size(112, 44),
        foregroundColor: context.muse.text,
        padding: const EdgeInsets.symmetric(horizontal: 14),
      ),
      child: const Text('↓ 新消息'),
    ),
  );

  Widget _messageList(BuildContext context) {
    final mobile =
        MediaQuery.sizeOf(context).width < MuseMetrics.mobileBreakpoint;
    final horizontal = mobile ? 16.0 : 24.0;
    final top = mobile ? MediaQuery.paddingOf(context).top + 130 : 12.0;
    final bottom = mobile
        ? _composerBottom(context) + _composerHeight + 12
        : 20.0;
    return RefreshIndicator(
      onRefresh: widget.onRefresh,
      color: context.muse.accent,
      backgroundColor: context.muse.bubble,
      child: LayoutBuilder(
        builder: (context, constraints) {
          return NotificationListener<UserScrollNotification>(
            onNotification: (notification) {
              if (notification.direction != ScrollDirection.idle) {
                _followingLayout = false;
                _layoutScrollAnimation.stop();
                _nearBottom =
                    widget.scrollController.position.extentAfter < 120;
              }
              return false;
            },
            child: Listener(
              onPointerDown: (_) => FocusScope.of(context).unfocus(),
              child: ListView(
                key: const ValueKey('chat-message-list'),
                controller: widget.scrollController,
                keyboardDismissBehavior:
                    ScrollViewKeyboardDismissBehavior.onDrag,
                physics: const AlwaysScrollableScrollPhysics(),
                padding: EdgeInsets.fromLTRB(
                  horizontal,
                  top,
                  horizontal,
                  bottom,
                ),
                children: [
                  if (widget.loadingOlder || _requestingOlder)
                    Padding(
                      padding: const EdgeInsets.only(bottom: 8),
                      child: Center(
                        child: Text(
                          '正在加载更早消息…',
                          style: TextStyle(
                            color: context.muse.muted,
                            fontSize: 12,
                          ),
                        ),
                      ),
                    ),
                  Center(
                    child: SizedBox(
                      width: constraints.maxWidth < MuseMetrics.chatColumn
                          ? constraints.maxWidth
                          : MuseMetrics.chatColumn,
                      child: _hasConversation
                          ? _conversation(context)
                          : SizedBox(
                              height: constraints.maxHeight > top + bottom
                                  ? constraints.maxHeight - top - bottom
                                  : 0,
                              child: _emptyState(context),
                            ),
                    ),
                  ),
                ],
              ),
            ),
          );
        },
      ),
    );
  }

  Widget _conversation(BuildContext context) {
    final children = <Widget>[];
    DateTime? previous;
    for (var index = 0; index < widget.messages.length; index++) {
      final message = widget.messages[index];
      final created = _createdAt(message);
      if (created != null &&
          previous != null &&
          (_differentDay(previous, created) ||
              created.difference(previous).inMinutes > 5)) {
        children.add(_timeSeparator(context, created));
      }
      children.add(
        _message(
          context,
          message,
          generating: widget.sending && index == widget.messages.length - 1,
        ),
      );
      if (created != null) previous = created;
    }
    if (widget.sending &&
        (widget.messages.isEmpty ||
            widget.messages.last['role'] != 'assistant')) {
      children.add(_thinkingBubble(context, const {}));
    }
    return Column(children: children);
  }

  Widget _emptyState(BuildContext context) {
    final pending = widget.tasks
        .where((task) => task['status'] != 'done')
        .length;
    return Center(
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 16),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Text(
              '早上好，我是 Luma',
              textAlign: TextAlign.center,
              style: TextStyle(
                color: context.muse.text,
                fontSize: 24,
                fontWeight: FontWeight.w600,
              ),
            ),
            const SizedBox(height: 8),
            Text(
              pending > 0
                  ? '今天有 $pending 项待处理任务。你想先处理哪一件事？'
                  : '我已经看过今天的安排。你想先处理哪一件事？',
              textAlign: TextAlign.center,
              style: TextStyle(
                color: context.muse.muted,
                fontSize: 14,
                height: 1.5,
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _timeSeparator(BuildContext context, DateTime time) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 8),
      child: Center(
        child: Text(
          _timeLabel(time),
          style: TextStyle(color: context.muse.muted, fontSize: 12),
        ),
      ),
    );
  }

  Widget _message(
    BuildContext context,
    Map<String, dynamic> message, {
    bool generating = false,
  }) {
    final mobile =
        MediaQuery.sizeOf(context).width < MuseMetrics.mobileBreakpoint;
    final user = message['role'] == 'user';
    final status = '${message['status'] ?? ''}';
    final streaming =
        status == 'streaming' ||
        message['streaming'] == true ||
        (generating && !user && status != 'error' && status != 'incomplete');
    final incomplete = status == 'incomplete';
    final failed = status == 'error';
    final content = displaySecretReferences('${message['content'] ?? ''}');
    final error = message['error'];
    if (!user && streaming && _onlyLoadingContent(message, content)) {
      return _thinkingBubble(context, message);
    }
    final display = content;
    final metadata = message['metadata'];
    final widgetEvent =
        user && metadata is Map && metadata['widget_event'] is Map;
    final textColor = user
        ? widgetEvent
              ? context.muse.muted
              : Colors.white
        : streaming || failed || incomplete
        ? context.muse.muted
        : context.muse.text;
    final bubble = Container(
      padding: EdgeInsets.symmetric(
        horizontal: widgetEvent ? 12 : 14,
        vertical: widgetEvent ? 6 : 10,
      ),
      decoration: BoxDecoration(
        color: widgetEvent
            ? context.muse.chip
            : user
            ? context.muse.accent
            : context.muse.bubble,
        borderRadius: BorderRadius.circular(
          mobile
              ? 22
              : widgetEvent
              ? 99
              : user
              ? MuseMetrics.userBubbleRadius
              : MuseMetrics.bubbleRadius,
        ),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          if (!user) ..._messageLabels(context, metadata),
          if (user)
            _messageText(
              context,
              display,
              style: TextStyle(
                color: textColor,
                fontSize: widgetEvent ? 12 : context.messageFontSize,
                height: 1.55,
              ),
            )
          else
            ..._assistantParts(context, message, display, streaming),
          if (user) ..._messageFiles(context, metadata),
          if (error is String && error.isNotEmpty) ...[
            const SizedBox(height: 5),
            Text(
              error,
              style: TextStyle(
                color: failed ? context.muse.danger : context.muse.warn,
                fontSize: 12,
                height: 1.4,
              ),
            ),
          ],
          if (!user && incomplete)
            Wrap(
              spacing: 8,
              crossAxisAlignment: WrapCrossAlignment.center,
              children: [
                _messageStatus(context, '已中断', context.muse.warn),
                if (error == '生成可能已中断' && widget.onRetry != null)
                  TextButton(
                    onPressed: () => widget.onRetry!(message),
                    style: TextButton.styleFrom(
                      minimumSize: const Size(44, 44),
                      padding: const EdgeInsets.symmetric(horizontal: 8),
                    ),
                    child: const Text('重试'),
                  ),
              ],
            ),
          if (!user && failed)
            Wrap(
              spacing: 8,
              crossAxisAlignment: WrapCrossAlignment.center,
              children: [
                _messageStatus(context, '生成失败', context.muse.danger),
                if (widget.onRetry != null)
                  TextButton(
                    onPressed: () => widget.onRetry!(message),
                    style: TextButton.styleFrom(
                      minimumSize: const Size(44, 44),
                      padding: const EdgeInsets.symmetric(horizontal: 8),
                    ),
                    child: const Text('重试'),
                  ),
              ],
            ),
        ],
      ),
    );
    final progress = user ? null : _toolProgress(context, message);
    return Padding(
      padding: const EdgeInsets.only(bottom: 14),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            mainAxisAlignment: user
                ? MainAxisAlignment.end
                : MainAxisAlignment.start,
            children: [
              Flexible(
                child: LayoutBuilder(
                  builder: (context, constraints) => ConstrainedBox(
                    constraints: BoxConstraints(
                      minWidth: mobile && !user ? constraints.maxWidth : 0,
                      maxWidth:
                          constraints.maxWidth *
                          (user
                              ? 0.75
                              : mobile
                              ? 1
                              : 0.85),
                    ),
                    child: GestureDetector(
                      onLongPress: content.isEmpty
                          ? null
                          : () => _copyMessageMenu(context, content),
                      child: bubble,
                    ),
                  ),
                ),
              ),
            ],
          ),
          ?progress,
        ],
      ),
    );
  }

  List<Widget> _messageLabels(BuildContext context, Object? metadata) {
    if (metadata is! Map) return const [];
    final proactive = metadata['proactive'];
    final fromFeed = metadata['feed_post_id'] != null;
    if (proactive == null && !fromFeed) return const [];
    final reason = proactive is Map ? '${proactive['reason'] ?? ''}' : '';
    return [
      Padding(
        padding: const EdgeInsets.only(bottom: 6),
        child: Wrap(
          spacing: 6,
          children: [
            if (proactive != null)
              ActionChip(
                label: const Text('主动'),
                visualDensity: VisualDensity.compact,
                labelStyle: TextStyle(color: context.muse.muted, fontSize: 11),
                onPressed: () => showDialog<void>(
                  context: context,
                  builder: (dialog) => AlertDialog(
                    title: const Text('为什么发给你'),
                    content: Text(
                      reason.isEmpty ? 'Luma 想主动帮你跟进关心的事。' : reason,
                    ),
                    actions: [
                      TextButton(
                        onPressed: () => Navigator.of(dialog).pop(),
                        child: const Text('知道了'),
                      ),
                    ],
                  ),
                ),
              ),
            if (fromFeed)
              Chip(
                label: const Text('来自动态'),
                visualDensity: VisualDensity.compact,
                labelStyle: TextStyle(color: context.muse.muted, fontSize: 11),
              ),
          ],
        ),
      ),
    ];
  }

  bool _onlyLoadingContent(Map<String, dynamic> message, String content) {
    final metadata = message['metadata'];
    if (_messageToolEvents(message).any(
      (call) => const {
        'file',
        'browser_live',
        'preview',
        'sandbox_job',
        'sandbox',
      }.contains(call['kind']),
    )) {
      return false;
    }
    final widgets = metadata is Map ? metadata['widgets'] : null;
    for (final part in parseMessageContent(content, streaming: true)) {
      if (part.type == MessageContentPartType.text) return false;
      if (part.type == MessageContentPartType.widget &&
          widgets is List &&
          widgets.any((item) => item is Map && item['id'] == part.value)) {
        return false;
      }
    }
    return true;
  }

  Widget _thinkingBubble(BuildContext context, Map<String, dynamic> message) {
    final progress = _toolProgress(context, message);
    return Padding(
      padding: const EdgeInsets.only(bottom: 14),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          ..._messageLabels(context, message['metadata']),
          Container(
            key: const ValueKey('chat-thinking-bubble'),
            padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
            decoration: BoxDecoration(
              color: context.muse.bubble,
              borderRadius: BorderRadius.circular(MuseMetrics.bubbleRadius),
            ),
            child: const _ThinkingDots(),
          ),
          ?progress,
        ],
      ),
    );
  }

  Widget? _toolProgress(BuildContext context, Map<String, dynamic> message) {
    for (final event in _messageToolEvents(message).reversed) {
      if (event['status'] != 'running') continue;
      final connector = '${event['connector'] ?? '工具'}';
      final title = '${event['title'] ?? event['tool'] ?? ''}';
      return Padding(
        padding: const EdgeInsets.only(top: 5, left: 4),
        child: Text(
          displaySecretReferences(
            browserProgressLabel('${event['tool'] ?? title}') ??
                '正在查询 $connector${title.isEmpty ? '' : ' · $title'}…',
          ),
          key: const ValueKey('chat-tool-progress'),
          style: TextStyle(
            color: context.muse.muted,
            fontSize: 11,
            height: 1.4,
          ),
        ),
      );
    }
    return null;
  }

  Widget _messageText(
    BuildContext context,
    String value, {
    required TextStyle style,
  }) => LumaMarkdown(
    data: value,
    style: style,
    linkColor: context.muse.link,
    selectable:
        MediaQuery.sizeOf(context).width >= MuseMetrics.mobileBreakpoint,
  );

  Future<void> _copyMessageMenu(BuildContext context, String content) async {
    final copy = await showModalBottomSheet<bool>(
      context: context,
      backgroundColor: context.muse.bubble,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(24)),
      ),
      builder: (context) => SafeArea(
        child: ListTile(
          minTileHeight: 56,
          leading: const Icon(Icons.copy_outlined),
          title: const Text('复制'),
          onTap: () => Navigator.of(context).pop(true),
        ),
      ),
    );
    if (copy != true) return;
    await Clipboard.setData(ClipboardData(text: content));
    if (!mounted) return;
    ScaffoldMessenger.of(this.context).showSnackBar(
      const SnackBar(content: Text('已复制'), duration: Duration(seconds: 2)),
    );
  }

  Widget _messageStatus(BuildContext context, String label, Color color) =>
      Text(label, style: TextStyle(color: color, fontSize: 12, height: 1.4));

  List<Widget> _messageFiles(BuildContext context, Object? metadata) {
    final files = metadata is Map ? metadata['files'] : null;
    if (files is! List) return const [];
    return [
      for (final file in files)
        if (file is Map)
          Padding(
            padding: const EdgeInsets.only(top: 8),
            child: _fileCard(context, {
              'file_id': file['file_id'] ?? file['id'] ?? '',
              'filename': file['filename'] ?? file['name'] ?? '附件',
              'media_type': file['media_type'] ?? file['mime_type'] ?? '',
              if (file['size_bytes'] != null) 'size_bytes': file['size_bytes'],
            }),
          ),
    ];
  }

  List<Map<String, dynamic>> _messageToolEvents(Map<String, dynamic> message) {
    final metadata = message['metadata'];
    final sources = [
      message['tool_states'],
      if (metadata is Map) metadata['tool_calls'],
      if (metadata is Map) metadata['tool_events'],
    ];
    final events = <Map<String, dynamic>>[];
    final indices = <String, int>{};
    for (final source in sources) {
      if (source is! List) continue;
      for (final item in source) {
        if (item is! Map) continue;
        final call = Map<String, dynamic>.from(item);
        final nested = call['payload'] is Map
            ? Map<String, dynamic>.from(call['payload'] as Map)
            : call['data'] is Map
            ? Map<String, dynamic>.from(call['data'] as Map)
            : const <String, dynamic>{};
        final event = <String, dynamic>{
          ...nested,
          ...call,
          'kind': nested['kind'] ?? call['kind'],
        };
        final callId = '${call['call_id'] ?? ''}';
        final fileId = event['kind'] == 'file'
            ? '${event['file_id'] ?? ''}'
            : '';
        final key = callId.isNotEmpty
            ? 'call:$callId'
            : fileId.isNotEmpty
            ? 'file:$fileId'
            : '';
        final index = key.isEmpty ? null : indices[key];
        if (index != null) {
          // A persisted result can add the file to an earlier progress row.
          if (event['kind'] == 'file' && events[index]['kind'] != 'file') {
            events[index] = event;
          }
          continue;
        }
        if (key.isNotEmpty) indices[key] = events.length;
        events.add(event);
      }
    }
    return events;
  }

  List<Widget> _assistantParts(
    BuildContext context,
    Map<String, dynamic> message,
    String content,
    bool streaming,
  ) {
    final metadata = message['metadata'];
    final rawWidgets = metadata is Map ? metadata['widgets'] : null;
    final widgets = <String, Map<String, dynamic>>{};
    if (rawWidgets is List) {
      for (final item in rawWidgets) {
        if (item is Map && item['id'] is String) {
          widgets[item['id'] as String] = Map<String, dynamic>.from(item);
        }
      }
    }
    final parts = <Widget>[];
    for (final event in _messageToolEvents(message)) {
      final kind = '${event['kind'] ?? ''}';
      if (kind == 'browser_live') {
        parts.add(
          BrowserLiveCard(
            url: '${event['url'] ?? ''}',
            expiresIn: int.tryParse('${event['expires_in'] ?? ''}'),
          ),
        );
        continue;
      }
      if (kind == 'sandbox') {
        parts.add(_sandboxCard(context, event));
        continue;
      }
      if (kind == 'file') {
        parts.add(_fileCard(context, event));
        continue;
      }
      if (kind == 'preview') {
        parts.add(_previewCard(context, event));
        continue;
      }
      if (kind == 'sandbox_job') {
        parts.add(_sandboxJobCard(context, event));
        continue;
      }
      final title = '${event['title'] ?? event['tool'] ?? '外部工具'}';
      final connector = '${event['connector'] ?? '连接器'}';
      final status = '${event['status'] ?? ''}';
      final label = switch (status) {
        'needs_confirmation' => '$title 需要你确认',
        'error' || 'failed' => '$connector · $title 调用失败',
        _ => '',
      };
      if (label.isEmpty) continue;
      parts.add(
        Padding(
          padding: const EdgeInsets.only(bottom: 7),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(
                status == 'error' || status == 'failed'
                    ? Icons.error_outline
                    : status == 'needs_confirmation'
                    ? Icons.help_outline
                    : Icons.circle,
                size: 13,
                color: status == 'error' || status == 'failed'
                    ? context.muse.danger
                    : status == 'needs_confirmation'
                    ? context.muse.warn
                    : context.muse.muted,
              ),
              const SizedBox(width: 6),
              Flexible(
                child: Text(
                  label,
                  style: TextStyle(color: context.muse.muted, fontSize: 12),
                ),
              ),
            ],
          ),
        ),
      );
    }
    void addText(String text) {
      if (text.isEmpty || text.trim().isEmpty) return;
      parts.add(
        _messageText(
          context,
          text,
          style: TextStyle(
            color: streaming ? context.muse.muted : context.muse.text,
            fontSize: context.messageFontSize,
            height: 1.55,
          ),
        ),
      );
    }

    for (final part in parseMessageContent(content, streaming: streaming)) {
      switch (part.type) {
        case MessageContentPartType.text:
          addText(part.value);
        case MessageContentPartType.widget:
          final widgetId = part.value;
          final data = widgets[widgetId];
          if (data != null) {
            parts.add(
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 6),
                child: ChatWidgetView(
                  key: ValueKey(widgetId),
                  widget: data,
                  onEvent: widget.onWidgetEvent,
                  disabled: widget.sending,
                ),
              ),
            );
          }
        case MessageContentPartType.loading:
          // A pending structured fragment shares the message's loading state.
          // Once visible content arrives, keep it in place without a second loader.
          break;
      }
    }
    return parts;
  }

  /// Sandbox events are untrusted tool output. Keep every field in plain
  /// text widgets so model-provided markup and URLs can never become UI.
  Widget _sandboxCard(BuildContext context, Map<String, dynamic> event) {
    final language = '${event['language'] ?? event['lang'] ?? 'text'}';
    final code = '${event['code'] ?? ''}';
    final output = '${event['output'] ?? ''}';
    final exitCode = event['exit_code'] ?? event['exitCode'];
    final exitLabel = exitCode == null ? '未知' : '$exitCode';
    Widget row(String label, String value) => Padding(
      padding: const EdgeInsets.only(top: 5),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            label,
            style: TextStyle(color: context.muse.muted, fontSize: 11),
          ),
          const SizedBox(height: 2),
          SelectableText(
            value,
            style: TextStyle(
              color: context.muse.text,
              fontSize: 12,
              height: 1.4,
              fontFamily: 'monospace',
            ),
          ),
        ],
      ),
    );
    return Container(
      width: double.infinity,
      margin: const EdgeInsets.only(bottom: 8),
      padding: const EdgeInsets.all(10),
      decoration: BoxDecoration(
        color: context.muse.chip,
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: context.muse.line),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            '沙箱工具',
            style: TextStyle(
              color: context.muse.text,
              fontSize: 12,
              fontWeight: FontWeight.w600,
            ),
          ),
          row('语言', language),
          row('代码', code),
          row('输出', output),
          row('退出码', exitLabel),
        ],
      ),
    );
  }

  Future<void> _openFile(Map<String, dynamic> event) async {
    final callback = widget.onFile;
    if (callback == null) return;
    try {
      await callback(event);
    } catch (error) {
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text('文件打开失败：$error')));
    }
  }

  Widget _fileCard(BuildContext context, Map<String, dynamic> event) {
    final filename = '${event['filename'] ?? event['name'] ?? '导出文件'}';
    final fileId = '${event['file_id'] ?? event['fileId'] ?? ''}'.trim();
    final mediaType = '${event['media_type'] ?? event['mime_type'] ?? ''}'
        .trim();
    final size = event['size_bytes'] ?? event['size'];
    final details = <String>[
      if (mediaType.isNotEmpty) mediaType,
      if (size != null) '$size bytes',
    ];
    final canOpen = widget.onFile != null && fileId.isNotEmpty;
    return Container(
      width: double.infinity,
      margin: const EdgeInsets.only(bottom: 8),
      decoration: BoxDecoration(
        color: context.muse.chip,
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: context.muse.line),
      ),
      child: Material(
        type: MaterialType.transparency,
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            ListTile(
              dense: true,
              contentPadding: const EdgeInsets.symmetric(
                horizontal: 10,
                vertical: 2,
              ),
              leading: Icon(
                Icons.insert_drive_file_outlined,
                color: context.muse.accent,
              ),
              title: Text(filename, style: TextStyle(color: context.muse.text)),
              subtitle: details.isEmpty
                  ? Text(
                      fileId.isEmpty ? '文件标识不可用' : '文件已就绪',
                      style: TextStyle(color: context.muse.muted, fontSize: 12),
                    )
                  : Text(
                      details.join(' · '),
                      style: TextStyle(color: context.muse.muted, fontSize: 12),
                    ),
              trailing: canOpen
                  ? const Icon(Icons.open_in_new, size: 18)
                  : const Icon(Icons.lock_outline, size: 17),
              onTap: canOpen ? () => _openFile(event) : null,
            ),
            if (isImageFile(mediaType) &&
                fileId.isNotEmpty &&
                widget.api != null)
              FileThumbnail(
                api: widget.api!,
                fileId: fileId,
                filename: filename,
              ),
          ],
        ),
      ),
    );
  }

  bool _isHttpsUrl(String value) {
    final uri = Uri.tryParse(value.trim());
    return uri != null &&
        uri.scheme.toLowerCase() == 'https' &&
        uri.host.isNotEmpty;
  }

  Future<void> _copyPreviewUrl(String url) async {
    await Clipboard.setData(ClipboardData(text: url));
    if (!mounted) return;
    ScaffoldMessenger.of(context)
        .showSnackBar(const SnackBar(content: Text('链接已复制')));
  }

  Widget _previewCard(BuildContext context, Map<String, dynamic> event) {
    final url = '${event['url'] ?? ''}'.trim();
    final valid = _isHttpsUrl(url);
    final note = '${event['note'] ?? ''}'.trim();
    final isPublic = event['public'] == true;
    const publicHint = '拿到链接的人在沙箱运行期间都能访问';
    return Container(
      width: double.infinity,
      margin: const EdgeInsets.only(bottom: 8),
      padding: const EdgeInsets.all(10),
      decoration: BoxDecoration(
        color: context.muse.chip,
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: context.muse.line),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            '预览链接',
            style: TextStyle(
              color: context.muse.text,
              fontWeight: FontWeight.w600,
              fontSize: 12,
            ),
          ),
          const SizedBox(height: 5),
          Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Expanded(
                child: SelectableText(
                  url.isEmpty ? '链接不可用' : url,
                  style: TextStyle(
                    color: valid ? context.muse.accent : context.muse.muted,
                    fontSize: 12,
                    height: 1.4,
                  ),
                ),
              ),
              if (valid)
                IconButton(
                  onPressed: () => _copyPreviewUrl(url),
                  tooltip: '复制链接',
                  icon: const Icon(Icons.copy_outlined, size: 17),
                  constraints: const BoxConstraints(
                    minWidth: 44,
                    minHeight: 44,
                  ),
                  padding: EdgeInsets.zero,
                ),
            ],
          ),
          if (note.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(top: 3),
              child: Text(
                note,
                style: TextStyle(color: context.muse.muted, fontSize: 11),
              ),
            ),
          if (isPublic && !note.contains(publicHint))
            Padding(
              padding: const EdgeInsets.only(top: 3),
              child: Text(
                publicHint,
                style: TextStyle(color: context.muse.warn, fontSize: 11),
              ),
            ),
          if (!valid && url.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(top: 3),
              child: Text(
                '仅支持 HTTPS 链接',
                style: TextStyle(color: context.muse.warn, fontSize: 11),
              ),
            ),
        ],
      ),
    );
  }

  Widget _sandboxJobCard(BuildContext context, Map<String, dynamic> event) {
    final status = '${event['status'] ?? 'unknown'}';
    final jobId = '${event['job_id'] ?? event['jobId'] ?? ''}';
    final command = '${event['command'] ?? ''}';
    final exitCode = event['exit_code'] ?? event['exitCode'];
    final stdout = '${event['stdout_tail'] ?? ''}';
    final stderr = '${event['stderr_tail'] ?? ''}';
    final statusColor = status == 'failed' || status == 'error'
        ? context.muse.danger
        : status == 'done' || status == 'completed'
        ? context.muse.green
        : context.muse.muted;
    return Container(
      width: double.infinity,
      margin: const EdgeInsets.only(bottom: 8),
      padding: const EdgeInsets.all(10),
      decoration: BoxDecoration(
        color: context.muse.chip,
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: context.muse.line),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Icon(
                Icons.work_history_outlined,
                size: 16,
                color: context.muse.accent,
              ),
              const SizedBox(width: 6),
              Text(
                '沙箱任务',
                style: TextStyle(
                  color: context.muse.text,
                  fontWeight: FontWeight.w600,
                  fontSize: 12,
                ),
              ),
              const Spacer(),
              Text(status, style: TextStyle(color: statusColor, fontSize: 12)),
            ],
          ),
          if (jobId.isNotEmpty) _plainToolRow(context, '任务 ID', jobId),
          if (command.isNotEmpty) _plainToolRow(context, '命令', command),
          if (exitCode != null) _plainToolRow(context, '退出码', '$exitCode'),
          if (stdout.isNotEmpty) _plainToolRow(context, '输出', stdout),
          if (stderr.isNotEmpty) _plainToolRow(context, '错误', stderr),
        ],
      ),
    );
  }

  Widget _plainToolRow(BuildContext context, String label, String value) =>
      Padding(
        padding: const EdgeInsets.only(top: 5),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              label,
              style: TextStyle(color: context.muse.muted, fontSize: 11),
            ),
            const SizedBox(height: 2),
            SelectableText(
              value,
              style: TextStyle(
                color: context.muse.text,
                fontSize: 12,
                height: 1.4,
                fontFamily: 'monospace',
              ),
            ),
          ],
        ),
      );

  void _sendMessage() {
    if (widget.sending || _voice.busy || widget.input.text.trim().isEmpty) {
      return;
    }
    HapticFeedback.lightImpact();
    if (containsSecretJson(widget.input.text)) {
      // Recreate EditableText so its private undo history cannot restore the
      // submitted configuration after the controller has been cleared.
      setState(() => _composerRevision++);
    }
    widget.onSend();
    if (MediaQuery.sizeOf(context).width < MuseMetrics.mobileBreakpoint) {
      _composerFocus.unfocus();
    } else {
      _composerFocus.requestFocus();
    }
    _scrollToBottom(settleLayout: true);
  }

  Widget _composerInput(Widget child) =>
      KeyedSubtree(key: ValueKey(_composerRevision), child: child);

  void _composerHint(String message) {
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(content: Text(message), duration: const Duration(seconds: 2)),
    );
  }

  Widget _attachmentDrafts(BuildContext context) => SizedBox(
    key: const ValueKey('chat-attachment-drafts'),
    height: 44,
    child: ListView.separated(
      scrollDirection: Axis.horizontal,
      itemCount: widget.attachments.length,
      separatorBuilder: (_, index) => const SizedBox(width: 8),
      itemBuilder: (context, index) {
        final file = widget.attachments[index];
        final id = '${file['file_id'] ?? file['id'] ?? ''}';
        final filename = '${file['filename'] ?? file['name'] ?? '附件'}';
        return GlassSurface(
          shape: GlassShape.capsule,
          padding: const EdgeInsets.only(left: 12),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(
                Icons.insert_drive_file_outlined,
                size: 20,
                color: context.muse.text,
              ),
              const SizedBox(width: 8),
              ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: 180),
                child: Text(
                  filename,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(color: context.muse.text, fontSize: 13),
                ),
              ),
              SizedBox(
                width: 44,
                height: 44,
                child: IconButton(
                  key: ValueKey('chat-remove-attachment-$id'),
                  tooltip: '移除附件 $filename',
                  padding: EdgeInsets.zero,
                  icon: Icon(Icons.close, size: 18, color: context.muse.text),
                  onPressed:
                      widget.sending ||
                          id.isEmpty ||
                          widget.onRemoveAttachment == null
                      ? null
                      : () => widget.onRemoveAttachment!(id),
                ),
              ),
            ],
          ),
        );
      },
    ),
  );

  Widget _mobileComposer(BuildContext context) {
    final hasText = widget.input.text.trim().isNotEmpty;
    return SizedBox(
      key: const ValueKey('chat-mobile-composer'),
      height: 52,
      child: Listener(
        onPointerUp: (_) => _releaseVoice(),
        onPointerCancel: (_) {
          if (_rawHold) _cancelVoice();
        },
        child: GlassSurface(
          shape: GlassShape.capsule,
          padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 4),
          child: _voice.busy
              ? _voiceComposerContent(context)
              : Row(
                  children: [
                    SizedBox(
                      width: 44,
                      height: 44,
                      child: IconButton(
                        tooltip: '附件',
                        onPressed: widget.sending
                            ? null
                            : widget.onAttach ??
                                  () => _composerHint('附件上传即将上线'),
                        padding: EdgeInsets.zero,
                        icon: Icon(
                          Icons.add,
                          color: context.muse.text,
                          size: 25,
                        ),
                      ),
                    ),
                    Expanded(
                      child: _composerInput(
                        TextField(
                          key: const ValueKey('chat-input'),
                          controller: widget.input,
                          focusNode: _composerFocus,
                          textInputAction: TextInputAction.send,
                          onEditingComplete: () {},
                          onSubmitted: (_) => _sendMessage(),
                          style: TextStyle(
                            color: context.muse.text,
                            fontSize: 15,
                          ),
                          decoration: InputDecoration(
                            constraints: const BoxConstraints(minHeight: 44),
                            hintText: '发消息',
                            hintStyle: TextStyle(color: context.muse.muted),
                            border: InputBorder.none,
                            filled: false,
                            isDense: true,
                            contentPadding: const EdgeInsets.symmetric(
                              horizontal: 6,
                              vertical: 11,
                            ),
                          ),
                        ),
                      ),
                    ),
                    _microphoneButton(context),
                    if (widget.sending || hasText)
                      SizedBox(
                        width: 44,
                        height: 44,
                        child: IconButton(
                          key: const ValueKey('chat-composer-action'),
                          tooltip: widget.sending ? '停止' : '发送',
                          padding: EdgeInsets.zero,
                          onPressed: widget.sending
                              ? widget.onStop
                              : _sendMessage,
                          icon: Icon(
                            widget.sending
                                ? Icons.stop_rounded
                                : Icons.arrow_upward_rounded,
                            color: context.muse.text,
                            size: 24,
                          ),
                        ),
                      ),
                  ],
                ),
        ),
      ),
    );
  }

  Widget _composer(BuildContext context) {
    final horizontal = MediaQuery.sizeOf(context).width < 640 ? 12.0 : 24.0;
    final available = MediaQuery.sizeOf(context).width - horizontal * 2;
    final composerWidth = available < MuseMetrics.composerWidth
        ? available
        : MuseMetrics.composerWidth;
    final hasText = widget.input.text.trim().isNotEmpty;
    return Padding(
      padding: EdgeInsets.fromLTRB(
        horizontal,
        4,
        horizontal,
        _keyboardInset > 0 ? 8 : 16,
      ),
      child: Center(
        child: SizedBox(
          width: composerWidth,
          child: Container(
            constraints: const BoxConstraints(
              minHeight: MuseMetrics.composerHeight,
            ),
            padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 6),
            decoration: BoxDecoration(
              color: context.muse.composer,
              borderRadius: BorderRadius.circular(26),
              border: Border.all(
                color: _composerFocus.hasFocus
                    ? context.muse.focus
                    : context.muse.composerLine,
              ),
            ),
            child: Listener(
              onPointerUp: (_) => _releaseVoice(),
              onPointerCancel: (_) {
                if (_rawHold) _cancelVoice();
              },
              child: _voice.busy
                  ? _voiceComposerContent(context)
                  : Row(
                      crossAxisAlignment: CrossAxisAlignment.end,
                      children: [
                        Expanded(
                          child: _composerInput(
                            TextField(
                              key: const ValueKey('chat-input'),
                              controller: widget.input,
                              focusNode: _composerFocus,
                              minLines: 1,
                              maxLines: 6,
                              textInputAction: TextInputAction.send,
                              onEditingComplete: () {},
                              onSubmitted: (_) => _sendMessage(),
                              style: TextStyle(
                                color: context.muse.text,
                                fontSize: 15,
                                height: 1.4,
                              ),
                              decoration: InputDecoration(
                                hintText: '消息',
                                hintStyle: TextStyle(
                                  color: context.muse.muted,
                                  fontSize: 15,
                                ),
                                border: InputBorder.none,
                                isDense: true,
                                contentPadding: const EdgeInsets.symmetric(
                                  horizontal: 8,
                                  vertical: 8,
                                ),
                              ),
                            ),
                          ),
                        ),
                        const SizedBox(width: 4),
                        _microphoneButton(context),
                        if (widget.sending)
                          SizedBox(
                            height: 36,
                            child: TextButton.icon(
                              onPressed: widget.onStop,
                              icon: const Icon(Icons.stop, size: 17),
                              label: const Text('停止'),
                              style: TextButton.styleFrom(
                                foregroundColor: Colors.white,
                                backgroundColor: context.muse.accent,
                                padding: const EdgeInsets.symmetric(
                                  horizontal: 10,
                                ),
                                shape: const StadiumBorder(),
                              ),
                            ),
                          )
                        else
                          SizedBox(
                            width: 36,
                            height: 36,
                            child: IconButton(
                              onPressed: hasText ? _sendMessage : null,
                              padding: EdgeInsets.zero,
                              icon: hasText
                                  ? const Icon(Icons.arrow_upward, size: 19)
                                  : const SizedBox.shrink(),
                              color: Colors.white,
                              tooltip: '发送',
                              style: IconButton.styleFrom(
                                backgroundColor: hasText
                                    ? context.muse.accent
                                    : context.muse.chip,
                                disabledBackgroundColor: context.muse.chip,
                                disabledForegroundColor: context.muse.faint,
                                shape: const CircleBorder(),
                              ),
                            ),
                          ),
                      ],
                    ),
            ),
          ),
        ),
      ),
    );
  }

  Widget _microphoneButton(BuildContext context) => Tooltip(
    message: '语音输入',
    triggerMode: TooltipTriggerMode.manual,
    child: Semantics(
      hint: '长按使用原话模式，松手完成录音',
      child: GestureDetector(
        onLongPressStart: widget.sending
            ? null
            : (_) {
                _rawHold = true;
                _startVoice(raw: true);
              },
        child: SizedBox(
          width: 44,
          height: 44,
          child: IconButton(
            key: const ValueKey('chat-voice-microphone'),
            onPressed: widget.sending ? null : () => _startVoice(),
            padding: EdgeInsets.zero,
            icon: Icon(
              Icons.mic_none_rounded,
              color: widget.sending ? context.muse.faint : context.muse.text,
              size: 24,
            ),
          ),
        ),
      ),
    ),
  );

  Widget _voiceComposerContent(BuildContext context) {
    final recording = _voice.state == VoiceInputState.recording;
    final canFinish = recording || _voice.state == VoiceInputState.starting;
    final canCancel = canFinish;
    final elapsed =
        '${(_voice.seconds ~/ 60).toString().padLeft(2, '0')}:'
        '${(_voice.seconds % 60).toString().padLeft(2, '0')}';
    return Row(
      children: [
        SizedBox(
          width: 44,
          height: 44,
          child: canCancel
              ? IconButton(
                  key: const ValueKey('chat-voice-cancel'),
                  tooltip: '取消录音',
                  padding: EdgeInsets.zero,
                  onPressed: _cancelVoice,
                  icon: Icon(Icons.close, color: context.muse.text),
                )
              : null,
        ),
        Expanded(
          child: recording
              ? Row(
                  children: [
                    Icon(Icons.circle, size: 8, color: context.muse.danger),
                    const SizedBox(width: 8),
                    Column(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        Text(
                          _voice.raw ? '原话模式' : '录音中',
                          style: TextStyle(
                            color: context.muse.text,
                            fontSize: 12,
                          ),
                        ),
                        Text(
                          elapsed,
                          style: TextStyle(
                            color: context.muse.muted,
                            fontSize: 12,
                          ),
                        ),
                      ],
                    ),
                    const SizedBox(width: 12),
                    Expanded(
                      child: Semantics(
                        label: '录音音量',
                        child: SizedBox(
                          key: const ValueKey('chat-voice-waveform'),
                          height: 28,
                          child: CustomPaint(
                            painter: _VoiceWaveform(
                              List.of(_voice.levels),
                              context.muse.danger,
                            ),
                          ),
                        ),
                      ),
                    ),
                  ],
                )
              : Row(
                  mainAxisAlignment: MainAxisAlignment.center,
                  children: [
                    SizedBox(
                      width: 16,
                      height: 16,
                      child: CircularProgressIndicator(
                        strokeWidth: 2,
                        color: context.muse.text,
                      ),
                    ),
                    const SizedBox(width: 8),
                    Text(
                      _voice.state == VoiceInputState.transcribing
                          ? '识别中…'
                          : _voice.state == VoiceInputState.cancelling
                          ? '取消中…'
                          : _voice.raw
                          ? '原话模式 · 准备录音…'
                          : '准备录音…',
                      style: TextStyle(color: context.muse.text, fontSize: 13),
                    ),
                  ],
                ),
        ),
        SizedBox(
          width: 44,
          height: 44,
          child: canFinish
              ? IconButton(
                  key: const ValueKey('chat-voice-finish'),
                  tooltip: '完成录音',
                  padding: EdgeInsets.zero,
                  onPressed: () {
                    _rawHold = false;
                    unawaited(_voice.finish());
                  },
                  icon: Icon(Icons.check, color: context.muse.text),
                )
              : null,
        ),
      ],
    );
  }

  DateTime? _createdAt(Map<String, dynamic> message) {
    final raw = message['created_at'] ?? message['createdAt'];
    if (raw is! String) return null;
    return DateTime.tryParse(raw)?.toLocal();
  }

  bool _differentDay(DateTime a, DateTime b) =>
      a.year != b.year || a.month != b.month || a.day != b.day;

  String _timeLabel(DateTime value) {
    final now = DateTime.now();
    final clock = '${value.hour}:${value.minute.toString().padLeft(2, '0')}';
    if (value.year == now.year &&
        value.month == now.month &&
        value.day == now.day) {
      return '今天 $clock';
    }
    final yesterday = now.subtract(const Duration(days: 1));
    if (value.year == yesterday.year &&
        value.month == yesterday.month &&
        value.day == yesterday.day) {
      return '昨天 $clock';
    }
    return '${value.month}月${value.day}日 $clock';
  }
}

class _VoiceWaveform extends CustomPainter {
  const _VoiceWaveform(this.levels, this.color);

  final List<double> levels;
  final Color color;

  @override
  void paint(Canvas canvas, Size size) {
    final step = size.width / levels.length;
    final paint = Paint()
      ..color = color
      ..strokeWidth = 3
      ..strokeCap = StrokeCap.round;
    for (var index = 0; index < levels.length; index++) {
      final height = 3 + levels[index] * (size.height - 3);
      final x = (index + .5) * step;
      canvas.drawLine(
        Offset(x, (size.height - height) / 2),
        Offset(x, (size.height + height) / 2),
        paint,
      );
    }
  }

  @override
  bool shouldRepaint(_VoiceWaveform oldDelegate) =>
      color != oldDelegate.color || !listEquals(levels, oldDelegate.levels);
}

class _ThinkingDots extends StatefulWidget {
  const _ThinkingDots();

  @override
  State<_ThinkingDots> createState() => _ThinkingDotsState();
}

class _ThinkingDotsState extends State<_ThinkingDots>
    with SingleTickerProviderStateMixin {
  late final AnimationController _animation;

  @override
  void initState() {
    super.initState();
    _animation = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 1200),
    )..repeat();
  }

  @override
  void dispose() {
    _animation.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => Semantics(
    label: 'Luma 正在思考',
    container: true,
    child: AnimatedBuilder(
      animation: _animation,
      builder: (context, _) => SizedBox(
        width: 32,
        height: 14,
        child: Row(
          mainAxisAlignment: MainAxisAlignment.spaceBetween,
          children: List.generate(3, (index) {
            final phase = (_animation.value - index * .16) % 1;
            final pulse = phase < .5 ? 1 - (phase * 4 - 1).abs() : 0.0;
            return Transform.translate(
              offset: Offset(0, -4 * pulse),
              child: Opacity(
                opacity: .4 + .6 * pulse,
                child: Container(
                  width: 6,
                  height: 6,
                  decoration: BoxDecoration(
                    shape: BoxShape.circle,
                    color: context.muse.muted,
                  ),
                ),
              ),
            );
          }),
        ),
      ),
    ),
  );
}
