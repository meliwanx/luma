import 'dart:async';

import 'package:flutter/material.dart';

import '../session_utils.dart';
import '../theme.dart';
import 'side_menu.dart';

class SessionPicker extends StatefulWidget {
  const SessionPicker({
    super.key,
    required this.sessions,
    required this.selectedSessionId,
    required this.mainSelected,
    required this.onMainChat,
    required this.onSelectSession,
    required this.onCreateSession,
    required this.onDeleteSession,
    this.onRenameSession,
    this.mainSessionId,
    this.generatingSessionIds = const <String>{},
    this.completedSessionIds = const <String>{},
  });

  final List<Map<String, dynamic>> sessions;
  final String? selectedSessionId;
  final bool mainSelected;
  final Future<void> Function() onMainChat;
  final Future<void> Function(String) onSelectSession;
  final Future<Map<String, dynamic>?> Function() onCreateSession;
  final Future<bool> Function(Map<String, dynamic>) onDeleteSession;
  final Future<bool> Function(Map<String, dynamic>)? onRenameSession;
  final String? mainSessionId;
  final Set<String> generatingSessionIds;
  final Set<String> completedSessionIds;

  @override
  State<SessionPicker> createState() => _SessionPickerState();
}

class _SessionPickerState extends State<SessionPicker> {
  late List<Map<String, dynamic>> _sessions;

  @override
  void initState() {
    super.initState();
    _sessions = widget.sessions
        .where((session) => session['kind'] == 'side')
        .map((session) => Map<String, dynamic>.from(session))
        .toList();
  }

  Future<bool> _deleteSession(Map<String, dynamic> session) async {
    final deleted = await widget.onDeleteSession(session);
    if (deleted && mounted) {
      setState(() {
        _sessions = _sessions
            .where((item) => item['id'] != session['id'])
            .toList();
      });
    }
    return deleted;
  }

  Future<bool> _renameSession(Map<String, dynamic> session) async {
    final renamed = await widget.onRenameSession?.call(session) ?? false;
    if (renamed && mounted) setState(() {});
    return renamed;
  }

  Future<void> _sessionActions(Map<String, dynamic> session) =>
      showSessionActions(
        context,
        session,
        onRenameSession: widget.onRenameSession == null ? null : _renameSession,
        onDeleteSession: _deleteSession,
      );

  Future<void> _createSession() async {
    final created = await widget.onCreateSession();
    if (created == null || !mounted) return;
    final id = created['id'];
    if (id is! String) return;
    Navigator.of(context).pop();
    await widget.onSelectSession(id);
  }

  String _preview(Map<String, dynamic> session) {
    final value = session['last_message_preview'];
    return value is String && value.trim().isNotEmpty ? value.trim() : '暂无消息';
  }

  String _relativeTime(Map<String, dynamic> session) {
    final raw = session['last_message_at'] ?? session['updated_at'];
    final time = raw is String ? DateTime.tryParse(raw) : null;
    return time == null ? '' : formatRelativeTime(time);
  }

  @override
  Widget build(BuildContext context) {
    final colors = context.muse;
    return SafeArea(
      child: ConstrainedBox(
        constraints: BoxConstraints(
          maxHeight: MediaQuery.sizeOf(context).height * .78,
        ),
        child: ListView(
          padding: const EdgeInsets.fromLTRB(20, 2, 20, 24),
          shrinkWrap: true,
          children: [
            const Text(
              '切换会话',
              style: TextStyle(fontSize: 20, fontWeight: FontWeight.w600),
            ),
            const SizedBox(height: 14),
            ListTile(
              contentPadding: EdgeInsets.zero,
              leading: Icon(Icons.chat_bubble_outline, color: colors.accent),
              title: const Text('主聊天'),
              trailing: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  if (widget.mainSessionId != null)
                    SessionGenerationIndicator(
                      sessionId: widget.mainSessionId!,
                      generating: widget.generatingSessionIds.contains(
                        widget.mainSessionId,
                      ),
                      completed: widget.completedSessionIds.contains(
                        widget.mainSessionId,
                      ),
                    ),
                  if (widget.mainSelected) ...[
                    const SizedBox(width: 8),
                    Icon(Icons.check, color: colors.accent),
                  ],
                ],
              ),
              onTap: () {
                Navigator.of(context).pop();
                unawaited(widget.onMainChat());
              },
            ),
            const SizedBox(height: 8),
            Row(
              children: [
                Text(
                  '旁聊',
                  style: TextStyle(
                    color: colors.muted,
                    fontSize: 13,
                    fontWeight: FontWeight.w600,
                  ),
                ),
                const Spacer(),
                IconButton(
                  tooltip: '新建旁聊',
                  icon: const Icon(Icons.add),
                  color: colors.accent,
                  onPressed: _createSession,
                ),
              ],
            ),
            if (_sessions.isEmpty)
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 22),
                child: Column(
                  children: [
                    Icon(Icons.forum_outlined, size: 30, color: colors.faint),
                    const SizedBox(height: 8),
                    Text(
                      '发起旁聊',
                      style: TextStyle(
                        color: colors.text,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                    const SizedBox(height: 4),
                    Text(
                      '旁聊是按主题整理对话的可选方式。',
                      textAlign: TextAlign.center,
                      style: TextStyle(color: colors.muted, fontSize: 12),
                    ),
                  ],
                ),
              )
            else
              ..._sessions.map(
                (session) => ListTile(
                  key: ValueKey('side-session-${session['id']}'),
                  contentPadding: EdgeInsets.zero,
                  title: Text(
                    '${session['title'] ?? '新旁聊'}',
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                  ),
                  subtitle: Text(
                    [
                      _preview(session),
                      _relativeTime(session),
                    ].where((value) => value.isNotEmpty).join(' · '),
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(color: colors.muted, fontSize: 12),
                  ),
                  trailing: Row(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      SessionGenerationIndicator(
                        sessionId: '${session['id']}',
                        generating: widget.generatingSessionIds.contains(
                          session['id'],
                        ),
                        completed: widget.completedSessionIds.contains(
                          session['id'],
                        ),
                      ),
                      if (widget.selectedSessionId == session['id']) ...[
                        const SizedBox(width: 8),
                        Icon(Icons.check, color: colors.accent),
                      ],
                      IconButton(
                        key: ValueKey(
                          'picker-session-actions-${session['id']}',
                        ),
                        tooltip: '旁聊操作',
                        constraints: const BoxConstraints.tightFor(
                          width: 48,
                          height: 48,
                        ),
                        icon: const Icon(Icons.more_horiz),
                        onPressed: () => _sessionActions(session),
                      ),
                    ],
                  ),
                  onLongPress: () => _sessionActions(session),
                  onTap: () {
                    final id = session['id'];
                    if (id is! String) return;
                    Navigator.of(context).pop();
                    unawaited(widget.onSelectSession(id));
                  },
                ),
              ),
          ],
        ),
      ),
    );
  }
}
