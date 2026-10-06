import 'package:flutter/material.dart';

import '../api.dart';
import '../theme.dart';

String _message(Object error, String fallback) =>
    error is AssistantApiException ? error.message : fallback;

class AccountProfileView extends StatefulWidget {
  const AccountProfileView({super.key, required this.api});
  final AssistantApi api;

  @override
  State<AccountProfileView> createState() => _AccountProfileViewState();
}

class _AccountProfileViewState extends State<AccountProfileView> {
  final _name = TextEditingController();
  final _email = TextEditingController();
  String _username = '';
  String? _error;
  bool _loading = true;
  bool _busy = false;

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void dispose() {
    _name.dispose();
    _email.dispose();
    super.dispose();
  }

  Future<void> _load() async {
    try {
      final user = await widget.api.currentUser();
      if (!mounted) return;
      setState(() {
        _username = '${user['username'] ?? ''}';
        _name.text = '${user['display_name'] ?? ''}';
        _email.text = '${user['email'] ?? ''}';
        _loading = false;
      });
    } catch (error) {
      if (mounted) {
        setState(() {
          _loading = false;
          _error = _message(error, '资料加载失败');
        });
      }
    }
  }

  Future<void> _save() async {
    if (_busy) return;
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await widget.api.updateProfile(
        displayName: _name.text,
        email: _email.text,
      );
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(const SnackBar(content: Text('资料已保存')));
    } catch (error) {
      if (mounted) setState(() => _error = _message(error, '资料保存失败'));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    backgroundColor: context.muse.bg,
    appBar: AppBar(title: const Text('编辑资料')),
    body: _loading
        ? const Center(child: CircularProgressIndicator())
        : ListView(
            padding: const EdgeInsets.all(24),
            children: [
              ListTile(
                contentPadding: EdgeInsets.zero,
                title: const Text('用户名'),
                subtitle: Text(_username),
              ),
              const SizedBox(height: 16),
              TextField(
                key: const Key('profile-display-name'),
                controller: _name,
                enabled: !_busy && _username.isNotEmpty,
                decoration: const InputDecoration(labelText: '显示名'),
              ),
              const SizedBox(height: 16),
              TextField(
                key: const Key('profile-email'),
                controller: _email,
                enabled: !_busy && _username.isNotEmpty,
                keyboardType: TextInputType.emailAddress,
                autocorrect: false,
                decoration: const InputDecoration(labelText: '邮箱（可选）'),
              ),
              const SizedBox(height: 24),
              if (_error != null)
                Text(_error!, style: TextStyle(color: context.muse.danger)),
              FilledButton(
                key: const Key('profile-save'),
                onPressed: _busy || _username.isEmpty ? null : _save,
                child: Text(_busy ? '保存中…' : '保存资料'),
              ),
            ],
          ),
  );
}

class AccountPasswordView extends StatefulWidget {
  const AccountPasswordView({super.key, required this.api});
  final AssistantApi api;

  @override
  State<AccountPasswordView> createState() => _AccountPasswordViewState();
}

class _AccountPasswordViewState extends State<AccountPasswordView> {
  final _form = GlobalKey<FormState>();
  final _current = TextEditingController();
  final _password = TextEditingController();
  final _confirmation = TextEditingController();
  String? _error;
  bool _busy = false;

  @override
  void dispose() {
    _current.dispose();
    _password.dispose();
    _confirmation.dispose();
    super.dispose();
  }

