import 'package:flutter/material.dart';

import '../api.dart';
import '../preferences.dart';
import '../theme.dart';
import 'account.dart';
import 'memory.dart';
import 'proactive_settings.dart';
import 'usage.dart';

// Release scripts provide the same version and build number as the native app.
const _appVersion = String.fromEnvironment(
  'APP_VERSION',
  defaultValue: '1.0.0+1',
);

class SettingsView extends StatelessWidget {
  const SettingsView({
    super.key,
    required this.api,
    this.onLogout,
    this.onNotifications,
    this.onConnectors,
    this.onSelectSession,
    this.memories = const [],
    this.onRefresh,
  });

  final AssistantApi api;
  final VoidCallback? onLogout;
  final VoidCallback? onNotifications;
  final VoidCallback? onConnectors;
  final ValueChanged<String>? onSelectSession;
  final List<Map<String, dynamic>> memories;
  final Future<void> Function()? onRefresh;

  Future<void> _save(BuildContext context, Future<void> change) async {
    try {
      await change;
    } catch (_) {
      if (!context.mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(const SnackBar(content: Text('偏好保存失败，本次设置已生效')));
    }
  }

  Future<void> _logout(BuildContext context) async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (dialog) => AlertDialog(
        title: const Text('退出登录？'),
        content: const Text('退出后需要重新登录才能继续使用个人助理。'),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(dialog).pop(false),
            child: const Text('取消'),
          ),
          FilledButton(
            onPressed: () => Navigator.of(dialog).pop(true),
            child: const Text('退出'),
          ),
        ],
      ),
    );
    if (confirmed != true || !context.mounted) return;
    Navigator.of(context).pop();
    onLogout?.call();
  }

  @override
  Widget build(BuildContext context) {
    final controller = LumaPreferencesScope.controllerOf(context);
    final colors = context.muse;
    return Scaffold(
      backgroundColor: colors.bg,
      appBar: AppBar(
        title: const Text('设置'),
        backgroundColor: colors.bg,
        foregroundColor: colors.text,
        elevation: 0,
      ),
      body: ListView(
        key: const Key('settings-list'),
        padding: const EdgeInsets.fromLTRB(16, 8, 16, 32),
        children: [
          const _SettingsHeading('外观'),
          ValueListenableBuilder<AppearancePreferences>(
            valueListenable: controller,
            builder: (context, appearance, _) => _SettingsGroup(
              child: Padding(
                padding: const EdgeInsets.all(16),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    const Text('主题'),
                    const SizedBox(height: 10),
                    SizedBox(
                      width: double.infinity,
                      child: SegmentedButton<ThemeMode>(
                        key: const Key('appearance-theme'),
                        showSelectedIcon: false,
                        segments: const [
                          ButtonSegment(
                            value: ThemeMode.system,
                            label: Text('跟随系统'),
                          ),
                          ButtonSegment(
                            value: ThemeMode.light,
                            label: Text('浅色'),
                          ),
                          ButtonSegment(
                            value: ThemeMode.dark,
                            label: Text('深色'),
                          ),
                        ],
                        selected: {appearance.themeMode},
                        onSelectionChanged: (selection) => _save(
                          context,
                          controller.setThemeMode(selection.first),
                        ),
                      ),
                    ),
                    const SizedBox(height: 20),
                    const Text('主色'),
                    const SizedBox(height: 6),
                    Wrap(
                      spacing: 6,
                      children: [
                        for (final accent in lumaAccents)
                          Semantics(
                            label: '主色${accent.label}',
                            button: true,
                            selected: appearance.accent == accent.color,
                            child: Tooltip(
                              message: accent.label,
                              child: InkResponse(
                                key: Key('accent-${accent.value}'),
                                radius: 24,
                                onTap: () => _save(
                                  context,
                                  controller.setAccent(accent.color),
                                ),
                                child: SizedBox(
                                  width: 44,
                                  height: 48,
                                  child: Center(
                                    child: Container(
                                      width: 32,
                                      height: 32,
                                      decoration: BoxDecoration(
                                        color: accent.color,
                                        shape: BoxShape.circle,
                                      ),
                                      child: appearance.accent == accent.color
                                          ? const Icon(
                                              Icons.check_rounded,
                                              size: 20,
                                              color: Colors.white,
                                            )
                                          : null,
                                    ),
                                  ),
                                ),
                              ),
                            ),
                          ),
                      ],
                    ),
                    const SizedBox(height: 14),
                    const Text('字号'),
                    const SizedBox(height: 10),
                    SizedBox(
                      width: double.infinity,
                      child: SegmentedButton<MessageFontSize>(
                        key: const Key('appearance-font-size'),
                        showSelectedIcon: false,
                        segments: const [
                          ButtonSegment(
                            value: MessageFontSize.small,
                            label: Text('小'),
                          ),
                          ButtonSegment(
                            value: MessageFontSize.standard,
                            label: Text('标准'),
                          ),
                          ButtonSegment(
                            value: MessageFontSize.large,
                            label: Text('大'),
                          ),
                        ],
                        selected: {appearance.fontSize},
                        onSelectionChanged: (selection) => _save(
                          context,
                          controller.setFontSize(selection.first),
                        ),
                      ),
                    ),
                  ],
                ),
              ),
            ),
          ),
          const _SettingsHeading('账号'),
          _SettingsGroup(
            child: Column(
              children: [
                _SettingsLink(
                  key: const Key('settings-profile'),
                  icon: Icons.person_outline,
                  title: '编辑资料',
                  subtitle: '查看用户名、编辑邮箱和显示名',
                  onTap: () => Navigator.of(context).push<void>(
                    MaterialPageRoute(
                      builder: (_) => AccountProfileView(api: api),
                    ),
                  ),
                ),
                _SettingsLink(
                  key: const Key('settings-password'),
                  icon: Icons.lock_outline,
                  title: '修改密码',
                  onTap: () => Navigator.of(context).push<void>(
                    MaterialPageRoute(
                      builder: (_) => AccountPasswordView(api: api),
                    ),
                  ),
                ),
                _SettingsLink(
                  key: const Key('settings-delete-account'),
                  icon: Icons.person_remove_outlined,
                  title: '删除账户',
                  onTap: () => Navigator.of(context).push<void>(
                    MaterialPageRoute(
                      builder: (_) =>
                          AccountDeleteView(api: api, onLogout: onLogout),
                    ),
                  ),
                ),
                _SettingsLink(
                  icon: Icons.devices_outlined,
                  title: '登录设备',
                  subtitle: '管理有效的登录会话',
                  onTap: () => Navigator.of(context).push<void>(
                    MaterialPageRoute(
                      builder: (_) =>
                          AccountSessionsView(api: api, onLogout: onLogout),
                    ),
                  ),
                ),
                if (onConnectors != null)
                  _SettingsLink(
                    icon: Icons.hub_outlined,
                    title: '连接器',
                    subtitle: '管理 MCP 数据源和工具',
                    onTap: onConnectors!,
                  ),
              ],
            ),
          ),
          const _SettingsHeading('权限'),
          _SettingsGroup(
            child: Column(
              children: [
                _SettingsLink(
                  icon: Icons.admin_panel_settings_outlined,
                  title: '写入权限',
                  subtitle: '管理每次询问和始终允许',
                  onTap: () => Navigator.of(context).push<void>(
                    MaterialPageRoute(
                      builder: (_) =>
                          PermissionsSandboxView(api: api, showSandbox: false),
                    ),
                  ),
                ),
                if (onNotifications != null)
                  _SettingsLink(
                    icon: Icons.notifications_outlined,
                    title: '通知与提醒',
                    subtitle: '查看任务和后台作业的提醒',
                    onTap: onNotifications!,
                  ),
              ],
            ),
          ),
          const _SettingsHeading('沙箱工作区'),
          _SettingsGroup(
            child: _SettingsLink(
              icon: Icons.workspaces_outlined,
              title: '工作区状态',
              subtitle: '查看状态或重置持久工作区',
              onTap: () => Navigator.of(context).push<void>(
                MaterialPageRoute(
                  builder: (_) =>
                      PermissionsSandboxView(api: api, showPermissions: false),
                ),
              ),
            ),
          ),
          const _SettingsHeading('主动消息'),
          _SettingsGroup(
            child: _SettingsLink(
              key: const Key('settings-proactive'),
              icon: Icons.auto_awesome_outlined,
              title: '主动消息与动态',
              subtitle: '设置频率、时间段和关心的话题',
              onTap: () => Navigator.of(context).push<void>(
                MaterialPageRoute(
                  builder: (_) => ProactiveSettingsView(api: api),
                ),
              ),
            ),
          ),
          const _SettingsHeading('记忆'),
          _SettingsGroup(
            child: _SettingsLink(
              key: const Key('settings-memory'),
              icon: Icons.book_outlined,
              title: '管理记忆',
              subtitle: '查看和确认 Luma 记住的内容',
              onTap: () => Navigator.of(context).push<void>(
                MaterialPageRoute(
                  builder: (context) => Scaffold(
                    backgroundColor: context.muse.bg,
                    appBar: AppBar(title: const Text('记忆')),
                    body: MemoryView(
                      memories: memories,
                      api: api,
                      showFiles: false,
                      showHeader: false,
                      contentPadding: const EdgeInsets.all(16),
                      onRefresh: onRefresh,
                    ),
                  ),
                ),
              ),
            ),
          ),
          const _SettingsHeading('用量'),
          _SettingsGroup(
            child: UsageSection(
              api: api,
              onSelectSession: onSelectSession == null
                  ? null
                  : (id) {
                      Navigator.of(context).pop();
                      onSelectSession!(id);
                    },
            ),
          ),
          const _SettingsHeading('关于'),
          _SettingsGroup(
            child: Column(
              children: [
                const ListTile(
                  title: Text('Luma 个人助理'),
                  subtitle: Text('版本 $_appVersion'),
                ),
                if (onLogout != null)
                  _SettingsLink(
                    key: const Key('settings-logout'),
                    icon: Icons.logout_rounded,
                    title: '退出登录',
                    onTap: () => _logout(context),
                  ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _SettingsHeading extends StatelessWidget {
  const _SettingsHeading(this.label);

  final String label;

  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.fromLTRB(4, 20, 4, 10),
    child: Text(
      label,
      style: TextStyle(
        color: context.muse.muted,
        fontSize: 13,
        fontWeight: FontWeight.w600,
      ),
    ),
  );
}

class _SettingsGroup extends StatelessWidget {
  const _SettingsGroup({required this.child});

  final Widget child;

  @override
  Widget build(BuildContext context) => Material(
    color: context.muse.bubble,
    borderRadius: BorderRadius.circular(MuseMetrics.cardRadius),
    clipBehavior: Clip.antiAlias,
    child: child,
  );
}

class _SettingsLink extends StatelessWidget {
  const _SettingsLink({
    super.key,
    required this.icon,
    required this.title,
    required this.onTap,
    this.subtitle,
  });

  final IconData icon;
  final String title;
  final String? subtitle;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) => ListTile(
    leading: Icon(icon, color: context.muse.accent),
    title: Text(title),
    subtitle: subtitle == null
        ? null
        : Text(
            subtitle!,
            style: TextStyle(color: context.muse.muted, fontSize: 12),
          ),
    trailing: Icon(Icons.chevron_right_rounded, color: context.muse.muted),
    onTap: onTap,
  );
}

/// Account settings that require a server-side session.
///
/// Device tokens are intentionally not collected here. APNs/FCM token
/// acquisition needs native configuration and a push dependency, which is
/// outside this client-only change. Once a platform integration has a token,
/// it can call [AssistantApi.registerPushDevice].
// TODO(native-push): add APNs/FCM token acquisition with native configuration
// and dependencies in a platform-specific follow-up.
class AccountSessionsView extends StatefulWidget {
  const AccountSessionsView({super.key, required this.api, this.onLogout});

  final AssistantApi api;
  final VoidCallback? onLogout;

  @override
  State<AccountSessionsView> createState() => _AccountSessionsViewState();
}

class _AccountSessionsViewState extends State<AccountSessionsView> {
  List<Map<String, dynamic>> _sessions = const [];
  bool _loading = true;
  bool _busy = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    if (_busy) return;
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final sessions = await widget.api.listAuthSessions();
      if (!mounted) return;
      setState(() {
        _sessions = sessions;
        _loading = false;
      });
    } catch (error) {
      if (!mounted) return;
      setState(() {
        _loading = false;
        _error = _message(error, '登录设备列表加载失败');
      });
    }
  }

  String _message(Object error, String fallback) {
    if (error is AssistantApiException) return error.message;
    return fallback;
  }

  Future<void> _revoke(Map<String, dynamic> session) async {
    final id = session['id'];
    if (id is! String || id.isEmpty || _busy) return;
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (dialog) => AlertDialog(
        title: const Text('踢出这台设备？'),
        content: Text(_deviceTitle(session)),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(dialog).pop(false),
            child: const Text('取消'),
          ),
          FilledButton(
            onPressed: () => Navigator.of(dialog).pop(true),
            child: const Text('踢出'),
          ),
        ],
      ),
    );
    if (confirmed != true || !mounted) return;
    setState(() => _busy = true);
    try {
      final revoked = await widget.api.revokeAuthSession(id);
      if (!revoked) {
        throw const AssistantApiException('设备不存在或已退出');
      }
      if (!mounted) return;
      setState(() {
        _sessions = _sessions.where((item) => item['id'] != id).toList();
        _busy = false;
      });
      ScaffoldMessenger.of(context)
          .showSnackBar(const SnackBar(content: Text('已踢出该设备')));
      try {
        // Session rows do not identify the current device; validate the bearer
        // after revocation to also handle revoking this very device.
        await widget.api.currentUser();
      } on AssistantApiException catch (error) {
        if (error.statusCode == 401 && mounted) {
          Navigator.of(context).popUntil((route) => route.isFirst);
          if (widget.api.onUnauthorized == null) widget.onLogout?.call();
        }
      }
    } catch (error) {
      if (!mounted) return;
      setState(() => _busy = false);
      _showError(error, '登录设备退出失败');
    }
  }

  Future<void> _logoutAll() async {
    if (_busy) return;
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (dialog) => AlertDialog(
        title: const Text('退出所有设备？'),
        content: const Text('所有登录会话都会失效，需要重新登录。'),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(dialog).pop(false),
            child: const Text('取消'),
          ),
          FilledButton(
            onPressed: () => Navigator.of(dialog).pop(true),
            child: const Text('退出所有设备'),
          ),
        ],
      ),
    );
    if (confirmed != true || !mounted) return;
    setState(() => _busy = true);
    try {
      await widget.api.logoutAll();
      if (!mounted) return;
      // AuthGate owns secure-storage cleanup. The API method deliberately does
      // not erase the local bearer token before this successful response.
      Navigator.of(context).popUntil((route) => route.isFirst);
      widget.onLogout?.call();
    } catch (error) {
      if (!mounted) return;
      setState(() => _busy = false);
      _showError(error, '退出所有设备失败');
    }
  }

  void _showError(Object error, String fallback) {
    final message = _message(error, fallback);
    ScaffoldMessenger.of(context)
        .showSnackBar(SnackBar(content: Text(message)));
  }

  String _deviceTitle(Map<String, dynamic> session) {
    final client = '${session['client'] ?? session['client_type'] ?? ''}'
        .trim();
    final platform = '${session['platform'] ?? ''}'.trim();
    if (client.isNotEmpty && platform.isNotEmpty) return '$client · $platform';
    if (client.isNotEmpty) return client;
    if (platform.isNotEmpty) return platform;
    return '未命名设备';
  }

  String _lastUsed(Map<String, dynamic> session) {
    final value = session['last_used_at'] ?? session['created_at'];
    DateTime? date;
    if (value is num && value.isFinite && value > 0 && value < 8640000000000) {
      date = DateTime.fromMillisecondsSinceEpoch((value * 1000).round());
    } else if (value is String && value.isNotEmpty) {
      date = DateTime.tryParse(value);
    }
    if (date == null) return '最近使用时间未知';
    final local = date.toLocal();
    String two(int part) => part.toString().padLeft(2, '0');
    return '最近使用：${local.year}-${two(local.month)}-${two(local.day)} '
        '${two(local.hour)}:${two(local.minute)}';
  }

  @override
  Widget build(BuildContext context) {
    final colors = context.muse;
    return Scaffold(
      backgroundColor: colors.bg,
      appBar: AppBar(
        title: const Text('登录设备'),
        backgroundColor: colors.bg,
        foregroundColor: colors.text,
        elevation: 0,
      ),
      body: RefreshIndicator(
        onRefresh: _load,
        child: ListView(
          padding: const EdgeInsets.fromLTRB(16, 8, 16, 32),
          children: [
            Text(
              '管理仍然有效的登录会话。这里不会显示任何会话令牌。',
              style: TextStyle(color: colors.muted, fontSize: 13, height: 1.5),
            ),
            const SizedBox(height: 16),
            if (_loading)
              const Center(
                child: Padding(
                  padding: EdgeInsets.all(24),
                  child: CircularProgressIndicator(),
                ),
              )
            else if (_error != null)
              _ErrorCard(message: _error!, colors: colors, onRetry: _load)
            else if (_sessions.isEmpty)
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 32),
                child: Center(
                  child: Text(
                    '没有有效的登录设备',
                    style: TextStyle(color: colors.muted),
                  ),
                ),
              )
            else
              ..._sessions.map(
                (session) => Card(
                  elevation: 0,
                  color: colors.bubble,
                  margin: const EdgeInsets.only(bottom: 8),
                  child: ListTile(
                    leading: Icon(Icons.devices_outlined, color: colors.accent),
                    title: Text(_deviceTitle(session)),
                    subtitle: Text(_lastUsed(session)),
                    trailing: TextButton(
                      onPressed: _busy ? null : () => _revoke(session),
                      child: const Text('踢出'),
                    ),
                  ),
                ),
              ),
            const SizedBox(height: 16),
            OutlinedButton.icon(
              onPressed: _busy ? null : _logoutAll,
              icon: const Icon(Icons.logout),
              label: const Text('退出所有设备'),
            ),
          ],
        ),
      ),
    );
  }
}

