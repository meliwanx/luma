import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:webview_flutter/webview_flutter.dart';

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

  Future<Map<String, dynamic>> authProviders() => _api.authProviders();

  Future<String> ssoPasswordLogin(String account, String password) async {
    final token = await _api.ssoPasswordLogin(account, password);
    await saveToken(token);
    return token;
  }

  Future<Map<String, dynamic>> ssoStart({String nextPath = '/app'}) =>
      _api.ssoStart(nextPath: nextPath);

  Future<String> exchangeSso(SsoCallback callback) async {
    final token = await _api.ssoExchange(
      ticket: callback.ticket,
      state: callback.state,
    );
    await saveToken(token);
    return token;
  }

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
  bool _showLocalPassword = true;
  bool _showSsoRedirect = false;
  bool _showSsoCredentials = false;
  String _ssoLabel = '单点登录';
  String _accountLabel = '账号';
  String? _ssoOrigin;

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
      final payload = await _repository.authProviders();
      if (!mounted) return;
      _applyProviders(payload);
    } catch (_) {
      try {
        final config = await _repository.authConfig();
        if (!mounted) return;
        setState(() {
          _showLocalPassword = true;
          _showSsoRedirect = false;
          _showSsoCredentials = false;
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
  }

  void _applyProviders(Map<String, dynamic> payload) {
    final providers = payload['providers'];
    if (providers is! List) {
      throw const AssistantApiException('登录方式接口返回了无效响应');
    }
    var local = false;
    var redirect = false;
    var credentials = false;
    String? origin;
    for (final item in providers) {
      if (item is! Map) continue;
      if (item['kind'] == 'password') local = true;
      if (item['kind'] == 'redirect') {
        redirect = true;
        origin = AssistantApi.originOf(item['origin']?.toString());
      }
      if (item['kind'] == 'credentials') credentials = true;
    }
    final ssoLabel = payload['sso_label'];
    final accountLabel = payload['account_label'];
    setState(() {
      _showLocalPassword = local;
      _showSsoRedirect = redirect;
      _showSsoCredentials = credentials;
      _ssoLabel = ssoLabel is String && ssoLabel.trim().isNotEmpty
          ? ssoLabel.trim()
          : '单点登录';
      _accountLabel = accountLabel is String && accountLabel.trim().isNotEmpty
          ? accountLabel.trim()
          : '账号';
      _ssoOrigin = origin;
      _registrationOpen = local && payload['registration_open'] == true;
      _requiresInvite = local && payload['requires_invite'] == true;
    });
  }

  Future<void> _ssoSignIn() async {
    if (_signingIn) return;
    setState(() {
      _signingIn = true;
      _error = null;
    });
    try {
      if (!AssistantApi.hasConfiguredBase) {
        throw AssistantApiException(AssistantApi.configurationMessage);
      }
      final start = await _repository.ssoStart();
      final url = start['url'];
      final state = start['state'];
      if (url is! String || state is! String || state.isEmpty) {
        throw const AssistantApiException('单点登录地址无效');
      }
      if (_ssoOrigin != null && AssistantApi.originOf(url) != _ssoOrigin) {
        throw const AssistantApiException('单点登录地址与配置不匹配');
      }
      if (!mounted) return;
      final callback = await Navigator.of(context).push<SsoCallback>(
        MaterialPageRoute(
          builder: (_) => SsoWebView(loginUrl: url, expectedState: state),
        ),
      );
      if (callback == null || !mounted) return;
      final token = await _repository.exchangeSso(callback);
      if (mounted) setState(() => _token = token);
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
      showLocalPassword: _showLocalPassword,
      showSsoRedirect: _showSsoRedirect,
      showSsoCredentials: _showSsoCredentials,
      ssoLabel: _ssoLabel,
      accountLabel: _accountLabel,
      onSso: _ssoSignIn,
      onSsoPassword: (account, password) => _authenticate(
        () => _repository.ssoPasswordLogin(account, password),
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
    this.showLocalPassword = true,
    this.showSsoRedirect = false,
    this.showSsoCredentials = false,
    this.ssoLabel = '单点登录',
    this.accountLabel = '账号',
    this.onSso,
    this.onSsoPassword,
    required this.onLogin,
    required this.onRegister,
  });

  final String? error;
  final bool busy;
  final bool registrationOpen;
  final bool requiresInvite;
  final bool showLocalPassword;
  final bool showSsoRedirect;
  final bool showSsoCredentials;
  final String ssoLabel;
  final String accountLabel;
  final Future<void> Function()? onSso;
  final Future<void> Function(String account, String password)? onSsoPassword;
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
  final _ssoAccount = TextEditingController();
  final _ssoPassword = TextEditingController();
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
      _ssoAccount,
      _ssoPassword,
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

  Future<void> _submitSsoPassword() async {
    if (widget.busy || _submitting || widget.onSsoPassword == null) return;
    final account = _ssoAccount.text.trim();
    final password = _ssoPassword.text;
    if (account.isEmpty || password.isEmpty) return;
    FocusScope.of(context).unfocus();
    setState(() => _submitting = true);
    try {
      await widget.onSsoPassword!(account, password);
    } finally {
      if (mounted) {
        _ssoPassword.clear();
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
                          if (widget.showLocalPassword) ...[
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
                          if (widget.showSsoRedirect)
                            SizedBox(
                              width: double.infinity,
                              child: OutlinedButton(
                                key: const Key('sso-login'),
                                onPressed: enabled ? widget.onSso : null,
                                child: Text(widget.ssoLabel),
                              ),
                            ),
                          if (widget.showSsoCredentials) ...[
                            const SizedBox(height: 16),
                            TextFormField(
                              key: const Key('sso-account'),
                              controller: _ssoAccount,
                              enabled: enabled,
                              autocorrect: false,
                              textInputAction: TextInputAction.next,
                              maxLength: 64,
                              decoration: decoration(
                                widget.accountLabel,
                              ).copyWith(counterText: ''),
                            ),
                            const SizedBox(height: 16),
                            TextFormField(
                              key: const Key('sso-password'),
                              controller: _ssoPassword,
                              enabled: enabled,
                              obscureText: true,
                              autocorrect: false,
                              enableSuggestions: false,
                              textInputAction: TextInputAction.done,
                              maxLength: 128,
                              decoration: decoration('密码').copyWith(counterText: ''),
                              onFieldSubmitted: (_) => _submitSsoPassword(),
                            ),
                            const SizedBox(height: 16),
                            SizedBox(
                              width: double.infinity,
                              child: FilledButton(
                                key: const Key('sso-credentials-submit'),
                                onPressed: enabled ? _submitSsoPassword : null,
                                style: FilledButton.styleFrom(
                                  backgroundColor: colors.accent,
                                  foregroundColor: Colors.white,
                                ),
                                child: const Text('登录'),
                              ),
                            ),
                          ],
                          if (!widget.showLocalPassword && widget.error != null) ...[
                            const SizedBox(height: 12),
                            Text(
                              widget.error!,
                              key: const Key('login-error'),
                              style: TextStyle(color: colors.danger, fontSize: 12),
                            ),
                          ],
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

class SsoCallback {
  const SsoCallback({required this.ticket, this.state});

  final String ticket;
  final String? state;
}

bool isAppSsoCallback(Uri uri, String appBase) {
  final baseOrigin = AssistantApi.originOf(appBase);
  if (baseOrigin == null) return false;
  return AssistantApi.originOf(uri.toString()) == baseOrigin &&
      uri.path == '/sso-callback';
}

/// Reads a same-origin `/sso-callback`. Ticket-only callbacks are accepted.
/// A state, when present, must match [expectedState].
SsoCallback? readSsoCallback(
  Uri uri, {
  required String appBase,
  String? expectedState,
}) {
  if (!isAppSsoCallback(uri, appBase)) return null;
  final ticket = uri.queryParameters['ticket'] ?? '';
  if (ticket.isEmpty) return null;
  final state = uri.queryParameters['state'];
  if (state != null && state.isNotEmpty && state != expectedState) return null;
  return SsoCallback(ticket: ticket, state: state);
}

class SsoWebView extends StatefulWidget {
  const SsoWebView({
    super.key,
    required this.loginUrl,
    required this.expectedState,
  });

  final String loginUrl;
  final String expectedState;

  @override
  State<SsoWebView> createState() => _SsoWebViewState();
}

class _SsoWebViewState extends State<SsoWebView> {
  late final WebViewController _controller;
  String? _error;

  @override
  void initState() {
    super.initState();
    _controller = WebViewController()
      ..setJavaScriptMode(JavaScriptMode.unrestricted)
      ..setNavigationDelegate(
        NavigationDelegate(
          onNavigationRequest: (request) {
            final uri = Uri.tryParse(request.url);
            if (uri == null) return NavigationDecision.navigate;
            if (!isAppSsoCallback(uri, AssistantApi.base)) {
              return NavigationDecision.navigate;
            }
            final callback = readSsoCallback(
              uri,
              appBase: AssistantApi.base,
              expectedState: widget.expectedState,
            );
            if (callback == null) {
              setState(() => _error = '登录回调校验失败，请重新发起登录');
              return NavigationDecision.prevent;
            }
            Navigator.of(context).pop(callback);
            return NavigationDecision.prevent;
          },
        ),
      )
      ..loadRequest(Uri.parse(widget.loginUrl));
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('单点登录')),
      body: Column(
        children: [
          if (_error != null)
            Padding(
              padding: const EdgeInsets.all(12),
              child: Text(_error!, style: const TextStyle(color: Colors.redAccent)),
            ),
          Expanded(child: WebViewWidget(controller: _controller)),
        ],
      ),
    );
  }
}
