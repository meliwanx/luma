import 'package:flutter/material.dart';

import '../brand.dart';
import '../glass.dart';
import '../session_utils.dart';
import '../theme.dart';

const mobileTabLabels = ['聊天', '动态', '点子', '目标', '资源库'];
const mobileTabIcons = [
  Icons.chat_bubble_outline_rounded,
  Icons.bolt_outlined,
  Icons.lightbulb_outline_rounded,
  Icons.track_changes_rounded,
  Icons.folder_outlined,
];

enum _SessionAction { rename, delete }

Future<void> showSessionActions(
  BuildContext context,
  Map<String, dynamic> session, {
  Future<bool> Function(Map<String, dynamic>)? onRenameSession,
  required Future<bool> Function(Map<String, dynamic>) onDeleteSession,
}) async {
  final action = await showModalBottomSheet<_SessionAction>(
    context: context,
    useRootNavigator: true,
    showDragHandle: true,
    builder: (sheetContext) => SafeArea(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          if (onRenameSession != null)
            ListTile(
              key: const ValueKey('session-action-rename'),
              leading: const Icon(Icons.edit_outlined),
              title: const Text('重命名'),
              onTap: () =>
                  Navigator.of(sheetContext).pop(_SessionAction.rename),
            ),
          ListTile(
            key: const ValueKey('session-action-delete'),
            leading: Icon(
              Icons.delete_outline,
              color: sheetContext.muse.danger,
            ),
            title: Text(
              '删除',
              style: TextStyle(color: sheetContext.muse.danger),
            ),
            onTap: () => Navigator.of(sheetContext).pop(_SessionAction.delete),
          ),
          const SizedBox(height: 8),
        ],
      ),
    ),
  );
  if (action == _SessionAction.rename) {
    await onRenameSession?.call(session);
  } else if (action == _SessionAction.delete) {
    await onDeleteSession(session);
  }
}

class SessionGenerationIndicator extends StatelessWidget {
  const SessionGenerationIndicator({
    super.key,
    required this.sessionId,
    required this.generating,
    required this.completed,
  });

  final String sessionId;
  final bool generating;
  final bool completed;

  @override
  Widget build(BuildContext context) {
    if (generating) {
      return Semantics(
        label: '回复生成中',
        child: SizedBox(
          key: ValueKey('session-generating-$sessionId'),
          width: 14,
          height: 14,
          child: CircularProgressIndicator(
            strokeWidth: 2,
            color: context.muse.accent,
          ),
        ),
      );
    }
    if (completed) {
      return Semantics(
        label: '回复已完成',
        child: Container(
          key: ValueKey('session-completed-$sessionId'),
          width: 8,
          height: 8,
          decoration: BoxDecoration(
            shape: BoxShape.circle,
            color: context.muse.accent,
          ),
        ),
      );
    }
    return const SizedBox.shrink();
  }
}

class SideMenu extends StatelessWidget {
  const SideMenu({
    super.key,
    required this.tab,
    required this.sessions,
    required this.sessionId,
    required this.onTabChanged,
    required this.onMainChat,
    required this.onSelectSession,
    required this.onDeleteSession,
    required this.onRenameSession,
    required this.onSettings,
    required this.onSearch,
    required this.onNewSession,
    this.mainSessionId,
    this.generatingSessionIds = const <String>{},
    this.completedSessionIds = const <String>{},
  });

  final int tab;
  final List<Map<String, dynamic>> sessions;
  final String sessionId;
  final ValueChanged<int> onTabChanged;
  final VoidCallback onMainChat;
  final ValueChanged<String> onSelectSession;
  final Future<bool> Function(Map<String, dynamic>) onDeleteSession;
  final Future<bool> Function(Map<String, dynamic>) onRenameSession;
  final VoidCallback onSettings;
  final VoidCallback onSearch;
  final VoidCallback onNewSession;
  final String? mainSessionId;
  final Set<String> generatingSessionIds;
  final Set<String> completedSessionIds;

  String _relativeTime(Map<String, dynamic> session) {
    final raw = session['last_message_at'] ?? session['updated_at'];
    final time = raw is String ? DateTime.tryParse(raw) : null;
    return time == null ? '' : formatRelativeTime(time);
  }

