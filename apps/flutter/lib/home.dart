import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'api.dart';
import 'browser_tools.dart';
import 'brand.dart';
import 'glass.dart';
import 'file_export.dart';
import 'theme.dart';
import 'views/chat.dart';
import 'views/feed.dart';
import 'views/ideas.dart';
import 'views/library.dart';
import 'views/attachment_picker.dart';
import 'views/mobile_shell.dart';
import 'views/search.dart';
import 'views/notifications.dart';
import 'mcp_utils.dart';
import 'views/connectors.dart';
import 'views/missions.dart';
import 'views/settings.dart';
import 'views/session_picker.dart';
import 'views/file_result.dart';

class _ResumeAttempt {
  int rounds = 0;
  DateTime? lastAttemptAt;
}

class _LocalGeneration {
  _LocalGeneration(this.sessionId, {this.messageId});

  final String sessionId;
  String? messageId;
  bool detached = false;
  bool stopRequested = false;
  bool cancelRequested = false;
  StreamSubscription<AssistantSseEvent>? subscription;
  Completer<bool>? ended;
}

class _MobileChatScrollBehavior extends MaterialScrollBehavior {
  const _MobileChatScrollBehavior({
    required this.controller,
    required this.headerReduction,
  });

  final ScrollController controller;
  final double headerReduction;

  @override
  Widget buildOverscrollIndicator(
    BuildContext context,
    Widget child,
    ScrollableDetails details,
  ) {
    final viewport = super.buildOverscrollIndicator(context, child, details);
    if (details.controller != controller) return viewport;
    // ChatView reserves the old two-row header. Extend only its message
    // viewport upward so the composer and refresh indicator stay in place.
    return Stack(
      fit: StackFit.expand,
      children: [
        Positioned(
          top: -headerReduction,
          left: 0,
          right: 0,
          bottom: 0,
          child: viewport,
        ),
      ],
    );
  }

  @override
  bool shouldNotify(covariant _MobileChatScrollBehavior oldDelegate) =>
      controller != oldDelegate.controller ||
      headerReduction != oldDelegate.headerReduction;
}

const _maxResumeConnections = 3;

DateTime _generationNow() => DateTime.now();

Duration _defaultResumeBackoff(int attempt) =>
    Duration(milliseconds: 250 * attempt);

Map<String, dynamic> _mergeMessageMetadata(Object? previous, Map incoming) => {
  if (previous is Map) ...Map<String, dynamic>.from(previous),
  ...Map<String, dynamic>.from(incoming),
};

class LumaHome extends StatefulWidget {
  const LumaHome({
    super.key,
    this.token,
    this.onLogout,
    this.api,
    this.resumeBackoff = _defaultResumeBackoff,
    this.generationNow = _generationNow,
  });

  final String? token;
  final VoidCallback? onLogout;
  final AssistantApi? api;
  @visibleForTesting
  final Duration Function(int attempt) resumeBackoff;
  @visibleForTesting
  final DateTime Function() generationNow;

  @override
  State<LumaHome> createState() => _LumaHomeState();
}

class _LumaHomeState extends State<LumaHome> with WidgetsBindingObserver {
  late final AssistantApi api;
  final input = TextEditingController();
  final _messagesScrollController = ScrollController();
  _LocalGeneration? _localGeneration;
  final Map<String, _LocalGeneration> _pendingStarts = {};
  StreamSubscription<AssistantSseEvent>? _notificationSubscription;
  Timer? _notificationReconnectTimer;
  String? _lastNotificationId;
  bool _notificationConnecting = false;
  bool _notificationUnavailableNotified = false;
  bool _foreground = true;
  Timer? _refreshTimer;
  bool _loadingRemote = false;
  Completer<void>? _remoteLoadComplete;
  int _loadGeneration = 0;
  int tab = 0;

  /// The session selected by the user. A null value means the durable main
  /// chat; polling must never replace a non-null selection with whichever
  /// session was updated most recently on another client.
  String? _selectedSessionId;
  String sessionId = 'ses_default';
  List<Map<String, dynamic>> _sessions = [];
  List<Map<String, dynamic>> _draftFiles = [];
  final _notificationItems = ValueNotifier<List<Map<String, dynamic>>>([]);
  List<Map<String, dynamic>> get _notifications => _notificationItems.value;
  set _notifications(List<Map<String, dynamic>> items) =>
      _notificationItems.value = items;
  final Map<String, String> _generatingMessages = {};
  final Map<String, DateTime> _generationDeadlines = {};
  final Map<String, String> _generationCursors = {};
  final Map<String, Map<String, dynamic>> _generationSnapshots = {};
  final Set<String> _completedSessions = {};
  bool _pollingGenerations = false;
  double _tabBarHeight = 0;
  bool get sending => _generatingMessages.containsKey(sessionId);
  bool loading = true;
  List<Map<String, dynamic>> messages = [];
  List<Map<String, dynamic>> tasks = [];
  List<Map<String, dynamic>> memories = [];
  List<Map<String, dynamic>> runtimeJobs = [];
  List<Map<String, dynamic>> approvals = [];
  int _composerFocusRequest = 0;
  bool _hasMoreMessages = false;
  bool _loadingOlderMessages = false;
  String? _oldestMessageId;
  final Map<String, _ResumeAttempt> _resumeAttempts = {};

  @override
  void initState() {
    super.initState();
    api =
        widget.api ??
        AssistantApi(token: widget.token, onUnauthorized: widget.onLogout);
    WidgetsBinding.instance.addObserver(this);
    _load(initial: true);
    _connectNotifications();
    _loadNotifications();
    // Cloud generation outlives the selected session and its SSE subscription.
    _refreshTimer = Timer.periodic(const Duration(seconds: 5), (_) {
      if (mounted) {
        unawaited(_pollBackgroundGenerations());
        unawaited(_load());
      }
    });
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    _foreground = state == AppLifecycleState.resumed;
    if (_foreground && mounted) {
      _pollBackgroundGenerations();
      _load();
      _connectNotifications();
    } else {
      _disconnectNotifications();
    }
  }

  Future<void> _connectNotifications() async {
    if (!_foreground ||
        !mounted ||
        api.token == null ||
        api.token!.isEmpty ||
        _notificationSubscription != null ||
        _notificationConnecting) {
      return;
    }
    _notificationReconnectTimer?.cancel();
    _notificationReconnectTimer = null;
    _notificationConnecting = true;
    try {
      final stream = await api.notificationStream(
        lastEventId: _lastNotificationId,
      );
      if (!mounted || !_foreground) return;
      _notificationUnavailableNotified = false;
      late final StreamSubscription<AssistantSseEvent> subscription;
      subscription = stream.listen(
        _handleNotification,
        onError: (Object error, StackTrace _) {
          _reportNotificationError(error);
          if (identical(_notificationSubscription, subscription)) {
            _notificationSubscription = null;
          }
          _scheduleNotificationReconnect();
        },
        onDone: () {
          if (identical(_notificationSubscription, subscription)) {
            _notificationSubscription = null;
          }
          _scheduleNotificationReconnect();
        },
        cancelOnError: true,
      );
      _notificationSubscription = subscription;
    } catch (error) {
      _reportNotificationError(error);
      if (error is! AssistantApiException || error.statusCode != 401) {
        _scheduleNotificationReconnect();
      }
    } finally {
      _notificationConnecting = false;
    }
  }

  void _reportNotificationError(Object error) {
    if (!mounted ||
        !_foreground ||
        _notificationUnavailableNotified ||
        error is! AssistantApiException ||
        !error.isUnavailable) {
      return;
    }
    _notificationUnavailableNotified = true;
    ScaffoldMessenger.of(context)
        .showSnackBar(const SnackBar(content: Text('服务暂时不可用')));
  }

