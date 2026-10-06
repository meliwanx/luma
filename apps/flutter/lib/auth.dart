import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

import 'api.dart';
import 'brand.dart';
import 'glass.dart';
import 'home.dart';
import 'theme.dart';

class AuthRepository {
  AuthRepository({AssistantApi? api, FlutterSecureStorage? storage})
    : _api = api ?? AssistantApi(),
      _storage = storage ?? FlutterSecureStorage();

  static const _tokenKey = 'luma_access_token';
  final AssistantApi _api;
  final FlutterSecureStorage _storage;

  Future<Map<String, dynamic>> authConfig() => _api.authConfig();

  Future<String> login(String login, String password) async {
    final token = await _api.login(login, password);
    await saveToken(token);
    return token;
  }

  Future<String> register({
    required String username,
    required String password,
    String? email,
    String? displayName,
    String? inviteCode,
  }) async {
    final token = await _api.register(
      username: username,
      password: password,
      email: email,
      displayName: displayName,
      inviteCode: inviteCode,
    );
    await saveToken(token);
    return token;
  }

  void close() => _api.close();

  Future<String?> readToken() async {
    try {
      final token = await _storage.read(key: _tokenKey);
      _api.token = token;
      return token;
    } catch (_) {
      // Unsupported plugin targets and widget tests can render without storage.
      return null;
    }
  }

  Future<void> saveToken(String token) async {
    _api.token = token;
    try {
      await _storage.write(key: _tokenKey, value: token);
    } catch (_) {}
  }

  Future<void> logout() async {
    try {
      if (_api.token != null) await _api.logout();
    } finally {
      await clearToken();
    }
  }

  Future<void> clearToken() async {
    _api.token = null;
    try {
      await _storage.delete(key: _tokenKey);
    } catch (_) {}
  }
}

class AuthGate extends StatefulWidget {
  const AuthGate({super.key, this.repository});

  final AuthRepository? repository;

  @override
  State<AuthGate> createState() => _AuthGateState();
}

class _AuthGateState extends State<AuthGate> {
  late final AuthRepository _repository;
  String? _token;
  String? _error;
  bool _loading = true;
  bool _signingIn = false;
  bool _registrationOpen = false;
  bool _requiresInvite = false;

  @override
  void initState() {
    super.initState();
    _repository = widget.repository ?? AuthRepository();
    _restore();
  }

  @override
  void dispose() {
    if (widget.repository == null) _repository.close();
    super.dispose();
  }

  Future<void> _restore() async {
    final token = await _repository.readToken();
    if (!mounted) return;
    setState(() {
      _token = AssistantApi.hasConfiguredBase ? token : null;
      _error = AssistantApi.hasConfiguredBase
          ? null
          : AssistantApi.configurationMessage;
      _loading = false;
    });
    if (AssistantApi.hasConfiguredBase) await _loadConfig();
  }

  Future<void> _loadConfig() async {
    try {
      final config = await _repository.authConfig();
      if (!mounted) return;
      setState(() {
        _registrationOpen = config['registration_open'] == true;
        _requiresInvite = config['requires_invite'] == true;
      });
    } catch (error) {
      if (mounted && _token == null) {
        setState(
          () => _error = error is AssistantApiException
              ? error.message
              : '注册配置加载失败，请稍后重试',
        );
      }
    }
  }

  Future<void> _authenticate(Future<String> Function() action) async {
    if (_signingIn) return;
    setState(() {
      _signingIn = true;
      _error = null;
    });
    try {
      if (!AssistantApi.hasConfiguredBase) {
        throw AssistantApiException(AssistantApi.configurationMessage);
      }
      final token = await action();
      if (mounted) {
        TextInput.finishAutofillContext();
        setState(() => _token = token);
      }
    } catch (error) {
      if (mounted) {
        setState(
          () => _error = error is AssistantApiException
              ? error.message
              : '登录失败，请稍后重试',
        );
      }
    } finally {
      if (mounted) setState(() => _signingIn = false);
    }
  }

  Future<void> _logout() async {
    try {
      await _repository.logout();
    } catch (_) {
      // Local cleanup still completes if the server session has expired.
    }
    if (mounted) {
      setState(() {
        _token = null;
        _error = null;
      });
      await _loadConfig();
    }
  }

  @override
  Widget build(BuildContext context) {
    if (_loading) {
      return const Scaffold(body: Center(child: CircularProgressIndicator()));
    }
    if (_token != null && _token!.isNotEmpty) {
      return LumaHome(token: _token, onLogout: _logout);
    }
    return LoginPage(
      error: _error,
      busy: _signingIn,
      registrationOpen: _registrationOpen,
      requiresInvite: _requiresInvite,
      onLogin: (login, password) =>
          _authenticate(() => _repository.login(login, password)),
      onRegister: (username, password, email, displayName, inviteCode) =>
          _authenticate(
            () => _repository.register(
              username: username,
              password: password,
              email: email,
              displayName: displayName,
              inviteCode: inviteCode,
            ),
          ),
    );
  }
}