  Future<void> _save() async {
    if (_busy || !_form.currentState!.validate()) return;
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await widget.api.changePassword(_current.text, _password.text);
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(const SnackBar(content: Text('密码已修改，其他设备需要重新登录')));
      Navigator.of(context).pop();
    } catch (error) {
      if (mounted) setState(() => _error = _message(error, '密码修改失败'));
    } finally {
      if (mounted) {
        _current.clear();
        _password.clear();
        _confirmation.clear();
        setState(() => _busy = false);
      }
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    backgroundColor: context.muse.bg,
    appBar: AppBar(title: const Text('修改密码')),
    body: Form(
      key: _form,
      child: ListView(
        padding: const EdgeInsets.all(24),
        children: [
          TextFormField(
            key: const Key('password-current'),
            controller: _current,
            enabled: !_busy,
            obscureText: true,
            autocorrect: false,
            enableSuggestions: false,
            decoration: const InputDecoration(labelText: '当前密码'),
            validator: (value) => (value ?? '').isEmpty ? '请输入当前密码' : null,
          ),
          const SizedBox(height: 16),
          TextFormField(
            key: const Key('password-new'),
            controller: _password,
            enabled: !_busy,
            obscureText: true,
            autocorrect: false,
            enableSuggestions: false,
            decoration: const InputDecoration(labelText: '新密码'),
            validator: (value) =>
                (value ?? '').runes.length < 8 ||
                    (value ?? '').runes.length > 128
                ? '密码需为 8–128 位'
                : null,
          ),
          const SizedBox(height: 16),
          TextFormField(
            key: const Key('password-confirm'),
            controller: _confirmation,
            enabled: !_busy,
            obscureText: true,
            autocorrect: false,
            enableSuggestions: false,
            decoration: const InputDecoration(labelText: '确认新密码'),
            validator: (value) => value != _password.text ? '两次输入的密码不一致' : null,
          ),
          const SizedBox(height: 24),
          if (_error != null)
            Text(_error!, style: TextStyle(color: context.muse.danger)),
          FilledButton(
            key: const Key('password-save'),
            onPressed: _busy ? null : _save,
            child: Text(_busy ? '修改中…' : '修改密码'),
          ),
        ],
      ),
    ),
  );
}

class AccountDeleteView extends StatefulWidget {
  const AccountDeleteView({super.key, required this.api, this.onLogout});
  final AssistantApi api;
  final VoidCallback? onLogout;

  @override
  State<AccountDeleteView> createState() => _AccountDeleteViewState();
}

class _AccountDeleteViewState extends State<AccountDeleteView> {
  final _form = GlobalKey<FormState>();
  final _password = TextEditingController();
  String? _error;
  bool _busy = false;

  @override
  void dispose() {
    _password.dispose();
    super.dispose();
  }

  Future<void> _delete() async {
    if (_busy || !_form.currentState!.validate()) return;
    setState(() => _busy = true);
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (dialog) => AlertDialog(
        title: const Text('永久删除账户？'),
        content: const Text('账户及全部对话、文件、记忆和连接器会被永久删除，无法恢复。请再次确认。'),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(dialog).pop(false),
            child: const Text('取消'),
          ),
          FilledButton(
            key: const Key('account-delete-confirm'),
            onPressed: () => Navigator.of(dialog).pop(true),
            child: const Text('确认永久删除'),
          ),
        ],
      ),
    );
    if (!mounted) return;
    if (confirmed != true) {
      _password.clear();
      setState(() => _busy = false);
      return;
    }
    setState(() => _error = null);
    try {
      await widget.api.deleteAccount(_password.text);
      if (!mounted) return;
      Navigator.of(context).popUntil((route) => route.isFirst);
      widget.onLogout?.call();
    } catch (error) {
      if (mounted) setState(() => _error = _message(error, '账户删除失败'));
    } finally {
      if (mounted) {
        _password.clear();
        setState(() => _busy = false);
      }
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    backgroundColor: context.muse.bg,
    appBar: AppBar(title: const Text('删除账户')),
    body: Form(
      key: _form,
      child: ListView(
        padding: const EdgeInsets.all(24),
        children: [
          const Text('此操作会永久删除账户及全部数据，无法恢复。请输入密码后确认。'),
          const SizedBox(height: 24),
          TextFormField(
            key: const Key('account-delete-password'),
            controller: _password,
            enabled: !_busy,
            obscureText: true,
            autocorrect: false,
            enableSuggestions: false,
            decoration: const InputDecoration(labelText: '当前密码'),
            validator: (value) => (value ?? '').isEmpty ? '请输入密码' : null,
          ),
          const SizedBox(height: 24),
          if (_error != null)
            Text(_error!, style: TextStyle(color: context.muse.danger)),
          FilledButton(
            key: const Key('account-delete-submit'),
            onPressed: _busy ? null : _delete,
            style: FilledButton.styleFrom(backgroundColor: context.muse.danger),
            child: Text(_busy ? '删除中…' : '删除账户'),
          ),
        ],
      ),
    ),
  );
}