  @override
  Widget build(BuildContext context) {
    final colors = context.muse;
    final sideSessions = sessions
        .where((session) => session['kind'] == 'side')
        .toList();
    return SafeArea(
      child: Padding(
        padding: const EdgeInsets.fromLTRB(16, 12, 16, 8),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Expanded(
              child: ListView(
                padding: EdgeInsets.zero,
                children: [
                  Row(
                    children: [
                      Expanded(
                        child: const Align(
                          alignment: Alignment.centerLeft,
                          child: LumaLogo(
                            key: ValueKey('drawer-logo'),
                            width: 108,
                          ),
                        ),
                      ),
                      GlassSurface(
                        shape: GlassShape.circle,
                        child: IconButton(
                          tooltip: '设置',
                          onPressed: onSettings,
                          constraints: const BoxConstraints.tightFor(
                            width: 48,
                            height: 48,
                          ),
                          icon: const Icon(Icons.settings_outlined, size: 22),
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 28),
                  _groupLabel(context, '选项卡'),
                  const SizedBox(height: 8),
                  for (var index = 0; index < mobileTabLabels.length; index++)
                    Padding(
                      padding: const EdgeInsets.only(bottom: 4),
                      child: Material(
                        color: tab == index ? colors.chip : Colors.transparent,
                        borderRadius: BorderRadius.circular(16),
                        child: InkWell(
                          key: ValueKey('menu-tab-$index'),
                          borderRadius: BorderRadius.circular(16),
                          onTap: index == 0
                              ? onMainChat
                              : () => onTabChanged(index),
                          child: Padding(
                            padding: const EdgeInsets.symmetric(
                              horizontal: 14,
                              vertical: 13,
                            ),
                            child: Row(
                              children: [
                                Icon(mobileTabIcons[index], size: 22),
                                const SizedBox(width: 13),
                                Text(
                                  index == 0 ? '主要聊天' : mobileTabLabels[index],
                                ),
                                if (index == 0 && mainSessionId != null) ...[
                                  const Spacer(),
                                  SessionGenerationIndicator(
                                    sessionId: mainSessionId!,
                                    generating: generatingSessionIds.contains(
                                      mainSessionId,
                                    ),
                                    completed: completedSessionIds.contains(
                                      mainSessionId,
                                    ),
                                  ),
                                ],
                              ],
                            ),
                          ),
                        ),
                      ),
                    ),
                  const SizedBox(height: 12),
                  Divider(color: colors.line),
                  const SizedBox(height: 16),
                  _groupLabel(context, '旁聊'),
                  const SizedBox(height: 8),
                  if (sideSessions.isEmpty)
                    Padding(
                      padding: const EdgeInsets.symmetric(
                        horizontal: 8,
                        vertical: 12,
                      ),
                      child: Text(
                        '你发起的旁聊会显示在这里。',
                        style: TextStyle(color: colors.muted, fontSize: 13),
                      ),
                    )
                  else
                    ListView.builder(
                      shrinkWrap: true,
                      physics: const NeverScrollableScrollPhysics(),
                      padding: EdgeInsets.zero,
                      itemCount: sideSessions.length,
                      itemBuilder: (context, index) {
                        final session = sideSessions[index];
                        final id = '${session['id'] ?? ''}';
                        final selected = tab == 0 && sessionId == id;
                        return Material(
                          key: ValueKey('menu-session-$id'),
                          color: selected ? colors.chip : Colors.transparent,
                          borderRadius: BorderRadius.circular(16),
                          child: InkWell(
                            borderRadius: BorderRadius.circular(16),
                            onTap: () => onSelectSession(id),
                            onLongPress: () => showSessionActions(
                              context,
                              session,
                              onRenameSession: onRenameSession,
                              onDeleteSession: onDeleteSession,
                            ),
                            child: Padding(
                              padding: const EdgeInsets.only(
                                left: 14,
                                top: 6,
                                bottom: 6,
                              ),
                              child: Row(
                                children: [
                                  Expanded(
                                    child: Column(
                                      crossAxisAlignment:
                                          CrossAxisAlignment.start,
                                      children: [
                                        Text(
                                          '${session['title'] ?? '新旁聊'}',
                                          maxLines: 1,
                                          overflow: TextOverflow.ellipsis,
                                        ),
                                        const SizedBox(height: 3),
                                        Text(
                                          _relativeTime(session),
                                          style: TextStyle(
                                            color: colors.muted,
                                            fontSize: 11,
                                          ),
                                        ),
                                      ],
                                    ),
                                  ),
                                  const SizedBox(width: 8),
                                  SessionGenerationIndicator(
                                    sessionId: id,
                                    generating: generatingSessionIds.contains(
                                      id,
                                    ),
                                    completed: completedSessionIds.contains(id),
                                  ),
                                  IconButton(
                                    key: ValueKey('menu-session-actions-$id'),
                                    tooltip: '旁聊操作',
                                    constraints: const BoxConstraints.tightFor(
                                      width: 48,
                                      height: 48,
                                    ),
                                    icon: const Icon(Icons.more_horiz),
                                    onPressed: () => showSessionActions(
                                      context,
                                      session,
                                      onRenameSession: onRenameSession,
                                      onDeleteSession: onDeleteSession,
                                    ),
                                  ),
                                ],
                              ),
                            ),
                          ),
                        );
                      },
                    ),
                ],
              ),
            ),
            const SizedBox(height: 12),
            Row(
              children: [
                Expanded(
                  child: GlassSurface(
                    shape: GlassShape.capsule,
                    child: InkWell(
                      key: const ValueKey('menu-search'),
                      borderRadius: BorderRadius.circular(999),
                      onTap: onSearch,
                      child: SizedBox(
                        height: 48,
                        child: Row(
                          children: [
                            const SizedBox(width: 14),
                            Icon(Icons.search, size: 20, color: colors.muted),
                            const SizedBox(width: 8),
                            Text('搜索', style: TextStyle(color: colors.muted)),
                          ],
                        ),
                      ),
                    ),
                  ),
                ),
                const SizedBox(width: 10),
                GlassSurface(
                  shape: GlassShape.circle,
                  child: IconButton(
                    tooltip: '新建旁聊',
                    onPressed: onNewSession,
                    constraints: const BoxConstraints.tightFor(
                      width: 48,
                      height: 48,
                    ),
                    icon: const Icon(Icons.edit_outlined, size: 22),
                  ),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }

  Widget _groupLabel(BuildContext context, String text) => Padding(
    padding: const EdgeInsets.only(left: 14),
    child: Text(
      text,
      style: TextStyle(color: context.muse.muted, fontSize: 12),
    ),
  );
}