  void _handleNotification(AssistantSseEvent event) {
    if (!mounted || event.name != 'notification') return;
    final id = event.id ?? event.data['id'];
    if (id is String && id.isNotEmpty) {
      _lastNotificationId = id;
      setState(() {
        _notifications = mergeNotifications(_notifications, [
          {...event.data, 'id': id},
        ]);
      });
    }
    final title = event.data['title'];
    final body = event.data['body'];
    final text = [
      title,
      body,
    ].whereType<String>().where((value) => value.trim().isNotEmpty).join('：');
    if (text.isNotEmpty) {
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(text)));
    }
    _pollBackgroundGenerations();
    _load();
  }

  Future<void> _loadNotifications() async {
    if (api.token == null || api.token!.isEmpty) return;
    try {
      final items = await api.listNotifications();
      if (!mounted) return;
      setState(
        () => _notifications = mergeNotifications(_notifications, items),
      );
    } catch (_) {
      // The list page offers retry; notification streaming remains independent.
    }
  }

  Future<void> _openNotifications() => Navigator.of(context).push<void>(
    MaterialPageRoute(
      builder: (_) => NotificationsView(
        api: api,
        notifications: _notifications,
        updates: _notificationItems,
        onChanged: (items) {
          if (mounted) {
            setState(
              () => _notifications = mergeNotifications(_notifications, items),
            );
          }
        },
      ),
    ),
  );

  Future<void> _openSearch() => Navigator.of(context).push<void>(
    MaterialPageRoute(
      builder: (_) => SearchView(
        api: api,
        sessions: _sessions,
        onSelectSession: (id) => unawaited(_selectSession(id)),
      ),
    ),
  );

  Future<void> _newSideChat() async {
    final session = await _createSideSession();
    final id = session?['id'];
    if (id is String && mounted) await _selectSession(id);
  }

  Future<void> _attachFile() async {
    if (sending) return;
    final selectedSession = sessionId;
    FocusScope.of(context).unfocus();
    HapticFeedback.selectionClick();
    final file = await showModalBottomSheet<Map<String, dynamic>>(
      context: context,
      isScrollControlled: true,
      showDragHandle: true,
      backgroundColor: context.muse.bg,
      builder: (_) => AttachmentPicker(api: api),
    );
    if (!mounted || sending || sessionId != selectedSession || file == null) {
      return;
    }
    final id = file['id'];
    if (id is! String || id.isEmpty) return;
    setState(() {
      _draftFiles = [
        ..._draftFiles.where((item) => item['id'] != id),
        {
          'id': id,
          'filename': file['filename'],
          'media_type': file['media_type'],
          'size_bytes': file['size_bytes'],
        },
      ];
    });
  }

  void _scheduleNotificationReconnect() {
    if (!_foreground || !mounted || _notificationReconnectTimer != null) return;
    _notificationReconnectTimer = Timer(const Duration(seconds: 5), () {
      _notificationReconnectTimer = null;
      _connectNotifications();
    });
  }

  void _disconnectNotifications() {
    _notificationReconnectTimer?.cancel();
    _notificationReconnectTimer = null;
    _notificationUnavailableNotified = false;
    _notificationSubscription?.cancel();
    _notificationSubscription = null;
  }

  bool _isGenerating(Map<String, dynamic> message) =>
      message['status'] == 'streaming' || message['status'] == 'pending';

  void _trackGeneration(String id, String messageId) {
    _generatingMessages[id] = messageId;
    _generationDeadlines.putIfAbsent(
      id,
      () => widget.generationNow().add(const Duration(minutes: 10)),
    );
    _completedSessions.remove(id);
  }

  void _finishGeneration(
    String id,
    String messageId,
    Map<String, dynamic> message,
  ) {
    if (_generatingMessages[id] != messageId) return;
    setState(() {
      _generatingMessages.remove(id);
      _generationDeadlines.remove(id);
      _generationCursors.remove(messageId);
      _generationSnapshots.remove(messageId);
      if (id != sessionId && message['status'] == 'complete') {
        _completedSessions.add(id);
      }
    });
  }

  void _detachLocalGeneration() {
    final connection = _localGeneration;
    if (connection == null) return;
    connection.detached = true;
    _localGeneration = null;
    _generationDeadlines[connection.sessionId] = widget.generationNow().add(
      const Duration(minutes: 10),
    );
    // Before the POST start event, retain only enough of the local stream to
    // learn the durable message id. Never call the server cancellation API.
    if (connection.messageId != null) {
      connection.subscription?.cancel();
      final ended = connection.ended;
      if (ended != null && !ended.isCompleted) ended.complete(false);
    }
  }

  Future<void> _pollBackgroundGenerations() async {
    if (_pollingGenerations || !mounted) return;
    _pollingGenerations = true;
    try {
      for (final entry in _generatingMessages.entries.toList()) {
        if (!mounted) return;
        if (_localGeneration?.sessionId == entry.key) continue;
        final deadline = _generationDeadlines[entry.key];
        if (deadline != null && !widget.generationNow().isBefore(deadline)) {
          setState(() {
            _generatingMessages.remove(entry.key);
            _generationDeadlines.remove(entry.key);
          });
          continue;
        }
        try {
          final page = await api.listMessages(entry.key, limit: 100);
          if (!mounted || _generatingMessages[entry.key] != entry.value) {
            continue;
          }
          for (final message in page.messages.reversed) {
            if (message['id'] == entry.value && !_isGenerating(message)) {
              _finishGeneration(entry.key, entry.value, message);
              if (entry.key == sessionId) unawaited(_load());
              break;
            }
          }
        } on AssistantApiException catch (error) {
          if (error.statusCode == 404 && mounted) {
            setState(() {
              _generatingMessages.remove(entry.key);
              _generationDeadlines.remove(entry.key);
            });
          }
        } catch (_) {
          // Transient network errors retry on the next bounded polling tick.
        }
      }
    } finally {
      _pollingGenerations = false;
    }
  }

  Future<void> _load({bool initial = false, bool forceResume = false}) async {
    if (_loadingRemote || _localGeneration?.sessionId == sessionId) return;
    _loadingRemote = true;
    _remoteLoadComplete = Completer<void>();
    final generation = ++_loadGeneration;
    final requestedSessionAtStart = _selectedSessionId;
    Map<String, dynamic>? data;
    AssistantApiException? loadError;
    Map<String, dynamic>? resumeCandidate;
    try {
      data = await api.dashboard(selectedSessionId: _selectedSessionId);
    } on AssistantApiException catch (error) {
      loadError = error;
    } catch (_) {
      loadError = const AssistantApiException('会话加载失败，请稍后重试');
    }
    if (!mounted ||
        generation != _loadGeneration ||
        requestedSessionAtStart != _selectedSessionId) {
      _finishRemoteLoad();
      if (mounted && requestedSessionAtStart != _selectedSessionId) {
        unawaited(_load(initial: true));
      }
      return;
    }
    if (_localGeneration?.sessionId == sessionId) {
      _finishRemoteLoad();
      return;
    }
    final remoteData = data;
    if (remoteData != null) {
      final remoteMessages = _maps(remoteData['messages']);
      final remoteSessionId = remoteData['conversation_id'] as String?;
      final remoteSessions = _maps(remoteData['sessions']);
      final requestedSessionId = _selectedSessionId;
      // A requested session that disappeared is deliberately allowed to fall
      // back to the server's main chat. Otherwise retain the user's choice
      // even if another client has since written a different session.
      final selectionFallback =
          remoteData['selection_fallback'] == true ||
          (requestedSessionId != null &&
              remoteSessionId != requestedSessionId &&
              remoteSessions.isNotEmpty &&
              remoteSessions.every((item) => item['id'] != requestedSessionId));
      if (remoteSessionId != null &&
          (requestedSessionId == null || selectionFallback)) {
        _selectedSessionId = remoteSessionId;
      }
      final effectiveSessionId = _selectedSessionId ?? remoteSessionId;
      final sessionsForState = selectionFallback && requestedSessionId != null
          ? remoteSessions
                .where((item) => item['id'] != requestedSessionId)
                .toList()
          : remoteSessions;
      if (effectiveSessionId != null && effectiveSessionId != sessionId) {
        _resumeAttempts.clear();
      }
      if (effectiveSessionId == sessionId) {
        final previous = {
          for (final message in messages) message['id']: message,
        };
        for (final message in remoteMessages) {
          final states = liveBrowserStates(
            previous[message['id']]?['tool_states'],
          );
          if (states.isNotEmpty) message['tool_states'] = states;
        }
      }
      setState(() {
        sessionId = effectiveSessionId ?? sessionId;
        _sessions = sessionsForState;
        messages = remoteMessages;
        _hasMoreMessages = remoteData['messages_has_more'] == true;
        _oldestMessageId = remoteData['messages_oldest_id'] is String
            ? remoteData['messages_oldest_id'] as String
            : (remoteMessages.isNotEmpty
                  ? '${remoteMessages.first['id'] ?? ''}'
                  : null);
        tasks = _maps(remoteData['tasks']);
        memories = _maps(remoteData['memories']);
        runtimeJobs = _maps(remoteData['runtime_jobs']);
        approvals = _maps(remoteData['approvals']);
        loading = false;
      });
      for (final message in remoteMessages.reversed) {
        if (message['role'] == 'assistant') {
          if (_isGenerating(message) && message['id'] is String) {
            resumeCandidate = message;
          }
          break;
        }
      }
      final trackedId = _generatingMessages[sessionId];
      if (trackedId != null) {
        for (final message in remoteMessages) {
          if (message['id'] == trackedId && !_isGenerating(message)) {
            _finishGeneration(sessionId, trackedId, message);
            break;
          }
        }
      }
    } else if (initial) {
      setState(() {
        messages = [];
        loading = false;
      });
    }
    if (loadError != null && mounted) {
      setState(() => loading = false);
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(
            loadError.isUnavailable ? '服务暂时不可用' : loadError.message,
          ),
        ),
      );
    }
    _finishRemoteLoad();
    final candidate = resumeCandidate;
    if (candidate != null) {
      final candidateId = candidate['id'];
      if (candidateId is String &&
          candidateId.isNotEmpty &&
          mounted &&
          _localGeneration == null) {
        if (_resumeAllowed(candidateId, force: forceResume)) {
          _recordResumeAttempt(candidateId);
          unawaited(_resumeStreamingMessage(candidate));
        } else {
          _markResumeUnavailable(candidateId);
        }
      }
    }
  }

  void _finishRemoteLoad() {
    _loadingRemote = false;
    final completed = _remoteLoadComplete;
    _remoteLoadComplete = null;
    if (completed != null && !completed.isCompleted) completed.complete();
  }

  bool _resumeAllowed(String messageId, {bool force = false}) {
    if (force) return true;
    final attempt = _resumeAttempts[messageId];
    final lastAttemptAt = attempt?.lastAttemptAt;
    return lastAttemptAt == null ||
        DateTime.now().difference(lastAttemptAt) >= const Duration(seconds: 60);
  }

  void _recordResumeAttempt(String messageId) {
    final attempt = _resumeAttempts.putIfAbsent(messageId, _ResumeAttempt.new);
    attempt.rounds++;
    attempt.lastAttemptAt = DateTime.now();
  }

  void _markResumeUnavailable(String messageId) {
    if (!mounted) return;
    final index = messages.indexWhere((item) => item['id'] == messageId);
    if (index < 0) return;
    setState(() {
      messages[index]['status'] = 'incomplete';
      messages[index]['streaming'] = false;
      messages[index]['error'] = '生成可能已中断';
    });
  }

  Future<void> _resumeStreamingMessage(Map<String, dynamic> source) async {
    if (_localGeneration != null) return;
    final messageId = source['id'];
    if (messageId is! String || messageId.isEmpty) return;
    final connection = _LocalGeneration(sessionId, messageId: messageId);
    _localGeneration = connection;
    setState(() => _trackGeneration(connection.sessionId, messageId));
    final snapshot = _generationSnapshots[messageId] ?? source;
    final persistedContent = '${snapshot['content'] ?? ''}';
    var content = persistedContent;
    var replaceOnFirstDelta = true;
    var hasReceivedDelta = false;
    var lastEventId = _generationCursors[messageId] ?? '';
    var retries = 0;
    var terminal = false;
    var streamError = '';
    final toolStates = <String, Map<String, dynamic>>{};
    StreamSubscription<AssistantSseEvent>? subscription;

    int messageIndex() =>
        messages.indexWhere((item) => item['id'] == messageId);
    void update(void Function(Map<String, dynamic>) apply) {
      final index = messageIndex();
      if (!mounted ||
          connection.detached ||
          sessionId != connection.sessionId ||
          index < 0) {
        return;
      }
      setState(() => apply(messages[index]));
      _generationSnapshots[messageId] = Map<String, dynamic>.from(
        messages[index],
      );
      _scrollToBottom();
    }

    void handle(AssistantSseEvent event) {
      if (connection.detached || !mounted) return;
      if (event.id != null && event.id!.isNotEmpty) {
        lastEventId = event.id!;
        _generationCursors[messageId] = lastEventId;
      }
      if (event.name == 'delta') {
        final delta = event.data['content'];
        if (delta is String && delta.isNotEmpty) {
          if (replaceOnFirstDelta) {
            content = delta;
            replaceOnFirstDelta = false;
          } else {
            if (!hasReceivedDelta && content.isEmpty) {
              content = persistedContent;
            }
            content += delta;
          }
          hasReceivedDelta = true;
          update((message) => message['content'] = content);
        }
      } else if (event.name == 'tool') {
        final callId = '${event.data['call_id'] ?? event.data['id'] ?? ''}';
        if (callId.isNotEmpty) {
          toolStates[callId] = Map<String, dynamic>.from(event.data);
          update(
            (message) => message['tool_states'] = toolStates.values
                .map((item) => Map<String, dynamic>.from(item))
                .toList(),
          );
        }
      } else if (event.name == 'status') {
        final status = '${event.data['status'] ?? 'streaming'}';
        update((message) {
          message['status'] = status;
          message['streaming'] = status == 'streaming' || status == 'pending';
        });
      } else if (event.name == 'error') {
        final value = event.data['message'] ?? event.data['detail'];
        streamError = value is String && value.isNotEmpty ? value : '流式服务返回了错误';
        terminal = true;
        update((message) {
          message['status'] = 'error';
          message['streaming'] = false;
          message['error'] = streamError;
        });
      } else if (event.name == 'done') {
        final doneContent = event.data['content'];
        if (doneContent is String) {
          content = doneContent;
        } else if (!hasReceivedDelta) {
          content = persistedContent;
        }
        final status = '${event.data['status'] ?? 'complete'}';
        terminal = true;
        _resumeAttempts.remove(messageId);
        update((message) {
          message['content'] = content;
          message['status'] = status == 'streaming' ? 'complete' : status;
          message['streaming'] = false;
          if (event.data['metadata'] is Map) {
            message['metadata'] = _mergeMessageMetadata(
              message['metadata'],
              event.data['metadata'] as Map,
            );
          }
        });
      }
    }

    Future<void> consume(Stream<AssistantSseEvent> stream) async {
      final ended = Completer<void>();
      final stopSignal = Completer<bool>();
      connection.ended = stopSignal;
      stopSignal.future.then((_) {
        if (!ended.isCompleted) ended.complete();
      });
      subscription = stream.listen(
        handle,
        onError: (Object error, StackTrace _) {
          if (!ended.isCompleted) ended.completeError(error);
        },
        onDone: () {
          if (!ended.isCompleted) ended.complete();
        },
        cancelOnError: true,
      );
      connection.subscription = subscription;
      try {
        await ended.future;
      } finally {
        connection.subscription = null;
        connection.ended = null;
      }
    }

    try {
      while (!terminal &&
          !connection.cancelRequested &&
          !connection.detached &&
          retries < _maxResumeConnections) {
        try {
          // A request without a cursor replays all deltas. Keep the
          // persisted text visible until the first replayed delta arrives,
          // then replace it with the replay so it cannot be appended twice.
          replaceOnFirstDelta = lastEventId.isEmpty;
          if (replaceOnFirstDelta) {
            content = '';
            hasReceivedDelta = false;
            update((message) => message['content'] = persistedContent);
          }
          final stream = await api.streamMessageAfter(
            messageId,
            after: lastEventId,
          );
          if (connection.detached || !mounted) {
            await stream.listen((_) {}).cancel();
            break;
          }
          await consume(stream);
          if (!terminal &&
              !connection.cancelRequested &&
              !connection.detached) {
            retries++;
            if (retries >= _maxResumeConnections) break;
            await Future<void>.delayed(widget.resumeBackoff(retries));
          }
        } catch (error) {
          if (connection.cancelRequested || connection.detached) break;
          streamError = error is AssistantApiException
              ? error.message
              : '续读连接异常';
          retries++;
          if (retries >= _maxResumeConnections) break;
          await Future<void>.delayed(widget.resumeBackoff(retries));
        }
      }
    } finally {
      // Start cancelling before updating the UI, but do not await it here:
      // waiting for StreamSubscription.cancel can postpone the terminal state
      // until after Flutter's current frame has settled.
      final cancellation = subscription?.cancel();
      if (identical(_localGeneration, connection)) _localGeneration = null;
      if (mounted && !connection.detached) {
        setState(() {
          final index = messageIndex();
          if (index >= 0 && sessionId == connection.sessionId) {
            messages[index]['streaming'] = false;
            if (connection.cancelRequested) {
              messages[index]['status'] = 'incomplete';
              messages[index]['error'] = '已停止生成';
            } else if (!terminal) {
              messages[index]['status'] = 'incomplete';
              messages[index]['error'] = '生成可能已中断';
            }
          }
        });
        if (terminal || connection.cancelRequested) {
          _finishGeneration(connection.sessionId, messageId, source);
        }
        await _load();
      }
      await cancellation;
    }
  }

  Future<void> _loadOlderMessages() async {
    if (_loadingOlderMessages ||
        !_hasMoreMessages ||
        _oldestMessageId == null) {
      return;
    }
    _loadingOlderMessages = true;
    final requestedSession = sessionId;
    if (mounted) setState(() {});
    try {
      final page = await api.listMessages(
        requestedSession,
        limit: 100,
        before: _oldestMessageId,
      );
      if (!mounted || sessionId != requestedSession) return;
      setState(() {
        messages = [...page.messages, ...messages];
        _hasMoreMessages = page.hasMore;
        _oldestMessageId =
            page.oldestId ??
            (messages.isNotEmpty ? '${messages.first['id'] ?? ''}' : null);
      });
    } on AssistantApiException catch (error) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            content: Text(error.isUnavailable ? '服务暂时不可用' : error.message),
          ),
        );
      }
    } finally {
      _loadingOlderMessages = false;
      if (mounted) setState(() {});
    }
  }

  List<Map<String, dynamic>> _maps(dynamic value) =>
      (value is List ? value : [])
          .map((e) => Map<String, dynamic>.from(e as Map))
          .toList();

  Map<String, dynamic>? _sessionById(String? id) {
    if (id == null || id.isEmpty) return null;
    for (final session in _sessions) {
      if (session['id'] == id) return session;
    }
    return null;
  }

  String? get _mainSessionId {
    for (final session in _sessions) {
      if (session['kind'] == 'main' && session['id'] is String) {
        return session['id'] as String;
      }
    }
    return null;
  }

  String _currentSessionTitle() {
    final session = _sessionById(_selectedSessionId ?? sessionId);
    if (session?['kind'] == 'main') return '主聊天';
    final title = session?['title'];
    return title is String && title.trim().isNotEmpty ? title : '主聊天';
  }

  Future<void> _selectSession(String id) async {
    if (id.isEmpty) return;
    if (mounted) {
      setState(() {
        tab = 0;
        _composerFocusRequest = 0;
      });
    }
    if (_selectedSessionId == id && sessionId == id && !loading) return;
    _detachLocalGeneration();
    if (mounted) {
      setState(() {
        _selectedSessionId = id;
        sessionId = id;
        _completedSessions.remove(id);
        _draftFiles = [];
        messages = [];
        _hasMoreMessages = false;
        _oldestMessageId = null;
        loading = true;
      });
    }
    while (mounted && _selectedSessionId == id && _loadingRemote) {
      await _remoteLoadComplete?.future;
    }
    if (mounted && _selectedSessionId == id && loading) {
      await _load(initial: true, forceResume: true);
    }
  }

  Future<void> _startIdea(String id, String prompt) async {
    await _selectSession(id);
    if (!mounted || sessionId != id || loading) {
      throw const AssistantApiException('会话未能打开，请稍后重试');
    }
    if (sending) throw const AssistantApiException('会话正在回复，请稍后重试');
    await _sendText(prompt);
  }

  Future<void> _discussFeed(String id) async {
    await _selectSession(id);
    if (!mounted || sessionId != id || loading) return;
    setState(() => _composerFocusRequest++);
  }

  Future<void> _selectMainSession() async {
    String? id;
    for (final session in _sessions) {
      if (session['kind'] == 'main' && session['id'] is String) {
        id = session['id'] as String;
        break;
      }
    }
    if (id == null) {
      try {
        final session = await api.mainSession();
        if (session != null && session['id'] is String) {
          id = session['id'] as String;
          if (mounted) {
            setState(() {
              _sessions = [
                ..._sessions.where((item) => item['kind'] != 'main'),
                session,
              ];
            });
          }
        }
      } on AssistantApiException catch (error) {
        if (mounted) {
          ScaffoldMessenger.of(context).showSnackBar(
            SnackBar(
              content: Text(error.isUnavailable ? '服务暂时不可用' : error.message),
            ),
          );
        }
        return;
      }
    }
    // Servers predating `/sessions/main` may still return the main chat as
    // the first regular session, without a `kind` field.
    if (id == null && _sessions.isNotEmpty) {
      final firstId = _sessions.first['id'];
      if (firstId is String && firstId.isNotEmpty) id = firstId;
    }
    if (id != null) await _selectSession(id);
  }

  Future<Map<String, dynamic>?> _createSideSession() async {
    try {
      final session = await api.createSession(kind: 'side');
      final id = session['id'];
      if (id is! String || id.isEmpty) return null;
      if (mounted) {
        setState(() {
          _sessions = [..._sessions.where((item) => item['id'] != id), session];
        });
      }
      return session;
    } on AssistantApiException catch (error) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            content: Text(error.isUnavailable ? '服务暂时不可用' : error.message),
          ),
        );
      }
      return null;
    }
  }

  Future<bool> _deleteSideSession(
    Map<String, dynamic> session, {
    BuildContext? pickerContext,
  }) async {
    final id = session['id'];
    if (id is! String || id.isEmpty || session['kind'] == 'main') return false;
    final title = '${session['title'] ?? '旁聊'}';
    final confirmed = await showDialog<bool>(
      context: context,
      useRootNavigator: true,
      builder: (dialogContext) => AlertDialog(
        title: const Text('删除旁聊？'),
        content: Text('将删除「$title」及其中的消息。'),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialogContext, false),
            child: const Text('取消'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(dialogContext, true),
            style: FilledButton.styleFrom(backgroundColor: context.muse.danger),
            child: const Text('删除'),
          ),
        ],
      ),
    );
    if (confirmed != true || !mounted) return false;
    try {
      await api.deleteSession(id);
      final wasCurrent = _selectedSessionId == id || sessionId == id;
      if (_localGeneration?.sessionId == id) _detachLocalGeneration();
      if (mounted) {
        setState(() {
          _loadGeneration++;
          _sessions = _sessions.where((item) => item['id'] != id).toList();
          final messageId = _generatingMessages.remove(id);
          _generationDeadlines.remove(id);
          _generationCursors.remove(messageId);
          _generationSnapshots.remove(messageId);
          _completedSessions.remove(id);
        });
      }
      if (wasCurrent) {
        if (pickerContext != null && pickerContext.mounted) {
          Navigator.of(pickerContext).pop();
        }
        await _selectMainSession();
      }
      return true;
    } catch (error) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            content: Text(
              error is AssistantApiException ? error.message : '旁聊删除失败，请稍后重试',
            ),
          ),
        );
      }
      return false;
    }
  }

  Future<bool> _renameSideSession(Map<String, dynamic> session) async {
    final id = session['id'];
    if (id is! String || id.isEmpty || session['kind'] == 'main') return false;
    final titleInput = TextEditingController(
      text: '${session['title'] ?? '旁聊'}',
    );
    final route = DialogRoute<String>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: const Text('重命名旁聊'),
        content: TextField(
          key: const ValueKey('rename-session-input'),
          controller: titleInput,
          autofocus: true,
          maxLength: 200,
          decoration: const InputDecoration(labelText: '名称'),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialogContext),
            child: const Text('取消'),
          ),
          FilledButton(
            onPressed: () {
              final value = titleInput.text.trim();
              if (value.isNotEmpty) Navigator.pop(dialogContext, value);
            },
            child: const Text('保存'),
          ),
        ],
      ),
    );
    final title = await Navigator.of(context, rootNavigator: true).push(route);
    await route.completed;
    titleInput.dispose();
    if (title == null || !mounted) return false;
    try {
      final updated = await api.renameSession(id, title);
      if (!mounted) return false;
      setState(() {
        _loadGeneration++;
        session.addAll(updated);
        _sessions = [
          for (final item in _sessions)
            if (item['id'] == id) {...item, ...updated} else item,
        ];
      });
      return true;
    } catch (error) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            content: Text(
              error is AssistantApiException ? error.message : '旁聊重命名失败，请稍后重试',
            ),
          ),
        );
      }
      return false;
    }
  }

  Future<void> _openSessionPicker() async {
    if (tab != 0) return;
    await showModalBottomSheet<void>(
      context: context,
      backgroundColor: context.muse.bg,
      isScrollControlled: true,
      showDragHandle: true,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(18)),
      ),
      builder: (sheetContext) => SessionPicker(
        sessions: _sessions,
        selectedSessionId: _selectedSessionId,
        mainSelected: _sessionById(_selectedSessionId)?['kind'] == 'main',
        mainSessionId: _mainSessionId,
        onMainChat: _selectMainSession,
        onSelectSession: _selectSession,
        onCreateSession: _createSideSession,
        onDeleteSession: (session) =>
            _deleteSideSession(session, pickerContext: sheetContext),
        onRenameSession: _renameSideSession,
        generatingSessionIds: _generatingMessages.keys.toSet(),
        completedSessionIds: _completedSessions,
      ),
    );
  }

  Future<void> _decideApproval(
    String id,
    bool approve, [
    bool remember = false,
  ]) async {
    Map<String, dynamic>? result;
    try {
      result = await api.decideApproval(id, approve, remember: remember);
    } on AssistantApiException catch (error) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            content: Text(error.isUnavailable ? '服务暂时不可用' : error.message),
          ),
        );
      }
      return;
    }
    if (!mounted) return;
    if (result == null) {
      ScaffoldMessenger.of(context)
          .showSnackBar(const SnackBar(content: Text('审批更新失败，请检查服务连接')));
      return;
    }
    await _load();
  }

  Future<void> _send() async {
    var text = input.text.trim();
    if (text.isEmpty || sending || loading) return;
    input.clear();
    final files = List<Map<String, dynamic>>.of(_draftFiles);
    setState(() => _draftFiles = []);
    final request = _sendText(
      text,
      metadata: files.isEmpty ? null : {'files': files},
    );
    text = '';
    await request;
  }

  Future<void> _sendText(String text, {Map<String, dynamic>? metadata}) async {
    if (text.trim().isEmpty || sending || loading) return;
    _loadGeneration++;
    final connection = _LocalGeneration(sessionId);
    _localGeneration = connection;
    _pendingStarts[sessionId] = connection;
    final widgetEvent = metadata?['widget_event'];
    final continuing =
        widgetEvent is Map &&
        (widgetEvent['action'] == 'confirm' ||
            widgetEvent['action'] == 'cancel');
    var requestContent = text;
    final hasSecrets = containsSecretJson(text);
    final preview = optimisticChatContent(text);
    text = '';
    Map<String, dynamic>? optimisticUser;
    late Map<String, dynamic> assistantMessage;
    setState(() {
      if (!continuing) {
        optimisticUser = {
          'role': 'user',
          'content': preview,
          'time': '现在',
          'metadata': ?metadata,
        };
        messages.add(optimisticUser!);
      }
      final existingIndex = continuing
          ? messages.indexWhere((message) {
              final messageMetadata = message['metadata'];
              final widgets = messageMetadata is Map
                  ? messageMetadata['widgets']
                  : null;
              return message['role'] == 'assistant' &&
                  widgets is List &&
                  widgets.any(
                    (widget) =>
                        widget is Map &&
                        widget['type'] == 'confirm' &&
                        widget['id'] == widgetEvent['widget_id'],
                  );
            })
          : -1;
      assistantMessage = existingIndex >= 0
          ? messages[existingIndex]
          : {'role': 'assistant', 'content': ''};
      if (existingIndex < 0) messages.add(assistantMessage);
      assistantMessage.addAll({
        'time': '正在回复',
        'status': 'streaming',
        'streaming': true,
      });
      assistantMessage.remove('error');
      assistantMessage.remove('tool_states');
      _trackGeneration(connection.sessionId, '');
    });
    _scrollToBottom(force: true);

    var received = '';
    var receivedDone = false;
    var streamError = '';
    var lastEventId = '';
    var reconnects = 0;
    var resumedWithoutCursor = false;
    final toolStates = <String, Map<String, dynamic>>{};
    StreamSubscription<AssistantSseEvent>? subscription;

    void bindAssistant(String id) {
      connection.messageId = id;
      if (identical(_pendingStarts[connection.sessionId], connection)) {
        _pendingStarts.remove(connection.sessionId);
      }
      if (!mounted || !_generatingMessages.containsKey(connection.sessionId)) {
        return;
      }
      setState(() => _trackGeneration(connection.sessionId, id));
      if (connection.stopRequested) unawaited(_cancelAfterStart(connection));
      if (connection.detached) {
        connection.subscription?.cancel();
        final ended = connection.ended;
        if (ended != null && !ended.isCompleted) ended.complete(false);
        return;
      }
      setState(() {
        final existingIndex = messages.indexWhere(
          (message) => message['role'] == 'assistant' && message['id'] == id,
        );
        if (existingIndex >= 0 &&
            !identical(messages[existingIndex], assistantMessage)) {
          final existing = messages[existingIndex];
          messages.remove(assistantMessage);
          assistantMessage = existing;
        }
        assistantMessage['id'] = id;
      });
    }

    void updateAssistant(void Function(Map<String, dynamic>) update) {
      if (!mounted ||
          connection.detached ||
          sessionId != connection.sessionId) {
        return;
      }
      final id = assistantMessage['id'];
      final index = id is String
          ? messages.indexWhere(
              (message) =>
                  message['role'] == 'assistant' && message['id'] == id,
            )
          : messages.indexOf(assistantMessage);
      if (index < 0) return;
      assistantMessage = messages[index];
      setState(() => update(assistantMessage));
      if (id is String) {
        _generationSnapshots[id] = Map<String, dynamic>.from(assistantMessage);
      }
      _scrollToBottom();
    }

    void handleEvent(AssistantSseEvent event) {
      if (!mounted) return;
      if (connection.detached &&
          event.name != 'start' &&
          event.name != 'status') {
        return;
      }
      if (event.id != null && event.id!.isNotEmpty) {
        lastEventId = event.id!;
        final id = connection.messageId;
        if (id != null) _generationCursors[id] = lastEventId;
      }
      if (event.name == 'start' || event.name == 'status') {
        final userMessage = event.data['user_message'];
        if (userMessage is Map &&
            mounted &&
            !connection.detached &&
            sessionId == connection.sessionId) {
          setState(() {
            final id = userMessage['id'];
            final existingIndex = id is String
                ? messages.indexWhere(
                    (message) =>
                        message['role'] == 'user' && message['id'] == id,
                  )
                : -1;
            final target = existingIndex >= 0
                ? messages[existingIndex]
                : optimisticUser;
            target?.addAll(Map<String, dynamic>.from(userMessage));
          });
        }
        final id = event.data['message_id'] ?? event.data['id'];
        if (id is String && id.isNotEmpty) {
          bindAssistant(id);
        }
        final nextStatus = '${event.data['status'] ?? 'streaming'}';
        updateAssistant((message) {
          message['status'] = nextStatus;
          message['streaming'] =
              nextStatus == 'streaming' || nextStatus == 'pending';
        });
        return;
      }
      if (event.name == 'delta') {
        final delta = event.data['content'];
        if (!resumedWithoutCursor && delta is String && delta.isNotEmpty) {
          received += delta;
          updateAssistant((message) => message['content'] = received);
        }
        return;
      }
      if (event.name == 'tool') {
        final callId = '${event.data['call_id'] ?? event.data['id'] ?? ''}';
        if (callId.isNotEmpty) {
          toolStates[callId] = Map<String, dynamic>.from(event.data);
          updateAssistant((assistant) {
            assistant['tool_states'] = toolStates.values
                .map((item) => Map<String, dynamic>.from(item))
                .toList();
          });
        }
        return;
      }
      if (event.name == 'error') {
        final nested = event.data['error'];
        final message =
            event.data['message'] ??
            event.data['detail'] ??
            (nested is Map ? nested['message'] ?? nested['detail'] : null);
        streamError = message is String && message.isNotEmpty
            ? message
            : '流式服务返回了错误';
        updateAssistant((assistant) {
          assistant['status'] = 'error';
          assistant['streaming'] = false;
          assistant['error'] = streamError;
        });
        receivedDone = true;
        return;
      }
      if (event.name == 'done') {
        receivedDone = true;
        final id = event.data['id'];
        if (id is String && id.isNotEmpty) bindAssistant(id);
        final doneContent = event.data['content'];
        final eventMetadata = event.data['metadata'];
        final finalStatus = '${event.data['status'] ?? 'complete'}';
        if (doneContent is String) received = doneContent;
        updateAssistant((assistant) {
          assistant['content'] = received;
          assistant['status'] = finalStatus == 'streaming'
              ? 'complete'
              : finalStatus;
          assistant['streaming'] = false;
          assistant['time'] = '刚刚';
          if (eventMetadata is Map) {
            final parsed = _mergeMessageMetadata(
              assistant['metadata'],
              eventMetadata,
            );
            assistant['metadata'] = parsed;
            if (parsed['tool_calls'] is List) {
              final liveStates = liveBrowserStates(assistant['tool_states']);
              if (liveStates.isEmpty) {
                assistant.remove('tool_states');
              } else {
                assistant['tool_states'] = liveStates;
              }
            }
            if (parsed['provider'] is String) {
              assistant['provider'] = parsed['provider'];
            }
            if (parsed['incomplete'] == true) {
              assistant['status'] = 'incomplete';
            }
          }
          if (assistant['status'] == 'incomplete' && streamError.isEmpty) {
            assistant['error'] = '流式回复未完整结束，已保留收到的内容';
          }
        });
      }
    }

    Future<bool> consume(Stream<AssistantSseEvent> stream) async {
      final ended = Completer<bool>();
      connection.ended = ended;
      subscription = stream.listen(
        handleEvent,
        onError: (Object error, StackTrace _) {
          if (!ended.isCompleted) ended.completeError(error);
        },
        onDone: () {
          if (!ended.isCompleted) ended.complete(receivedDone);
        },
        cancelOnError: true,
      );
      connection.subscription = subscription;
      try {
        return await ended.future;
      } finally {
        connection.ended = null;
        connection.subscription = null;
      }
    }

    try {
      while (!receivedDone &&
          !connection.cancelRequested &&
          (!connection.detached || connection.messageId == null)) {
        try {
          final stream = reconnects == 0
              ? await api.streamMessage(
                  connection.sessionId,
                  requestContent,
                  metadata: metadata,
                )
              : await api.streamMessageAfter(
                  connection.messageId!,
                  after: lastEventId,
                );
          requestContent = '';
          if (!mounted ||
              (connection.detached && connection.messageId != null)) {
            await stream.listen((_) {}).cancel();
            break;
          }
          resumedWithoutCursor = reconnects > 0 && lastEventId.isEmpty;
          final complete = await consume(stream);
          if (complete ||
              receivedDone ||
              connection.cancelRequested ||
              connection.detached) {
            break;
          }
          if (connection.messageId == null || reconnects >= 3) break;
          reconnects++;
          await Future<void>.delayed(Duration(milliseconds: 250 * reconnects));
        } catch (error) {
          if (connection.cancelRequested) break;
          if (!connection.detached &&
              connection.messageId != null &&
              reconnects < 3) {
            reconnects++;
            await Future<void>.delayed(
              Duration(milliseconds: 250 * reconnects),
            );
            continue;
          }
          streamError = hasSecrets && connection.messageId == null
              ? '敏感信息未确认保存，请重新粘贴后重试'
              : error is AssistantApiException
              ? error.message
              : '流式连接异常';
          updateAssistant((assistant) {
            assistant['status'] = 'error';
            assistant['streaming'] = false;
            assistant['error'] = streamError;
          });
          receivedDone = true;
        }
      }
    } finally {
      requestContent = '';
      final cancellation = subscription?.cancel();
      if (identical(_pendingStarts[connection.sessionId], connection)) {
        _pendingStarts.remove(connection.sessionId);
      }
      if (identical(_localGeneration, connection)) _localGeneration = null;
      if (mounted) {
        if (!connection.detached) {
          setState(() {
            final assistant = assistantMessage;
            assistant['streaming'] = false;
            assistant['time'] = '刚刚';
            if (connection.cancelRequested) {
              assistant['status'] = 'incomplete';
              assistant['error'] = '已停止生成';
            } else if (!receivedDone && streamError.isEmpty) {
              assistant['status'] = 'incomplete';
              assistant['error'] = '流式连接提前结束，已保留收到的内容';
            }
          });
        }
        if (receivedDone || connection.cancelRequested) {
          _finishGeneration(
            connection.sessionId,
            connection.messageId ?? '',
            assistantMessage,
          );
        }
        if (streamError.isNotEmpty &&
            connection.messageId == null &&
            (connection.detached || !hasSecrets)) {
          ScaffoldMessenger.of(context)
              .showSnackBar(SnackBar(content: Text(streamError)));
        }
        if (!connection.detached) {
          _scrollToBottom();
          await _load();
        }
      }
      await cancellation;
    }
  }

  Future<void> _stopGeneration() async {
    if (!sending) return;
    final currentSession = sessionId;
    final connection = _localGeneration?.sessionId == currentSession
        ? _localGeneration
        : _pendingStarts[currentSession];
    final messageId = _generatingMessages[currentSession];
    if ((messageId == null || messageId.isEmpty) && connection != null) {
      connection.stopRequested = true;
      return;
    }
    try {
      if (messageId != null && messageId.isNotEmpty) {
        await api.cancelMessage(messageId);
      }
    } on AssistantApiException catch (error) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            content: Text(error.isUnavailable ? '服务暂时不可用' : error.message),
          ),
        );
      }
      return;
    }
    if (connection != null && connection.sessionId == currentSession) {
      connection.cancelRequested = true;
      final ended = connection.ended;
      if (ended != null && !ended.isCompleted) ended.complete(false);
    } else if (mounted) {
      _finishGeneration(currentSession, messageId ?? '', {
        'status': 'incomplete',
      });
      if (sessionId == currentSession) await _load();
    }
  }

  Future<void> _cancelAfterStart(_LocalGeneration connection) async {
    connection.stopRequested = false;
    try {
      await api.cancelMessage(connection.messageId!);
      connection.cancelRequested = true;
      final ended = connection.ended;
      if (ended != null && !ended.isCompleted) ended.complete(false);
      if (mounted && connection.detached) {
        _finishGeneration(connection.sessionId, connection.messageId!, {
          'status': 'incomplete',
        });
      }
    } catch (error) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            content: Text(
              error is AssistantApiException ? error.message : '停止生成失败，请稍后重试',
            ),
          ),
        );
      }
    }
  }

  Future<void> _retryMessage(Map<String, dynamic> message) async {
    if (sending) return;
    final index = messages.indexOf(message);
    if (index <= 0) return;
    for (var i = index - 1; i >= 0; i--) {
      if (messages[i]['role'] == 'user') {
        final content = '${messages[i]['content'] ?? ''}'.trim();
        if (content.isNotEmpty) await _sendText(content);
        return;
      }
    }
  }

  Future<void> _onWidgetEvent(
    String widgetId,
    String action,
    Object? value,
  ) async {
    if (sending) return;
    final requestedSession = sessionId;
    try {
      final result = await api.widgetEvent(widgetId, action, value);
      if (!mounted || sessionId != requestedSession) return;
      final updated = result['widget'];
      if (updated is Map && mounted) {
        setState(() {
          for (final message in messages) {
            final metadata = message['metadata'];
            final list = metadata is Map ? metadata['widgets'] : null;
            if (list is List) {
              final index = list.indexWhere(
                (item) => item is Map && item['id'] == widgetId,
              );
              if (index >= 0) list[index] = Map<String, dynamic>.from(updated);
            }
          }
        });
      }
      final response = result['message'];
      if (response is String && response.isNotEmpty) {
        await _sendText(
          response,
          metadata: {
            'widget_event': {'widget_id': widgetId, 'action': action},
          },
        );
      }
    } catch (error) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(
            error is AssistantApiException ? error.message : '组件操作失败',
          ),
        ),
      );
    }
  }

  Future<void> _openFileResult(Map<String, dynamic> file) async {
    final fileId = '${file['file_id'] ?? file['fileId'] ?? file['id'] ?? ''}'
        .trim();
    if (fileId.isEmpty || !mounted) return;
    final title = '${file['filename'] ?? file['name'] ?? '文件'}';
    await Navigator.of(context).push(
      MaterialPageRoute(
        builder: (_) => FileResultView(
          title: title,
          uri: api.fileContentUri(fileId),
          token: api.token,
        ),
      ),
    );
  }

  Future<void> _exportLibraryFile(Map<String, dynamic> file) async {
    final id = '${file['id'] ?? file['file_id'] ?? ''}';
    if (id.isEmpty) return;
    try {
      final bytes = await api.downloadFileBytes(id);
      if (!mounted) return;
      await exportFile(bytes, '${file['filename'] ?? file['title'] ?? '文件'}');
    } catch (_) {
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(const SnackBar(content: Text('文件保存失败，请稍后重试')));
    }
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _refreshTimer?.cancel();
    _localGeneration?.subscription?.cancel();
    final ended = _localGeneration?.ended;
    if (ended != null && !ended.isCompleted) ended.complete(false);
    _localGeneration?.detached = true;
    for (final connection in _pendingStarts.values) {
      connection.detached = true;
      connection.subscription?.cancel();
      final ended = connection.ended;
      if (ended != null && !ended.isCompleted) ended.complete(false);
    }
    _disconnectNotifications();
    api.close();
    _notificationItems.dispose();
    input.dispose();
    _messagesScrollController.dispose();
    super.dispose();
  }

  void _scrollToBottom({bool force = false}) {
    if (!force &&
        _messagesScrollController.hasClients &&
        _messagesScrollController.position.extentAfter > 120) {
      return;
    }
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted || !_messagesScrollController.hasClients) return;
      final target = _messagesScrollController.position.maxScrollExtent;
      _messagesScrollController.animateTo(
        target,
        duration: const Duration(milliseconds: 180),
        curve: Curves.easeOut,
      );
    });
  }

  Future<void> _openSettings() async {
    FocusManager.instance.primaryFocus?.unfocus();
    await Navigator.of(context).push(
      MaterialPageRoute(
        builder: (_) => SettingsView(
          api: api,
          memories: memories,
          onRefresh: () => _load(),
          onLogout: widget.onLogout,
          onNotifications: _openNotifications,
          onConnectors: _openConnectors,
          onSelectSession: (id) => unawaited(_selectSession(id)),
        ),
      ),
    );
  }

  Future<void> _openConnectors() async {
    await Navigator.of(context)
        .push(MaterialPageRoute(builder: (_) => ConnectorsView(api: api)));
  }

  @override
  Widget build(BuildContext context) {
    return LayoutBuilder(
      builder: (context, constraints) {
        final wide = constraints.maxWidth >= MuseMetrics.mobileBreakpoint;
        if (!wide) {
          return MobileShell(
            tab: tab,
            title: tab == 0 ? _mobileSessionTitle() : mobileTabLabels[tab],
            sessions: _sessions,
            sessionId: sessionId,
            onTabChanged: (index) => setState(() {
              tab = index;
              _composerFocusRequest = 0;
            }),
            onMainChat: _selectMainSession,
            onNewSession: _newSideChat,
            onSettings: _openSettings,
            onSearch: _openSearch,
            onSelectSession: (id) => unawaited(_selectSession(id)),
            onDeleteSession: (session) => _deleteSideSession(session),
            onRenameSession: _renameSideSession,
            mainSessionId: _mainSessionId,
            generatingSessionIds: _generatingMessages.keys.toSet(),
            completedSessionIds: _completedSessions,
            onTabBarHeightChanged: (height) {
              if (mounted && _tabBarHeight != height) {
                setState(() => _tabBarHeight = height);
              }
            },
            unreadCount: _notifications
                .where((item) => item['read_at'] == null)
                .length,
            onNotifications: _openNotifications,
            child: _tabContent(mobile: true),
          );
        }
        return Scaffold(
          backgroundColor: context.muse.bg,
          body: SafeArea(
            child: Row(
              children: [
                _sidebar(),
                Expanded(
                  child: Stack(
                    children: [
                      Padding(
                        padding: const EdgeInsets.only(top: 58),
                        child: AnimatedSwitcher(
                          duration: const Duration(milliseconds: 180),
                          layoutBuilder: (currentChild, _) =>
                              currentChild ?? const SizedBox.shrink(),
                          child: KeyedSubtree(
                            key: ValueKey(tab),
                            child: _tabContent(),
                          ),
                        ),
                      ),
                      Positioned(
                        top: 8,
                        left: 24,
                        right: 24,
                        child: _floatingHeader(),
                      ),
                    ],
                  ),
                ),
              ],
            ),
          ),
        );
      },
    );
  }

  String _mobileSessionTitle() {
    final current = _sessionById(sessionId);
    if (current == null || current['kind'] == 'main') return 'Luma';
    return '${current['title'] ?? '旁聊'}';
  }

  Widget _tabContent({bool mobile = false}) {
    final padding = mobile
        ? EdgeInsets.fromLTRB(
            16,
            MediaQuery.paddingOf(context).top + MobileShell.headerHeight,
            16,
            MediaQuery.paddingOf(context).bottom +
                MobileShell.tabBarHeight +
                24,
          )
        : null;
    switch (tab) {
      case 0:
        final chat = ChatView(
          key: ValueKey('chat-$sessionId'),
          api: api,
          sessionId: sessionId,
          messages: messages,
          tasks: tasks,
          memories: memories,
          sending: sending,
          bottomOverlayHeight: mobile ? _tabBarHeight : 0,
          focusRequest: _composerFocusRequest,
          input: input,
          scrollController: _messagesScrollController,
          onRefresh: () => _load(forceResume: true),
          onSend: _send,
          onWidgetEvent: _onWidgetEvent,
          onFile: _openFileResult,
          onAttach: _attachFile,
          attachments: _draftFiles,
          onRemoveAttachment: (id) => setState(
            () => _draftFiles = _draftFiles
                .where((item) => item['id'] != id)
                .toList(),
          ),
          onLoadOlder: _loadOlderMessages,
          onStop: _stopGeneration,
          onRetry: _retryMessage,
          loadingOlder: _loadingOlderMessages,
          hasMoreMessages: _hasMoreMessages,
        );
        if (!mobile) return chat;
        final isSideChat = MobileShell.isSideSession(
          sessionId: sessionId,
          sessions: _sessions,
          mainSessionId: _mainSessionId,
        );
        return ScrollConfiguration(
          behavior: _MobileChatScrollBehavior(
            controller: _messagesScrollController,
            headerReduction: isSideChat
                ? 0
                : MobileShell.headerHeight - MobileShell.mainChatHeaderHeight,
          ),
          child: chat,
        );
      case 1:
        return FeedView(
          api: api,
          onDiscuss: _discussFeed,
          contentPadding: padding,
          showHeader: !mobile,
        );
      case 2:
        return IdeasView(
          api: api,
          onStart: _startIdea,
          onSelectSession: (id) => unawaited(_selectSession(id)),
          contentPadding: padding,
          showHeader: !mobile,
        );
      case 3:
        return MissionsView(
          tasks: tasks,
          approvals: approvals,
          runtimeJobs: runtimeJobs,
          onDecideApproval: _decideApproval,
          onDecideApprovalWithRemember: (id, approve, remember) =>
              _decideApproval(id, approve, remember),
          api: api,
          contentPadding: padding,
          showHeader: !mobile,
          onRefresh: () => _load(),
        );
      default:
        return LibraryView(
          api: api,
          contentPadding: padding,
          showHeader: !mobile,
          onFile: _exportLibraryFile,
          onSelectSession: (id) => unawaited(_selectSession(id)),
        );
    }
  }

  Widget _sidebar() {
    return Container(
      width: MuseMetrics.railWidth,
      decoration: BoxDecoration(
        color: context.muse.rail,
        border: Border(right: BorderSide(color: context.muse.line)),
      ),
      child: Column(
        children: [
          const SizedBox(height: 12),
          Expanded(
            child: Center(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  for (var index = 0; index < mobileTabLabels.length; index++)
                    _nav(mobileTabLabels[index], mobileTabIcons[index], index),
                  const SizedBox(height: 14),
                  Container(height: 1, width: 28, color: context.muse.line),
                  const SizedBox(height: 14),
                  _avatar(),
                ],
              ),
            ),
          ),
          _railIconButton(
            tooltip: '设置',
            icon: Icons.settings_outlined,
            onPressed: _openSettings,
          ),
          const SizedBox(height: 12),
        ],
      ),
    );
  }

  Widget _nav(String label, IconData icon, int index) {
    final active = tab == index;
    return Padding(
      padding: const EdgeInsets.only(bottom: 6),
      child: Tooltip(
        message: label,
        child: IconButton(
          onPressed: () {
            HapticFeedback.selectionClick();
            FocusScope.of(context).unfocus();
            setState(() => tab = index);
          },
          icon: Icon(icon),
          iconSize: 20,
          color: active ? context.muse.text : context.muse.muted,
          style: IconButton.styleFrom(
            fixedSize: const Size(
              MuseMetrics.railButton,
              MuseMetrics.railButton,
            ),
            backgroundColor: active
                ? context.muse.chip
                : context.muse.rail.withValues(alpha: 0),
            shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(12),
            ),
          ),
        ),
      ),
    );
  }

  Widget _avatar() {
    return const GlassSurface(
      shape: GlassShape.circle,
      child: SizedBox(
        width: 36,
        height: 36,
        child: Center(child: LumaLogo(key: ValueKey('rail-logo'), width: 29)),
      ),
    );
  }

  Widget _railIconButton({
    required String tooltip,
    required IconData icon,
    required VoidCallback onPressed,
  }) {
    return Tooltip(
      message: tooltip,
      child: IconButton(
        onPressed: onPressed,
        icon: Icon(icon),
        iconSize: 19,
        color: context.muse.muted,
        style: IconButton.styleFrom(
          fixedSize: const Size(MuseMetrics.railButton, MuseMetrics.railButton),
          backgroundColor: context.muse.chip,
          shape: const CircleBorder(),
        ),
      ),
    );
  }

  Widget _floatingHeader() {
    final title = tab == 0 ? _currentSessionTitle() : mobileTabLabels[tab];
    return Row(
      children: [
        Flexible(
          child: InkWell(
            onTap: tab == 0 ? _openSessionPicker : null,
            borderRadius: BorderRadius.circular(17),
            child: Container(
              height: MuseMetrics.pillHeight,
              padding: const EdgeInsets.symmetric(horizontal: 14),
              decoration: BoxDecoration(
                color: context.muse.chip,
                borderRadius: BorderRadius.circular(17),
              ),
              alignment: Alignment.centerLeft,
              child: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Flexible(
                    child: Text(
                      title,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(
                        color: context.muse.text,
                        fontSize: 14,
                        fontWeight: FontWeight.w500,
                      ),
                    ),
                  ),
                  if (tab == 0) ...[
                    const SizedBox(width: 4),
                    Icon(
                      Icons.expand_more,
                      size: 17,
                      color: context.muse.muted,
                    ),
                  ],
                ],
              ),
            ),
          ),
        ),
        if (tab == 0 && _completedSessions.contains(_mainSessionId)) ...[
          const SizedBox(width: 7),
          Semantics(
            label: '主聊天回复已完成',
            child: Container(
              key: const ValueKey('desktop-main-completed'),
              width: 8,
              height: 8,
              decoration: BoxDecoration(
                color: context.muse.accent,
                shape: BoxShape.circle,
              ),
            ),
          ),
        ],
        const Spacer(),
        _circleAction('刷新', Icons.refresh, () => _load(forceResume: true)),
        const SizedBox(width: 6),
        _circleAction('设置', Icons.settings_outlined, _openSettings),
      ],
    );
  }

  Widget _circleAction(String tooltip, IconData icon, VoidCallback onPressed) {
    return Tooltip(
      message: tooltip,
      child: IconButton(
        onPressed: onPressed,
        icon: Icon(icon, size: 18),
        color: context.muse.muted,
        style: IconButton.styleFrom(
          fixedSize: const Size(34, 34),
          backgroundColor: context.muse.chip,
          shape: const CircleBorder(),
        ),
      ),
    );
  }
}