class LoginPage extends StatefulWidget {
  const LoginPage({
    super.key,
    this.error,
    required this.busy,
    this.registrationOpen = false,
    this.requiresInvite = false,
    required this.onLogin,
    required this.onRegister,
  });

  final String? error;
  final bool busy;
  final bool registrationOpen;
  final bool requiresInvite;
  final Future<void> Function(String login, String password) onLogin;
  final Future<void> Function(
    String username,
    String password,
    String? email,
    String? displayName,
    String? inviteCode,
  )
  onRegister;

  @override
  State<LoginPage> createState() => _LoginPageState();
}

class _LoginPageState extends State<LoginPage> {
  final _formKey = GlobalKey<FormState>();
  final _account = TextEditingController();
  final _password = TextEditingController();
  final _email = TextEditingController();
  final _displayName = TextEditingController();
  final _confirmation = TextEditingController();
  final _invite = TextEditingController();
  final _passwordFocus = FocusNode();
  bool _showPassword = false;
  bool _registering = false;
  bool _submitting = false;

  @override
  void didUpdateWidget(LoginPage oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!widget.registrationOpen) _registering = false;
  }

  @override
  void dispose() {
    for (final controller in [
      _account,
      _password,
      _email,
      _displayName,
      _confirmation,
      _invite,
    ]) {
      controller.dispose();
    }
    _passwordFocus.dispose();
    super.dispose();
  }

  void _selectTab(bool registering) {
    setState(() {
      _registering = registering;
      _password.clear();
      _confirmation.clear();
      _formKey.currentState?.reset();
    });
  }

  Future<void> _submit() async {
    if (widget.busy || _submitting || !AssistantApi.hasConfiguredBase) return;
    if (!_formKey.currentState!.validate()) return;
    FocusScope.of(context).unfocus();
    setState(() => _submitting = true);
    try {
      if (_registering) {
        await widget.onRegister(
          _account.text.trim(),
          _password.text,
          _email.text.trim(),
          _displayName.text.trim(),
          widget.requiresInvite ? _invite.text.trim() : null,
        );
      } else {
        await widget.onLogin(_account.text.trim(), _password.text);
      }
    } finally {
      if (mounted) {
        _password.clear();
        _confirmation.clear();
        setState(() => _submitting = false);
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final colors = context.muse;
    final busy = widget.busy || _submitting;
    final enabled = !busy && AssistantApi.hasConfiguredBase;
    InputDecoration decoration(String label, {String? hint, Widget? suffix}) =>
        InputDecoration(
          labelText: label,
          hintText: hint,
          suffixIcon: suffix,
          filled: true,
          fillColor: colors.composer,
          border: OutlineInputBorder(
            borderRadius: BorderRadius.circular(14),
            borderSide: BorderSide(color: colors.line),
          ),
          enabledBorder: OutlineInputBorder(
            borderRadius: BorderRadius.circular(14),
            borderSide: BorderSide(color: colors.line),
          ),
        );
    return Scaffold(
      backgroundColor: colors.bg,
      body: GestureDetector(
        behavior: HitTestBehavior.translucent,
        onTap: () => FocusScope.of(context).unfocus(),
        child: SafeArea(
          child: Center(
            child: SingleChildScrollView(
              keyboardDismissBehavior: ScrollViewKeyboardDismissBehavior.onDrag,
              padding: const EdgeInsets.all(24),
              child: ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: 420),
                child: GlassSurface(
                  padding: const EdgeInsets.all(28),
                  child: AutofillGroup(
                    onDisposeAction: AutofillContextAction.cancel,
                    child: Form(
                      key: _formKey,
                      child: Column(
                        mainAxisSize: MainAxisSize.min,
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          const LumaLogo(
                            key: ValueKey('login-logo'),
                            width: 112,
                          ),
                          const SizedBox(height: 22),
                          Text(
                            _registering ? '注册 Luma' : '登录 Luma',
                            style: const TextStyle(
                              fontSize: 28,
                              fontWeight: FontWeight.w700,
                            ),
                          ),
                          const SizedBox(height: 8),
                          Text(
                            '你的数据存放在你部署的服务器上。',
                            style: TextStyle(color: colors.muted, height: 1.5),
                          ),
                          const SizedBox(height: 20),
                          if (widget.registrationOpen) ...[
                            SizedBox(
                              width: double.infinity,
                              child: SegmentedButton<bool>(
                                key: const Key('auth-tabs'),
                                showSelectedIcon: false,
                                segments: const [
                                  ButtonSegment(
                                    value: false,
                                    label: Text('登录'),
                                  ),
                                  ButtonSegment(value: true, label: Text('注册')),
                                ],
                                selected: {_registering},
                                onSelectionChanged: enabled
                                    ? (selection) => _selectTab(selection.first)
                                    : null,
                              ),
                            ),
                            const SizedBox(height: 20),
                          ],
                          TextFormField(
                            key: const Key('login-account'),
                            controller: _account,
                            enabled: enabled,
                            autofillHints: const [AutofillHints.username],
                            autocorrect: false,
                            textInputAction: TextInputAction.next,
                            decoration: decoration(
                              _registering ? '用户名' : '用户名或邮箱',
                            ),
                            validator: (value) {
                              final account = (value ?? '').trim();
                              if (account.isEmpty) {
                                return _registering ? '请输入用户名' : '请输入用户名或邮箱';
                              }
                              if (_registering &&
                                  !RegExp(r'^[a-zA-Z0-9_.-]{3,32}$')
                                      .hasMatch(account)) {
                                return '用户名需为 3–32 位字母、数字或 _ . -';
                              }
                              return null;
                            },
                            onFieldSubmitted: (_) =>
                                _passwordFocus.requestFocus(),
                          ),
                          if (_registering) ...[
                            const SizedBox(height: 16),
                            TextFormField(
                              key: const Key('register-email'),
                              controller: _email,
                              enabled: enabled,
                              keyboardType: TextInputType.emailAddress,
                              autofillHints: const [AutofillHints.email],
                              autocorrect: false,
                              textInputAction: TextInputAction.next,
                              decoration: decoration('邮箱（可选）'),
                            ),
                            const SizedBox(height: 16),
                            TextFormField(
                              key: const Key('register-display-name'),
                              controller: _displayName,
                              enabled: enabled,
                              textInputAction: TextInputAction.next,
                              decoration: decoration('显示名（可选）'),
                            ),
                          ],
                          const SizedBox(height: 16),
                          TextFormField(
                            key: const Key('login-password'),
                            controller: _password,
                            focusNode: _passwordFocus,
                            enabled: enabled,
                            obscureText: !_showPassword,
                            autofillHints: [
                              _registering
                                  ? AutofillHints.newPassword
                                  : AutofillHints.password,
                            ],
                            autocorrect: false,
                            enableSuggestions: false,
                            textInputAction: _registering
                                ? TextInputAction.next
                                : TextInputAction.done,
                            decoration: decoration(
                              '密码',
                              suffix: IconButton(
                                tooltip: _showPassword ? '隐藏密码' : '显示密码',
                                onPressed: enabled
                                    ? () => setState(
                                        () => _showPassword = !_showPassword,
                                      )
                                    : null,
                                icon: Icon(
                                  _showPassword
                                      ? Icons.visibility_off_outlined
                                      : Icons.visibility_outlined,
                                ),
                              ),
                            ),
                            validator: (value) {
                              final password = value ?? '';
                              if (password.isEmpty) return '请输入密码';
                              if (_registering &&
                                  (password.runes.length < 8 ||
                                      password.runes.length > 128)) {
                                return '密码需为 8–128 位';
                              }
                              return null;
                            },
                            onFieldSubmitted: _registering
                                ? null
                                : (_) => _submit(),
                          ),
                          if (_registering) ...[
                            const SizedBox(height: 16),
                            TextFormField(
                              key: const Key('register-confirm-password'),
                              controller: _confirmation,
                              enabled: enabled,
                              obscureText: !_showPassword,
                              autocorrect: false,
                              enableSuggestions: false,
                              autofillHints: const [AutofillHints.newPassword],
                              textInputAction: widget.requiresInvite
                                  ? TextInputAction.next
                                  : TextInputAction.done,
                              decoration: decoration('确认密码'),
                              validator: (value) =>
                                  value != _password.text ? '两次输入的密码不一致' : null,
                              onFieldSubmitted: widget.requiresInvite
                                  ? null
                                  : (_) => _submit(),
                            ),
                            if (widget.requiresInvite) ...[
                              const SizedBox(height: 16),
                              TextFormField(
                                key: const Key('register-invite'),
                                controller: _invite,
                                enabled: enabled,
                                autocorrect: false,
                                textInputAction: TextInputAction.done,
                                decoration: decoration('邀请码'),
                                validator: (value) =>
                                    (value ?? '').trim().isEmpty
                                    ? '请输入邀请码'
                                    : null,
                                onFieldSubmitted: (_) => _submit(),
                              ),
                            ],
                          ],
                          const SizedBox(height: 16),
                          if (widget.error != null) ...[
                            Text(
                              widget.error!,
                              key: const Key('login-error'),
                              style: TextStyle(
                                color: colors.danger,
                                fontSize: 12,
                              ),
                            ),
                            const SizedBox(height: 12),
                          ],
                          SizedBox(
                            width: double.infinity,
                            child: FilledButton(
                              key: Key(
                                _registering
                                    ? 'register-submit'
                                    : 'login-submit',
                              ),
                              onPressed: enabled ? _submit : null,
                              style: FilledButton.styleFrom(
                                backgroundColor: colors.accent,
                                foregroundColor: Colors.white,
                              ),
                              child: busy
                                  ? const SizedBox(
                                      width: 20,
                                      height: 20,
                                      child: CircularProgressIndicator(
                                        strokeWidth: 2,
                                      ),
                                    )
                                  : Text(_registering ? '注册' : '登录'),
                            ),
                          ),
                        ],
                      ),
                    ),
                  ),
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}