class _ErrorCard extends StatelessWidget {
  const _ErrorCard({
    required this.message,
    required this.colors,
    required this.onRetry,
  });

  final String message;
  final MuseColors colors;
  final VoidCallback onRetry;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: colors.warn.withValues(alpha: 0.08),
        borderRadius: BorderRadius.circular(MuseMetrics.cardRadius),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(message, style: TextStyle(color: colors.warn)),
          const SizedBox(height: 8),
          TextButton(onPressed: onRetry, child: const Text('重试')),
        ],
      ),
    );
  }
}

/// Settings for server-managed write permissions and the durable sandbox.
///
/// Permission state is always read from the server; this view never stores or
/// displays connector credentials.
class PermissionsSandboxView extends StatefulWidget {
  const PermissionsSandboxView({
    super.key,
    required this.api,
    this.showPermissions = true,
    this.showSandbox = true,
  });

  final AssistantApi api;
  final bool showPermissions;
  final bool showSandbox;

  @override
  State<PermissionsSandboxView> createState() => _PermissionsSandboxViewState();
}

class _PermissionsSandboxViewState extends State<PermissionsSandboxView> {
  List<Map<String, dynamic>> _permissions = const [];
  Map<String, dynamic>? _sandbox;
  bool _loading = true;
  bool _busy = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    if (_busy) return;
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final results = await Future.wait<Object?>([
        widget.showPermissions
            ? widget.api.permissions()
            : Future<List<Map<String, dynamic>>>.value(const []),
        widget.showSandbox
            ? widget.api.sandboxStatus()
            : Future<Map<String, dynamic>?>.value(),
      ]);
      if (!mounted) return;
      setState(() {
        _permissions = (results[0] as List<Map<String, dynamic>>);
        _sandbox = results[1] as Map<String, dynamic>?;
        _loading = false;
      });
    } catch (error) {
      if (!mounted) return;
      setState(() {
        _loading = false;
        _error = error is AssistantApiException ? error.message : '设置加载失败';
      });
    }
  }

  Future<void> _setPermission(Map<String, dynamic> item, bool enabled) async {
    if (_busy || item['allow_always'] != true) return;
    final key = item['key'];
    if (key is! String || key.isEmpty) return;
    setState(() => _busy = true);
    try {
      final updated = await widget.api.setPermission(
        key,
        enabled ? 'always' : 'ask',
      );
      if (!mounted) return;
      setState(() {
        final index = _permissions.indexWhere((row) => row['key'] == key);
        if (index >= 0) _permissions[index] = updated;
        _busy = false;
      });
    } catch (error) {
      if (!mounted) return;
      setState(() => _busy = false);
      _showError(error, '权限更新失败');
    }
  }

  Future<void> _resetSandbox() async {
    if (_busy) return;
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (dialog) => AlertDialog(
        title: const Text('重置沙箱工作区？'),
        content: const Text('当前沙箱和工作区备份都会删除，之后会按需重新创建。'),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(dialog).pop(false),
            child: const Text('取消'),
          ),
          FilledButton(
            onPressed: () => Navigator.of(dialog).pop(true),
            child: const Text('重置'),
          ),
        ],
      ),
    );
    if (confirmed != true || !mounted) return;
    setState(() => _busy = true);
    try {
      await widget.api.resetSandbox();
      if (!mounted) return;
      setState(() {
        _sandbox = null;
        _busy = false;
      });
      ScaffoldMessenger.of(context)
          .showSnackBar(const SnackBar(content: Text('沙箱工作区已重置')));
    } catch (error) {
      if (!mounted) return;
      setState(() => _busy = false);
      _showError(error, '沙箱重置失败');
    }
  }

  void _showError(Object error, String fallback) {
    final message = error is AssistantApiException ? error.message : fallback;
    ScaffoldMessenger.of(context)
        .showSnackBar(SnackBar(content: Text(message)));
  }

  String _categoryLabel(Object? value) {
    switch ('$value') {
      case 'mcp':
        return '连接器权限';
      case 'sandbox':
        return '沙箱权限';
      case 'browser':
        return '浏览器权限';
      default:
        return 'Luma 权限';
    }
  }

  String _sandboxState() {
    final value = _sandbox?['state'] ?? _sandbox?['status'];
    switch ('$value') {
      case 'running':
        return '运行中';
      case 'paused':
        return '已暂停';
      default:
        return '空闲';
    }
  }

  @override
  Widget build(BuildContext context) {
    final colors = context.muse;
    return Scaffold(
      backgroundColor: colors.bg,
      appBar: AppBar(
        title: Text(
          widget.showPermissions && widget.showSandbox
              ? '权限与沙箱工作区'
              : widget.showPermissions
              ? '权限'
              : '沙箱工作区',
        ),
        backgroundColor: colors.bg,
        foregroundColor: colors.text,
        elevation: 0,
      ),
      body: RefreshIndicator(
        onRefresh: _load,
        child: ListView(
          padding: const EdgeInsets.fromLTRB(16, 8, 16, 32),
          children: [
            if (_loading)
              const Center(
                child: Padding(
                  padding: EdgeInsets.all(24),
                  child: CircularProgressIndicator(),
                ),
              )
            else if (_error != null)
              _ErrorCard(message: _error!, colors: colors, onRetry: _load)
            else ...[
              if (widget.showPermissions) ...[
                _settingsHeading('权限', colors),
                const SizedBox(height: 6),
                if (_permissions.isEmpty)
                  _mutedText('服务端暂未返回可配置权限。', colors)
                else
                  ..._permissions.map((item) => _permissionRow(item, colors)),
              ],
              if (widget.showSandbox) ...[
                if (widget.showPermissions) const SizedBox(height: 24),
                _settingsHeading('沙箱工作区', colors),
                const SizedBox(height: 6),
                _sandboxCard(colors),
              ],
            ],
          ],
        ),
      ),
    );
  }

  Widget _settingsHeading(String label, MuseColors colors) => Padding(
    padding: const EdgeInsets.only(top: 4),
    child: Text(
      label,
      style: TextStyle(
        color: colors.text,
        fontSize: 18,
        fontWeight: FontWeight.w600,
      ),
    ),
  );

  Widget _mutedText(String text, MuseColors colors) => Padding(
    padding: const EdgeInsets.symmetric(vertical: 12),
    child: Text(text, style: TextStyle(color: colors.muted, fontSize: 13)),
  );

  Widget _permissionRow(Map<String, dynamic> item, MuseColors colors) {
    final allowAlways = item['allow_always'] == true;
    final mode = '${item['mode'] ?? 'ask'}';
    final title = '${item['label'] ?? item['key'] ?? '未命名权限'}';
    final description = '${item['description'] ?? ''}'.trim();
    return Card(
      elevation: 0,
      color: colors.bubble,
      margin: const EdgeInsets.only(bottom: 8),
      child: SwitchListTile(
        value: mode == 'always',
        onChanged: allowAlways ? (value) => _setPermission(item, value) : null,
        title: Text(title),
        subtitle: Text(
          description.isEmpty
              ? '${_categoryLabel(item['category'])} · ${allowAlways ? '可始终允许' : '每次询问'}'
              : '$description\n${allowAlways ? '可始终允许' : '必须每次询问'}',
          style: TextStyle(color: colors.muted, fontSize: 12, height: 1.4),
        ),
        secondary: Icon(
          allowAlways ? Icons.lock_open_outlined : Icons.lock_outline,
          color: allowAlways ? colors.accent : colors.muted,
        ),
      ),
    );
  }

  Widget _sandboxCard(MuseColors colors) {
    final state = _sandboxState();
    final backup = _sandbox?['backup'];
    final backupMap = backup is Map ? Map<String, dynamic>.from(backup) : null;
    final details = <String>['状态：$state'];
    final lastSeen = _sandbox?['last_seen_at'];
    if (lastSeen is String && lastSeen.isNotEmpty) {
      details.add('最近使用：$lastSeen');
    }
    if (backupMap != null) {
      final size = backupMap['size_bytes'];
      if (size != null) details.add('工作区备份：$size 字节');
    }
    return Card(
      elevation: 0,
      color: colors.bubble,
      child: Padding(
        padding: const EdgeInsets.all(14),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              '每位用户独立保存工作区，沙箱闲置后会暂停。',
              style: TextStyle(color: colors.muted, fontSize: 13, height: 1.4),
            ),
            const SizedBox(height: 8),
            for (final detail in details)
              Padding(
                padding: const EdgeInsets.only(bottom: 3),
                child: Text(
                  detail,
                  style: TextStyle(color: colors.text, fontSize: 12),
                ),
              ),
            const SizedBox(height: 8),
            Align(
              alignment: Alignment.centerRight,
              child: OutlinedButton(
                onPressed: _busy ? null : _resetSandbox,
                child: const Text('重置工作区'),
              ),
            ),
          ],
        ),
      ),
    );
  }
}
