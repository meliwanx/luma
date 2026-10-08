import 'dart:async';
import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;

import 'audio_upload_stub.dart'
    if (dart.library.io) 'audio_upload_io.dart'
    as audio_upload;
import 'platform_version_stub.dart'
    if (dart.library.io) 'platform_version_io.dart'
    as platform_version;

class AssistantApi {
  static const _configuredBase = String.fromEnvironment('API_BASE_URL');

  AssistantApi({this.token, this.onUnauthorized, http.Client? client})
    : _client = client ?? http.Client();

  String? token;
  VoidCallback? onUnauthorized;

  /// The release build must receive its service URL explicitly. Debug builds
  /// use the local development server so the open-source checkout has no
  /// deployment-specific address baked into it.
  static String get base => _configuredBase.isNotEmpty
      ? _configuredBase
      : (kDebugMode ? 'http://localhost:8000' : '');

  /// Whether requests can be sent to a configured API endpoint.
  static bool get hasConfiguredBase => base.isNotEmpty;

  /// A missing release-time define is actionable on the login screen instead
  /// of causing a malformed relative URL to be sent by the HTTP client.
  static String get configurationMessage =>
      '此版本未配置服务器地址，需要使用 --dart-define=API_BASE_URL=... 构建';

  /// Normalize a service URL to an origin. HTTP is accepted only for loopback
  /// when [requireSecure] is set, so a redirect target cannot silently downgrade.
  static String? originOf(String? value, {bool requireSecure = false}) {
    final raw = (value ?? '').trim();
    if (raw.isEmpty) return null;
    final parsed = Uri.tryParse(raw);
    if (parsed == null || !parsed.hasScheme || parsed.host.isEmpty) {
      return null;
    }
    final scheme = parsed.scheme.toLowerCase();
    final host = parsed.host.toLowerCase();
    final localHost =
        host == 'localhost' || host == '127.0.0.1' || host == '::1';
    if (scheme != 'https' && scheme != 'http') return null;
    if (requireSecure && scheme != 'https' && !(scheme == 'http' && localHost)) {
      return null;
    }
    final port =
        parsed.hasPort &&
            !((scheme == 'https' && parsed.port == 443) ||
                (scheme == 'http' && parsed.port == 80))
        ? parsed.port
        : null;
    return Uri(scheme: scheme, host: host, port: port).toString();
  }

  Future<Map<String, dynamic>> authConfig() async {
    final result = await _accountRequest(
      'GET',
      '/api/v1/auth/config',
      '注册配置加载失败',
      authenticated: false,
    );
    if (result is! Map ||
        result['registration_open'] is! bool ||
        result['requires_invite'] is! bool) {
      throw const AssistantApiException('注册配置接口返回了无效响应');
    }
    return Map<String, dynamic>.from(result);
  }

  Future<Map<String, dynamic>> authProviders() async {
    final result = await _accountRequest(
      'GET',
      '/api/v1/auth/providers',
      '登录方式加载失败',
      authenticated: false,
    );
    if (result is! Map || result['providers'] is! List) {
      throw const AssistantApiException('登录方式接口返回了无效响应');
    }
    return Map<String, dynamic>.from(result);
  }

  Future<String> ssoPasswordLogin(String account, String password) async {
    try {
      final request = http.Request(
        'POST',
        _uri('/api/v1/auth/password/login'),
      )
        ..headers.addAll(_headers(json: true, authenticated: false))
        ..body = jsonEncode({'account': account, 'password': password});
      final response = await http.Response.fromStream(
        await _client.send(request).timeout(const Duration(seconds: 20)),
      );
      if (response.statusCode != 200) {
        final retry = response.headers['retry-after'];
        final message = response.statusCode == 401
            ? '账号或密码错误'
            : response.statusCode == 429
            ? (retry == '900'
                  ? '尝试次数过多，请 15 分钟后再试'
                  : '尝试次数过多，请稍后再试')
            : response.statusCode == 502 || response.statusCode == 503
            ? '登录服务暂时不可用'
            : '登录失败，请稍后重试';
        throw AssistantApiException(message, statusCode: response.statusCode);
      }
      return _sessionToken(_decode(response));
    } on AssistantApiException {
      rethrow;
    } catch (_) {
      throw const AssistantApiException('登录失败，请稍后重试');
    }
  }

  Future<Map<String, dynamic>> ssoStart({String nextPath = '/app'}) async {
    final safeNext = nextPath.startsWith('/') && !nextPath.startsWith('//')
        ? nextPath
        : '/app';
    final result = await _accountRequest(
      'GET',
      '/api/v1/auth/sso/start?next=${Uri.encodeQueryComponent(safeNext)}',
      '无法创建单点登录会话',
      authenticated: false,
    );
    if (result is! Map || result['url'] is! String || result['state'] is! String) {
      throw const AssistantApiException('单点登录地址无效');
    }
    return Map<String, dynamic>.from(result);
  }

  Future<String> ssoExchange({required String ticket, String? state}) async {
    final result = await _accountRequest(
      'POST',
      '/api/v1/auth/sso/exchange',
      '单点登录暂时不可用',
      authenticated: false,
      body: {
        'ticket': ticket,
        if (state != null && state.isNotEmpty) 'state': state,
      },
    );
    return _sessionToken(result);
  }

  Future<String> login(String login, String password) async {
    final result = await _accountRequest(
      'POST',
      '/api/v1/auth/login',
      '登录失败，请稍后重试',
      authenticated: false,
      body: {'login': login, 'password': password},
    );
    return _sessionToken(result);
  }

  Future<String> register({
    required String username,
    required String password,
    String? email,
    String? displayName,
    String? inviteCode,
  }) async {
    final result = await _accountRequest(
      'POST',
      '/api/v1/auth/register',
      '注册失败，请稍后重试',
      authenticated: false,
      body: {
        'username': username,
        'password': password,
        if (email != null && email.trim().isNotEmpty) 'email': email.trim(),
        if (displayName != null && displayName.trim().isNotEmpty)
          'display_name': displayName.trim(),
        if (inviteCode != null && inviteCode.trim().isNotEmpty)
          'invite_code': inviteCode.trim(),
      },
    );
    return _sessionToken(result);
  }

  String _sessionToken(dynamic result) {
    final value = result is Map ? result['access_token'] : null;
    if (result is! Map ||
        result['authenticated'] != true ||
        value is! String ||
        value.isEmpty) {
      throw const AssistantApiException('登录响应缺少会话令牌');
    }
    return value;
  }

  Future<Map<String, dynamic>> currentUser() async {
    final result = await _accountRequest('GET', '/api/v1/auth/me', '资料加载失败');
    if (result is! Map) {
      throw const AssistantApiException('资料接口返回了无效响应');
    }
    return Map<String, dynamic>.from(result);
  }

  Future<void> changePassword(
    String currentPassword,
    String newPassword,
  ) async {
    await _accountRequest(
      'POST',
      '/api/v1/account/password',
      '密码修改失败',
      body: {'current_password': currentPassword, 'new_password': newPassword},
    );
  }

  Future<void> updateProfile({String? displayName, String? email}) async {
    await _accountRequest(
      'PATCH',
      '/api/v1/account/profile',
      '资料保存失败',
      body: {
        if (displayName != null) 'display_name': displayName.trim(),
        if (email != null) 'email': email.trim(),
      },
    );
  }

  Future<void> deleteAccount(String password) async {
    await _accountRequest(
      'DELETE',
      '/api/v1/account',
      '账户删除失败',
      body: {'password': password},
    );
  }

  Future<void> logout() async {
    await _accountRequest('POST', '/api/v1/auth/logout', '退出登录失败');
  }

  Future<dynamic> _accountRequest(
    String method,
    String path,
    String fallback, {
    bool authenticated = true,
    Map<String, dynamic>? body,
  }) async {
    try {
      final request = http.Request(method, _uri(path))
        ..headers.addAll(
          _headers(json: body != null, authenticated: authenticated),
        );
      if (body != null) request.body = jsonEncode(body);
      final response = await http.Response.fromStream(
        await _client.send(request).timeout(const Duration(seconds: 20)),
      );
      // An incorrect current password also returns 401 and does not mean the
      // bearer session has expired. Do not sign the user out for mutations.
      if (authenticated && method == 'GET') _markUnauthorized(response);
      final decoded = _decode(response);
      if (response.statusCode < 200 || response.statusCode >= 300) {
        final detail = decoded is Map ? decoded['detail'] : null;
        throw AssistantApiException(
          detail is String && detail.isNotEmpty ? detail : fallback,
          statusCode: response.statusCode,
        );
      }
      return decoded;
    } on AssistantApiException {
      rethrow;
    } catch (_) {
      // Never reflect a transport exception, which can include request data.
      throw AssistantApiException(fallback);
    }
  }

  /// Fetch the user's durable main chat.
  ///
  /// Older servers do not expose `/sessions/main`; they return 404 and the
  /// caller can fall back to the first item from the regular session list.
  Future<Map<String, dynamic>?> mainSession() async {
    final response = await _client.get(
      _uri('/api/v1/sessions/main'),
      headers: _headers(),
    );
    _markUnauthorized(response);
    if (response.statusCode == 404) return null;
    final decoded = _decode(response);
    if (response.statusCode == 503) {
      _throwResponse(response, decoded, '服务暂时不可用');
    }
    if (response.statusCode != 200) {
      _throwResponse(response, decoded, '主聊天加载失败');
    }
    if (decoded is! Map) {
      throw const AssistantApiException('主聊天接口返回了无效响应');
    }
    return Map<String, dynamic>.from(decoded);
  }

  /// List the user's chat sessions.
  Future<List<Map<String, dynamic>>> listSessions({
    String? kind,
    int limit = 50,
  }) async {
    final query = <String, String>{'limit': '$limit'};
    if (kind != null && kind.trim().isNotEmpty) query['kind'] = kind.trim();
    final response = await _client.get(
      _uri('/api/v1/sessions').replace(queryParameters: query),
      headers: _headers(),
    );
    _markUnauthorized(response);
    final decoded = _decode(response);
    if (response.statusCode != 200) {
      _throwResponse(response, decoded, '会话列表加载失败');
    }
    if (decoded is! List) {
      throw const AssistantApiException('会话列表接口返回了无效响应');
    }
    return decoded
        .whereType<Map>()
        .map((item) => Map<String, dynamic>.from(item))
        .toList();
  }

  /// Fetch only the signed-in user's model usage aggregates.
  Future<Map<String, dynamic>> usage({String range = '7d'}) async {
    if (!const {'7d', '30d', '90d'}.contains(range)) {
      throw const AssistantApiException('不支持的用量范围');
    }
    final response = await _client.get(
      _uri('/api/v1/usage').replace(queryParameters: {'range': range}),
      headers: _headers(),
    );
    _markUnauthorized(response);
    final decoded = _decode(response);
    if (response.statusCode != 200) {
      _throwResponse(response, decoded, '用量加载失败');
    }
    if (decoded is! Map ||
        decoded['totals'] is! Map ||
        decoded['performance'] is! Map ||
        decoded['daily'] is! List ||
        decoded['purposes'] is! List ||
        decoded['models'] is! List ||
        decoded['top_sessions'] is! List) {
      throw const AssistantApiException('用量接口返回了无效响应');
    }
    return Map<String, dynamic>.from(decoded);
  }

  /// Search plain-text conversation titles and message snippets.
  Future<SearchResults> search(String q) async {
    final response = await _client.get(
      _uri('/api/v1/search').replace(queryParameters: {'q': q.trim()}),
      headers: _headers(),
    );
    _markUnauthorized(response);
    final decoded = _decode(response);
    if (response.statusCode != 200) {
      _throwResponse(response, decoded, '搜索失败');
    }
    if (decoded is! Map ||
        decoded['query'] is! String ||
        decoded['sessions'] is! List ||
        decoded['messages'] is! List) {
      throw const AssistantApiException('搜索接口返回了无效响应');
    }
    List<Map<String, dynamic>> rows(String key) => (decoded[key] as List)
        .whereType<Map>()
        .map((item) => Map<String, dynamic>.from(item))
        .toList();
    return SearchResults(
      query: decoded['query'] as String,
      sessions: rows('sessions'),
      messages: rows('messages'),
    );
  }

  /// Upload a recording for transcription without sending a chat message.
  Future<Map<String, dynamic>> transcribe(
    Object audio, {
    String? sessionId,
    bool raw = false,
  }) async {
    try {
      final bytes = await audio_upload.readAudioBytes(audio);
      if (bytes.isEmpty) {
        throw const AssistantApiException('录音为空，请重新录音');
      }
      final request =
          http.MultipartRequest('POST', _uri('/api/v1/voice/transcribe'))
            ..headers.addAll(_headers())
            ..fields['mode'] = raw ? 'raw' : 'smart'
            ..files.add(
              http.MultipartFile.fromBytes(
                'audio',
                bytes,
                filename: audio_upload.audioFilename(audio),
              ),
            );
      if (sessionId != null && sessionId.trim().isNotEmpty) {
        request.fields['session_id'] = sessionId.trim();
      }
      final response = await http.Response.fromStream(
        await _client.send(request),
      );
      _markUnauthorized(response);
      final decoded = _decode(response);
      if (response.statusCode != 200) {
        final detail = decoded is Map ? decoded['detail'] : null;
        if (detail is String && detail.isNotEmpty) {
          throw AssistantApiException(detail, statusCode: response.statusCode);
        }
        _throwResponse(response, decoded, '语音识别失败');
      }
      if (decoded is! Map ||
          decoded['text'] is! String ||
          decoded['transcript'] is! String ||
          decoded['cleaned'] is! bool) {
        throw const AssistantApiException('语音识别接口返回了无效响应');
      }
      return Map<String, dynamic>.from(decoded);
    } on AssistantApiException {
      rethrow;
    } catch (_) {
      throw const AssistantApiException('语音识别失败，请稍后重试');
    }
  }

  Future<Map<String, dynamic>> library({
    String type = 'all',
    String q = '',
    String sort = 'recent',
    int limit = 50,
    String? cursor,
  }) async {
    final query = <String, String>{
      'type': type,
      'q': q.trim(),
      'sort': sort,
      'limit': '$limit',
      if (cursor != null && cursor.isNotEmpty) 'cursor': cursor,
    };
    final result = await _optionalResourceMap(
      'GET',
      '/api/v1/library',
      '资源库加载失败',
      query: query,
      empty: const {
        'items': <Map<String, dynamic>>[],
        'next_cursor': null,
        'counts': {
          'all': 0,
          'document': 0,
          'sheet': 0,
          'web': 0,
          'image': 0,
          'code': 0,
          'archive': 0,
          'other': 0,
        },
      },
    );
    if (result['items'] is! List || result['counts'] is! Map) {
      throw const AssistantApiException('资源库接口返回了无效响应');
    }
    return result;
  }

  Future<Map<String, dynamic>> libraryPreview(String id) async {
    final result = await _optionalResourceMap(
      'GET',
      '/api/v1/library/${Uri.encodeComponent(id)}/preview',
      '预览加载失败',
      empty: const {'kind': 'none'},
    );
    final kind = result['kind'];
    final valid =
        kind == 'image' ||
        kind == 'none' ||
        (kind == 'csv' &&
            result['columns'] is List &&
            result['rows'] is List) ||
        (const {'markdown', 'text', 'html_source'}.contains(kind) &&
            result['text'] is String);
    if (!valid) throw const AssistantApiException('预览接口返回了无效响应');
    return result;
  }

  Future<Map<String, dynamic>> updateLibrary(
    String id, {
    String? title,
    bool? pinned,
  }) => _optionalResourceMap(
    'PATCH',
    '/api/v1/library/${Uri.encodeComponent(id)}',
    '资源更新失败',
    body: {'title': ?title, 'pinned': ?pinned},
  );

  Future<void> deleteFile(String id) => _optionalResourceAction(
    'DELETE',
    '/api/v1/files/${Uri.encodeComponent(id)}',
    '删除文件失败',
  );

  Future<List<Map<String, dynamic>>> listMemories({int limit = 200}) =>
      _resourceList('/api/v1/memories', {'limit': '$limit'}, '记忆加载失败');

  Future<Map<String, dynamic>> ideas() async {
    final result = await _optionalResourceMap(
      'GET',
      '/api/v1/ideas',
      '点子加载失败',
      empty: const {
        'featured': <Map<String, dynamic>>[],
        'groups': <Map<String, dynamic>>[],
        'generated_at': null,
        'generating': false,
      },
    );
    if (result['featured'] is! List ||
        result['groups'] is! List ||
        result['generating'] is! bool) {
      throw const AssistantApiException('点子接口返回了无效响应');
    }
    return result;
  }

  Future<void> refreshIdeas() =>
      _optionalResourceAction('POST', '/api/v1/ideas/refresh', '点子生成失败');

  Future<void> ideaFeedback(String id, String action) {
    if (!const {'more_like', 'not_interested'}.contains(action)) {
      throw const AssistantApiException('不支持的点子反馈');
    }
    return _optionalResourceAction(
      'POST',
      '/api/v1/ideas/${Uri.encodeComponent(id)}/feedback',
      '点子反馈失败',
      body: {'action': action},
    );
  }

  Future<Map<String, dynamic>> startIdea(String id) async {
    final result = await _optionalResourceMap(
      'POST',
      '/api/v1/ideas/${Uri.encodeComponent(id)}/start',
      '点子启动失败',
    );
    if (result.isNotEmpty &&
        (result['session_id'] is! String || result['prompt'] is! String)) {
      throw const AssistantApiException('点子启动接口返回了无效响应');
    }
    return result;
  }

  Future<Map<String, dynamic>> feed({int limit = 20, String? cursor}) async {
    final result = await _optionalResourceMap(
      'GET',
      '/api/v1/feed',
      '动态加载失败',
      query: {
        'limit': '$limit',
        if (cursor != null && cursor.isNotEmpty) 'cursor': cursor,
      },
      empty: const {'items': <Map<String, dynamic>>[], 'next_cursor': null},
    );
    if (result['items'] is! List) {
      throw const AssistantApiException('动态接口返回了无效响应');
    }
    return result;
  }

  Future<void> feedFeedback(String id, String action) {
    if (!const {'like', 'unlike', 'not_interested'}.contains(action)) {
      throw const AssistantApiException('不支持的动态反馈');
    }
    return _optionalResourceAction(
      'POST',
      '/api/v1/feed/${Uri.encodeComponent(id)}/feedback',
      '动态反馈失败',
      body: {'action': action},
    );
  }

  Future<void> deleteFeedPost(String id) => _optionalResourceAction(
    'DELETE',
    '/api/v1/feed/${Uri.encodeComponent(id)}',
    '删除动态失败',
  );

  Future<Map<String, dynamic>> discussFeedPost(String id) async {
    final result = await _optionalResourceMap(
      'POST',
      '/api/v1/feed/${Uri.encodeComponent(id)}/discuss',
      '创建讨论失败',
    );
    if (result.isNotEmpty && result['session_id'] is! String) {
      throw const AssistantApiException('动态讨论接口返回了无效响应');
    }
    return result;
  }

  Future<void> refreshFeed() =>
      _optionalResourceAction('POST', '/api/v1/feed/refresh', '动态生成失败');

  Future<Map<String, dynamic>> proactivePrefs() =>
      _optionalResourceMap('GET', '/api/v1/proactive/prefs', '主动消息设置加载失败');

  Future<Map<String, dynamic>> updateProactivePrefs(
    Map<String, dynamic> prefs,
  ) => _optionalResourceMap(
    'PUT',
    '/api/v1/proactive/prefs',
    '主动消息设置保存失败',
    body: prefs,
  );

  /// New resource endpoints remain optional while older servers are deployed.
  /// Only 404 is compatible; auth failures and other errors retain their status.
  Future<Map<String, dynamic>> _optionalResourceMap(
    String method,
    String path,
    String fallback, {
    Map<String, String>? query,
    Map<String, dynamic>? body,
    Map<String, dynamic> empty = const {},
  }) async {
    final response = await _optionalResourceRequest(
      method,
      path,
      query: query,
      body: body,
    );
    if (response.statusCode == 404) return Map<String, dynamic>.from(empty);
    final decoded = _decode(response);
    if (response.statusCode < 200 || response.statusCode >= 300) {
      _throwResponse(response, decoded, fallback);
    }
    if (decoded is! Map) {
      throw AssistantApiException('$fallback：接口返回了无效响应');
    }
    return Map<String, dynamic>.from(decoded);
  }

  Future<void> _optionalResourceAction(
    String method,
    String path,
    String fallback, {
    Map<String, dynamic>? body,
  }) async {
    final response = await _optionalResourceRequest(method, path, body: body);
    if (response.statusCode == 404) return;
    if (response.statusCode < 200 || response.statusCode >= 300) {
      _throwResponse(response, _decode(response), fallback);
    }
  }

  Future<http.Response> _optionalResourceRequest(
    String method,
    String path, {
    Map<String, String>? query,
    Map<String, dynamic>? body,
  }) async {
    final request = http.Request(
      method,
      _uri(path).replace(queryParameters: query),
    )..headers.addAll(_headers(json: body != null));
    if (body != null) request.body = jsonEncode(body);
    final response = await http.Response.fromStream(
      await _client.send(request),
    );
    _markUnauthorized(response);
    return response;
  }

  Future<List<Map<String, dynamic>>> listFiles({
    String? sessionId,
    int limit = 100,
  }) async {
    final query = <String, String>{'limit': '$limit'};
    if (sessionId != null && sessionId.trim().isNotEmpty) {
      query['session_id'] = sessionId.trim();
    }
    return _resourceList('/api/v1/files', query, '文件列表加载失败');
  }

  Future<List<Map<String, dynamic>>> listNotifications({
    bool unreadOnly = false,
    int limit = 100,
  }) async {
    final query = <String, String>{'limit': '$limit'};
    if (unreadOnly) query['unread_only'] = 'true';
    return _resourceList('/api/v1/notifications', query, '通知列表加载失败');
  }

  Future<List<Map<String, dynamic>>> _resourceList(
    String path,
    Map<String, String> query,
    String fallback,
  ) async {
    final response = await _client.get(
      _uri(path).replace(queryParameters: query),
      headers: _headers(),
    );
    _markUnauthorized(response);
    if (response.statusCode == 404) return const <Map<String, dynamic>>[];
    final decoded = _decode(response);
    if (response.statusCode != 200) {
      _throwResponse(response, decoded, fallback);
    }
    if (decoded is! List) {
      throw AssistantApiException('$fallback：接口返回了无效响应');
    }
    return decoded
        .whereType<Map>()
        .map((item) => Map<String, dynamic>.from(item))
        .toList();
  }

  Future<Map<String, dynamic>> markNotificationRead(String id) async {
    final response = await _client.post(
      _uri('/api/v1/notifications/${Uri.encodeComponent(id)}/read'),
      headers: _headers(),
    );
    _markUnauthorized(response);
    final decoded = _decode(response);
    if (response.statusCode != 200) {
      _throwResponse(response, decoded, '通知更新失败');
    }
    if (decoded is! Map) {
      throw const AssistantApiException('通知接口返回了无效响应');
    }
    return Map<String, dynamic>.from(decoded);
  }

  /// Create a side chat. The API deliberately accepts only `kind: side` for
  /// creation; the main chat is created or returned by [mainSession].
  Future<Map<String, dynamic>> createSession({
    String? title,
    String kind = 'side',
  }) async {
    final body = <String, dynamic>{'kind': kind};
    if (title != null && title.trim().isNotEmpty) body['title'] = title.trim();
    final response = await _client.post(
      _uri('/api/v1/sessions'),
      headers: _headers(json: true),
      body: jsonEncode(body),
    );
    _markUnauthorized(response);
    final decoded = _decode(response);
    if (response.statusCode != 200 && response.statusCode != 201) {
      _throwResponse(response, decoded, '创建会话失败');
    }
    if (decoded is! Map) {
      throw const AssistantApiException('创建会话接口返回了无效响应');
    }
    return Map<String, dynamic>.from(decoded);
  }

  /// Resolve a session through the server's ownership check before navigation.
  Future<Map<String, dynamic>> getSession(String sessionId) async {
    final response = await _client
        .get(
          _uri('/api/v1/sessions/${Uri.encodeComponent(sessionId)}'),
          headers: _headers(),
        )
        .timeout(const Duration(seconds: 20));
    _markUnauthorized(response);
    final decoded = _decode(response);
    if (response.statusCode != 200) {
      _throwResponse(response, decoded, '会话已删除或无法访问');
    }
    if (decoded is! Map || decoded['id'] != sessionId) {
      throw const AssistantApiException('会话已删除或无法访问');
    }
    return Map<String, dynamic>.from(decoded);
  }

  Future<void> deleteSession(String sessionId) async {
    final response = await _client.delete(
      _uri('/api/v1/sessions/${Uri.encodeComponent(sessionId)}'),
      headers: _headers(),
    );
    _markUnauthorized(response);
    final decoded = _decode(response);
    if (response.statusCode < 200 || response.statusCode >= 300) {
      _throwResponse(response, decoded, '删除会话失败');
    }
  }

  Future<Map<String, dynamic>> renameSession(
    String sessionId,
    String title,
  ) async {
    final response = await _client.patch(
      _uri('/api/v1/sessions/${Uri.encodeComponent(sessionId)}'),
      headers: _headers(json: true),
      body: jsonEncode({'title': title.trim()}),
    );
    _markUnauthorized(response);
    final decoded = _decode(response);
    if (response.statusCode != 200) {
      _throwResponse(response, decoded, '重命名会话失败');
    }
    if (decoded is! Map) {
      throw const AssistantApiException('重命名会话接口返回了无效响应');
    }
    return Map<String, dynamic>.from(decoded);
  }

  /// Select a session for the dashboard. An explicit selection always wins;
  /// otherwise the durable main chat is preferred, then the first session is
  /// used for compatibility with servers predating `/sessions/main`.
  static String? selectSessionId({
    String? selectedSessionId,
    Map<String, dynamic>? mainSession,
    List<Map<String, dynamic>> sessions = const <Map<String, dynamic>>[],
  }) {
    final selected = selectedSessionId?.trim();
    if (selected != null && selected.isNotEmpty) return selected;
    final mainId = mainSession?['id'];
    if (mainId is String && mainId.trim().isNotEmpty) return mainId;
    for (final session in sessions) {
      final id = session['id'];
      if (id is String && id.trim().isNotEmpty) return id;
    }
    return null;
  }

  /// Fetch the same versioned resources used by the web client.
  ///
  /// The old `/api/dashboard` endpoint is intentionally not used here. The
  /// selected session is stable on the client, while a missing selected
  /// session falls back to the durable main chat.
  Future<Map<String, dynamic>?> dashboard({String? selectedSessionId}) async {
    try {
      late final List<Map<String, dynamic>> sessions;
      try {
        sessions = await listSessions();
      } on AssistantApiException catch (error) {
        if (error.statusCode == 503) rethrow;
        return null;
      }
      Map<String, dynamic>? main;
      if (selectedSessionId == null || selectedSessionId.trim().isEmpty) {
        try {
          main = await mainSession();
        } on AssistantApiException catch (error) {
          if (error.statusCode == 503) rethrow;
          return null;
        }
      }
      var currentId = selectSessionId(
        selectedSessionId: selectedSessionId,
        mainSession: main,
        sessions: sessions,
      );
      if (currentId == null) return null;

      var responses = await _dashboardResponses(currentId);
      var selectionFallback = false;
      if (responses[0].statusCode == 404 &&
          selectedSessionId != null &&
          selectedSessionId.trim().isNotEmpty) {
        // A selected side chat may have been removed by another client. Ask
        // for main and use the legacy first-session fallback if needed.
        try {
          main ??= await mainSession();
        } on AssistantApiException catch (error) {
          if (error.statusCode == 503) rethrow;
          return null;
        }
        final fallbackId = selectSessionId(
          mainSession: main,
          sessions: sessions,
        );
        if (fallbackId == null || fallbackId == currentId) return null;
        currentId = fallbackId;
        selectionFallback = true;
        responses = await _dashboardResponses(currentId);
      }
      for (final response in responses) {
        _markUnauthorized(response);
        if (response.statusCode == 503) {
          _throwResponse(response, _decode(response), '服务暂时不可用');
        }
      }
      if (responses.any((response) => response.statusCode != 200)) return null;
      return {
        'conversation_id': currentId,
        'selection_fallback': selectionFallback,
        'sessions': sessions,
        'messages': jsonDecode(responses[0].body),
        'messages_has_more':
            responses[0].headers['x-has-more']?.toLowerCase() == 'true',
        'messages_oldest_id': responses[0].headers['x-oldest-id'],
        'tasks': jsonDecode(responses[1].body),
        'memories': jsonDecode(responses[2].body),
        'runtime_jobs': jsonDecode(responses[3].body),
        'approvals': jsonDecode(responses[4].body),
        'runtime_activity': jsonDecode(responses[5].body),
      };
    } on AssistantApiException {
      rethrow;
    } catch (_) {}
    return null;
  }

  Future<List<http.Response>> _dashboardResponses(String sessionId) async {
    final encodedId = Uri.encodeComponent(sessionId);
    return Future.wait([
      _client.get(
        _uri('/api/v1/sessions/$encodedId/messages?limit=500'),
        headers: _headers(),
      ),
      _client.get(_uri('/api/v1/tasks?limit=200'), headers: _headers()),
      _client.get(_uri('/api/v1/memories?limit=200'), headers: _headers()),
      _client.get(_uri('/api/v1/runtime/jobs?limit=50'), headers: _headers()),
      _client.get(
        _uri('/api/v1/runtime/approvals?limit=50'),
        headers: _headers(),
      ),
      _client.get(
        _uri('/api/v1/runtime/activity?limit=50'),
        headers: _headers(),
      ),
    ]);
  }

  /// Fetch one page of messages in oldest-to-newest order.
  Future<MessagePage> listMessages(
    String sessionId, {
    int limit = 100,
    String? before,
  }) async {
    final query = <String, String>{'limit': '$limit'};
    if (before != null && before.isNotEmpty) query['before'] = before;
    final uri = _uri(
      '/api/v1/sessions/${Uri.encodeComponent(sessionId)}/messages',
    ).replace(queryParameters: query);
    final response = await _client.get(uri, headers: _headers());
    _markUnauthorized(response);
    final decoded = _decode(response);
    if (response.statusCode != 200) {
      _throwResponse(response, decoded, '消息加载失败');
    }
    if (decoded is! List) {
      throw const AssistantApiException('消息接口返回了无效响应');
    }
    return MessagePage(
      messages: decoded
          .whereType<Map>()
          .map((item) => Map<String, dynamic>.from(item))
          .toList(),
      hasMore: response.headers['x-has-more']?.toLowerCase() == 'true',
      oldestId: response.headers['x-oldest-id'],
    );
  }

  Future<Map<String, dynamic>?> systemStatus() async {
    try {
      final response = await http.get(
        _uri('/api/v1/system/status'),
        headers: _headers(),
      );
      _markUnauthorized(response);
      if (response.statusCode == 200) {
        return jsonDecode(response.body) as Map<String, dynamic>;
      }
      if (response.statusCode == 503) {
        _throwResponse(response, _decode(response), '服务暂时不可用');
      }
    } on AssistantApiException {
      rethrow;
    } catch (_) {}
    return null;
  }

  /// Return the permission entries available to the current user.
  ///
  /// Older servers do not expose the permissions endpoint yet. Treating a
  /// legacy 404 as an empty list lets the settings page continue to load while
  /// newer servers can return the normal `{items: [...]}` envelope.
  Future<List<Map<String, dynamic>>> permissions() async {
    final response = await _client.get(
      _uri('/api/v1/permissions'),
      headers: _headers(),
    );
    _markUnauthorized(response);
    if (response.statusCode == 404) return const <Map<String, dynamic>>[];
    final decoded = _decode(response);
    if (response.statusCode != 200) {
      _throwResponse(response, decoded, '权限列表加载失败');
    }
    final rows = decoded is Map && decoded['items'] is List
        ? decoded['items'] as List
        : decoded is List
        ? decoded
        : const <dynamic>[];
    return rows
        .whereType<Map>()
        .map((item) => Map<String, dynamic>.from(item))
        .toList();
  }

  /// Update one permission mode (`ask` or `always`).
  Future<Map<String, dynamic>> setPermission(String key, String mode) async {
    final response = await _client.put(
      _uri('/api/v1/permissions/${Uri.encodeComponent(key)}'),
      headers: _headers(json: true),
      body: jsonEncode({'mode': mode}),
    );
    _markUnauthorized(response);
    final decoded = _decode(response);
    if (response.statusCode < 200 || response.statusCode >= 300) {
      _throwResponse(response, decoded, '权限更新失败');
    }
    if (decoded is! Map) {
      throw const AssistantApiException('权限接口返回了无效响应');
    }
    return Map<String, dynamic>.from(decoded);
  }

  /// Return the user's durable sandbox state. A 404 means the endpoint is
  /// unavailable on an older server and is represented as `null`.
  Future<Map<String, dynamic>?> sandboxStatus() async {
    final response = await _client.get(
      _uri('/api/v1/sandbox'),
      headers: _headers(),
    );
    _markUnauthorized(response);
    if (response.statusCode == 404) return null;
    final decoded = _decode(response);
    if (response.statusCode != 200) {
      _throwResponse(response, decoded, '沙箱状态加载失败');
    }
    if (decoded is! Map) {
      throw const AssistantApiException('沙箱状态接口返回了无效响应');
    }
    return Map<String, dynamic>.from(decoded);
  }

  /// Reset the user's sandbox workspace. A 404 is treated as success for
  /// compatibility with servers that predate the sandbox endpoint.
  Future<void> resetSandbox() async {
    final response = await _client.post(
      _uri('/api/v1/sandbox/reset'),
      headers: _headers(),
    );
    _markUnauthorized(response);
    if (response.statusCode == 404) return;
    final decoded = _decode(response);
    if (response.statusCode < 200 || response.statusCode >= 300) {
      _throwResponse(response, decoded, '沙箱工作区重置失败');
    }
  }

  /// Build the authenticated file-content endpoint used by the file result
  /// card. The file id is encoded as a path segment; the caller supplies the
  /// bearer token through the existing authenticated client/view.
  Uri fileContentUri(String fileId) =>
      _uri('/api/v1/files/${Uri.encodeComponent(fileId)}/content');

  Future<Uint8List> downloadFileBytes(String fileId) async {
    final response = await _client
        .get(fileContentUri(fileId), headers: _headers())
        .timeout(const Duration(seconds: 20));
    _markUnauthorized(response);
    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw AssistantApiException('文件下载失败', statusCode: response.statusCode);
    }
    return response.bodyBytes;
  }

  /// Open the authenticated notification SSE stream. The server uses the
  /// standard Last-Event-ID header to resume from the last notification after
  /// a foreground reconnect, so callers only need to retain the latest event
  /// id and pass it back here.
  Future<Stream<AssistantSseEvent>> notificationStream({
    String? lastEventId,
  }) async {
    final request = http.Request('GET', _uri('/api/v1/notifications/stream'))
      ..headers.addAll(_headers())
      ..headers['accept'] = 'text/event-stream';
    if (lastEventId != null && lastEventId.trim().isNotEmpty) {
      request.headers['last-event-id'] = lastEventId.trim();
    }
    final response = await _client.send(request);
    if (response.statusCode != 200) {
      if (response.statusCode == 401) onUnauthorized?.call();
      final body = await response.stream.bytesToString();
      dynamic decoded;
      try {
        decoded = body.trim().isEmpty ? null : jsonDecode(body);
      } catch (_) {
        decoded = null;
      }
      final materialized = http.Response(body, response.statusCode);
      _throwResponse(materialized, decoded, '通知流连接失败');
    }
    return _parseSse(
      response.stream.transform(utf8.decoder).transform(const LineSplitter()),
    );
  }

  /// Register an APNs or FCM token. Native token acquisition is intentionally
  /// left to platform-specific integrations; this method only writes the
  /// already acquired token to the authenticated API.
  Future<Map<String, dynamic>> registerPushDevice(
    String platform,
    String token,
  ) async {
    final response = await _client.post(
      _uri('/api/v1/push/devices'),
      headers: _headers(json: true),
      body: jsonEncode({'platform': platform, 'token': token}),
    );
    _markUnauthorized(response);
    final decoded = _decode(response);
    if (response.statusCode != 200 && response.statusCode != 201) {
      _throwResponse(response, decoded, '推送设备注册失败');
    }
    if (decoded is! Map) {
      throw const AssistantApiException('推送设备接口返回了无效响应');
    }
    return Map<String, dynamic>.from(decoded);
  }

  Future<List<Map<String, dynamic>>> listAuthSessions() async {
    final response = await _client.get(
      _uri('/api/v1/auth/sessions'),
      headers: _headers(),
    );
    _markUnauthorized(response);
    final decoded = _decode(response);
    if (response.statusCode != 200) {
      _throwResponse(response, decoded, '登录设备列表加载失败');
    }
    if (decoded is! List) {
      throw const AssistantApiException('登录设备接口返回了无效响应');
    }
    return decoded
        .whereType<Map>()
        .map((item) => Map<String, dynamic>.from(item))
        .toList();
  }

  Future<bool> revokeAuthSession(String sessionId) async {
    final response = await _client.delete(
      _uri('/api/v1/auth/sessions/${Uri.encodeComponent(sessionId)}'),
      headers: _headers(),
    );
    _markUnauthorized(response);
    final decoded = _decode(response);
    if (response.statusCode != 200) {
      _throwResponse(response, decoded, '登录设备退出失败');
    }
    return decoded is Map && decoded['revoked'] == true;
  }

  /// Revoke every server-side session. The local bearer token is deliberately
  /// retained here; the caller decides whether to leave the current client.
  Future<void> logoutAll() async {
    final response = await _client.post(
      _uri('/api/v1/auth/logout-all'),
      headers: _headers(),
    );
    _markUnauthorized(response);
    final decoded = _decode(response);
    if (response.statusCode != 200 && response.statusCode != 204) {
      _throwResponse(response, decoded, '退出所有设备失败');
    }
  }

  Future<List<Map<String, dynamic>>> listConnectors() async {
    final response = await _client.get(
      _uri('/api/v1/connectors'),
      headers: _headers(),
    );
    _markUnauthorized(response);
    final decoded = _decode(response);
    if (response.statusCode != 200) {
      _throwResponse(response, decoded, '连接器列表加载失败');
    }
    final rows = decoded is List
        ? decoded
        : decoded is Map && decoded['connectors'] is List
        ? decoded['connectors'] as List
        : const <dynamic>[];
    return rows
        .whereType<Map>()
        .map((item) => Map<String, dynamic>.from(item))
        .toList();
  }

  Future<Map<String, dynamic>> addMcpConnector({
    String? config,
    String? name,
    String? url,
    Map<String, String>? headers,
  }) async {
    final body = <String, dynamic>{};
    if (config != null) body['config'] = config;
    if (name != null && name.trim().isNotEmpty) body['name'] = name.trim();
    if (url != null && url.trim().isNotEmpty) body['url'] = url.trim();
    if (headers != null) body['headers'] = headers;
    final response = await _client.post(
      _uri('/api/v1/connectors/mcp'),
      headers: _headers(json: true),
      body: jsonEncode(body),
    );
    _markUnauthorized(response);
    final decoded = _decode(response);
    if (response.statusCode != 200 && response.statusCode != 201) {
      _throwResponse(response, decoded, '添加连接器失败');
    }
    if (decoded is! Map) throw const AssistantApiException('连接器接口返回了无效响应');
    return Map<String, dynamic>.from(decoded);
  }

  Future<Map<String, dynamic>> updateMcpConnector(
    String id, {
    String? name,
    String? url,
    Map<String, String>? headers,
    Map<String, bool>? tools,
  }) async {
    final body = <String, dynamic>{};
    if (name != null) body['name'] = name;
    if (url != null) body['url'] = url;
    if (headers != null) body['headers'] = headers;
    if (tools != null) body['tools'] = tools;
    return _connectorMutation(
      'PATCH',
      '/api/v1/connectors/${Uri.encodeComponent(id)}/mcp',
      body,
      '更新连接器失败',
    );
  }

  Future<Map<String, dynamic>> syncConnector(String id) async {
    return _connectorMutation(
      'POST',
      '/api/v1/connectors/${Uri.encodeComponent(id)}/sync',
      const <String, dynamic>{},
      '连接器同步失败',
    );
  }

  Future<Map<String, dynamic>> setConnectorEnabled(
    String id,
    bool enabled,
  ) async {
    return _connectorMutation(
      'PATCH',
      '/api/v1/connectors/${Uri.encodeComponent(id)}',
      {'enabled': enabled},
      '连接器状态更新失败',
    );
  }

  Future<void> deleteConnector(String id) async {
    final response = await _client.delete(
      _uri('/api/v1/connectors/${Uri.encodeComponent(id)}'),
      headers: _headers(),
    );
    _markUnauthorized(response);
    final decoded = _decode(response);
    if (response.statusCode < 200 || response.statusCode >= 300) {
      _throwResponse(response, decoded, '删除连接器失败');
    }
  }

  dynamic _decode(http.Response response) {
    if (response.body.trim().isEmpty) return null;
    try {
      return jsonDecode(response.body);
    } catch (_) {
      return null;
    }
  }

  Never _throwResponse(
    http.Response response,
    dynamic decoded,
    String fallback,
  ) {
    if (response.statusCode == 503) {
      throw AssistantApiException('服务暂时不可用', statusCode: 503);
    }
    final detail = decoded is Map ? decoded['detail'] : null;
    if (detail is String && detail.isNotEmpty) {
      throw AssistantApiException(detail, statusCode: response.statusCode);
    }
    throw AssistantApiException(
      '$fallback（HTTP ${response.statusCode}）',
      statusCode: response.statusCode,
    );
  }

  Future<Map<String, dynamic>> _connectorMutation(
    String method,
    String path,
    Map<String, dynamic> body,
    String fallback,
  ) async {
    final request = http.Request(method, _uri(path))
      ..headers.addAll(_headers(json: true))
      ..body = jsonEncode(body);
    final response = await _client.send(request);
    final materialized = await http.Response.fromStream(response);
    _markUnauthorized(materialized);
    final decoded = _decode(materialized);
    if (materialized.statusCode < 200 || materialized.statusCode >= 300) {
      _throwResponse(materialized, decoded, fallback);
    }
    if (decoded is! Map) throw const AssistantApiException('连接器接口返回了无效响应');
    return Map<String, dynamic>.from(decoded);
  }

  Uri _uri(String path) => Uri.parse(
    '${base.endsWith('/') ? base.substring(0, base.length - 1) : base}$path',
  );

  static const _clientVersion = String.fromEnvironment(
    'APP_VERSION',
    defaultValue: '0.1.0',
  );

  static String get _clientPlatform {
    if (kIsWeb) return 'web';
    return switch (defaultTargetPlatform) {
      TargetPlatform.iOS => 'ios',
      TargetPlatform.android => 'android',
      TargetPlatform.macOS => 'macos',
      TargetPlatform.windows => 'windows',
      TargetPlatform.linux => 'linux',
      // Flutter also supports Fuchsia, but the Luma client identifiers do not.
      TargetPlatform.fuchsia => 'linux',
    };
  }

  static String get _platformDescription {
    final client = _clientPlatform;
    if (kIsWeb) return client;
    // Header values must be ASCII; localized OS strings would break requests.
    final version = platform_version.operatingSystemVersion
        .replaceAll(RegExp(r'[^\x20-\x7E]'), '')
        .trim();
    final description = version.isEmpty ? client : '$client $version';
    return description.length > 120
        ? description.substring(0, 120)
        : description;
  }

  Map<String, String> _headers({
    bool json = false,
    bool authenticated = true,
  }) => {
    if (json) 'content-type': 'application/json',
    'accept': 'application/json',
    'X-Luma-Client': _clientPlatform,
    'X-Luma-Platform': _platformDescription,
    'X-Luma-Client-Version': _clientVersion,
    if (authenticated && token != null && token!.isNotEmpty)
      'authorization': 'Bearer $token',
  };

  void _markUnauthorized(http.Response response) {
    if (response.statusCode == 401) onUnauthorized?.call();
  }

  Future<Stream<AssistantSseEvent>> streamMessage(
    String sessionId,
    String content, {
    Map<String, dynamic>? metadata,
  }) async {
    final request =
        http.Request(
            'POST',
            _uri('/api/v1/sessions/$sessionId/messages/stream'),
          )
          ..headers.addAll(_headers(json: true))
          ..headers['accept'] = 'text/event-stream'
          ..body = jsonEncode({'content': content, 'metadata': ?metadata});
    final response = await _client.send(request);
    if (response.statusCode != 200) {
      if (response.statusCode == 401) onUnauthorized?.call();
      final body = await response.stream.bytesToString();
      if (response.statusCode == 503) {
        throw const AssistantApiException('服务暂时不可用', statusCode: 503);
      }
      final decoded = _decode(http.Response(body, response.statusCode));
      if (response.statusCode == 429 &&
          decoded is Map &&
          decoded['detail'] is Map &&
          decoded['detail']['code'] == 'too_many_generations') {
        throw const AssistantApiException('同时进行的回复太多，请稍后', statusCode: 429);
      }
      throw AssistantApiException(
        '流式接口返回 HTTP ${response.statusCode}${body.isEmpty ? '' : ': $body'}',
        statusCode: response.statusCode,
      );
    }
    final lines = response.stream
        .transform(utf8.decoder)
        .transform(const LineSplitter());
    return _parseSse(lines);
  }

  /// Resume a generation stream after the last SSE event received by the
  /// client. Both cursor forms are sent so this works through SSE proxies.
  Future<Stream<AssistantSseEvent>> streamMessageAfter(
    String messageId, {
    String? after,
  }) async {
    final query = <String, String>{};
    if (after != null && after.isNotEmpty) query['after'] = after;
    final request =
        http.Request(
            'GET',
            _uri('/api/v1/messages/${Uri.encodeComponent(messageId)}/stream')
                .replace(queryParameters: query),
          )
          ..headers.addAll(_headers())
          ..headers['accept'] = 'text/event-stream';
    if (after != null && after.isNotEmpty) {
      request.headers['Last-Event-ID'] = after;
    }
    final response = await _client.send(request);
    if (response.statusCode != 200) {
      if (response.statusCode == 401) onUnauthorized?.call();
      await response.stream.bytesToString();
      if (response.statusCode == 503) {
        throw const AssistantApiException('服务暂时不可用', statusCode: 503);
      }
      throw AssistantApiException(
        '续读接口返回 HTTP ${response.statusCode}',
        statusCode: response.statusCode,
      );
    }
    final lines = response.stream
        .transform(utf8.decoder)
        .transform(const LineSplitter());
    return _parseSse(lines);
  }

  Future<void> cancelMessage(String messageId) async {
    final response = await _client.post(
      _uri('/api/v1/messages/${Uri.encodeComponent(messageId)}/cancel'),
      headers: _headers(json: true),
    );
    _markUnauthorized(response);
    final decoded = _decode(response);
    if (response.statusCode != 200 && response.statusCode != 202) {
      _throwResponse(response, decoded, '停止生成失败');
    }
  }

  final http.Client _client;

  Stream<AssistantSseEvent> _parseSse(Stream<String> lines) async* {
    final parser = _SseParser();
    await for (final line in lines) {
      for (final event in parser.addLine(line)) {
        yield event;
      }
    }
    for (final event in parser.finish()) {
      yield event;
    }
  }

  void close() => _client.close();

  Future<Map<String, dynamic>> widgetEvent(
    String widgetId,
    String action,
    Object? value,
  ) async {
    final response = await _client.post(
      _uri('/api/v1/widgets/${Uri.encodeComponent(widgetId)}/events'),
      headers: _headers(json: true),
      body: jsonEncode({'action': action, 'value': value}),
    );
    _markUnauthorized(response);
    dynamic decoded;
    try {
      decoded = jsonDecode(response.body);
    } catch (_) {
      if (response.statusCode == 503) {
        throw const AssistantApiException('服务暂时不可用', statusCode: 503);
      }
      throw AssistantApiException('组件接口返回了无效响应（HTTP ${response.statusCode}）');
    }
    if (response.statusCode != 200) {
      _throwResponse(response, decoded, '组件操作失败');
    }
    if (decoded is! Map || decoded['widget'] is! Map) {
      throw const AssistantApiException('组件接口缺少更新后的组件');
    }
    return Map<String, dynamic>.from(decoded);
  }

  Future<Map<String, dynamic>?> decideApproval(
    String id,
    bool approve, {
    bool remember = false,
  }) async {
    try {
      final response = await _client.post(
        _uri('/api/v1/runtime/approvals/${Uri.encodeComponent(id)}/decision'),
        headers: _headers(json: true),
        body: jsonEncode({
          'decision': approve ? 'approve' : 'reject',
          'remember': remember,
        }),
      );
      _markUnauthorized(response);
      if (response.statusCode == 200) {
        return jsonDecode(response.body) as Map<String, dynamic>;
      }
      if (response.statusCode == 503) {
        _throwResponse(response, _decode(response), '服务暂时不可用');
      }
    } on AssistantApiException {
      rethrow;
    } catch (_) {}
    return null;
  }

  /// Return the durable, user-owned routines configured on the server.
  Future<List<Map<String, dynamic>>> listRoutines({
    bool enabledOnly = false,
  }) async {
    final suffix = enabledOnly ? '?enabled_only=true' : '';
    final response = await _client.get(
      _uri('/api/v1/routines$suffix'),
      headers: _headers(),
    );
    _markUnauthorized(response);
    final decoded = _decode(response);
    if (response.statusCode != 200) {
      _throwResponse(response, decoded, '例程列表加载失败');
    }
    final rows = decoded is List
        ? decoded
        : decoded is Map && decoded['routines'] is List
        ? decoded['routines'] as List
        : const <dynamic>[];
    return rows
        .whereType<Map>()
        .map((item) => Map<String, dynamic>.from(item))
        .toList();
  }

  Future<Map<String, dynamic>> createRoutine({
    required String title,
    required String prompt,
    required String schedule,
    String timezone = 'Asia/Shanghai',
    bool enabled = true,
  }) async {
    return _routineMutation('POST', '/api/v1/routines', {
      'title': title,
      'prompt': prompt,
      'schedule': schedule,
      'timezone': timezone,
      'enabled': enabled,
    }, '创建例程失败');
  }

  Future<Map<String, dynamic>> updateRoutine(
    String id, {
    String? title,
    String? prompt,
    String? schedule,
    String? timezone,
    bool? enabled,
  }) async {
    final body = <String, dynamic>{};
    if (title != null) body['title'] = title;
    if (prompt != null) body['prompt'] = prompt;
    if (schedule != null) body['schedule'] = schedule;
    if (timezone != null) body['timezone'] = timezone;
    if (enabled != null) body['enabled'] = enabled;
    return _routineMutation(
      'PATCH',
      '/api/v1/routines/${Uri.encodeComponent(id)}',
      body,
      '更新例程失败',
    );
  }

  Future<void> deleteRoutine(String id) async {
    final response = await _client.delete(
      _uri('/api/v1/routines/${Uri.encodeComponent(id)}'),
      headers: _headers(),
    );
    _markUnauthorized(response);
    final decoded = _decode(response);
    if (response.statusCode < 200 || response.statusCode >= 300) {
      _throwResponse(response, decoded, '删除例程失败');
    }
  }

  Future<Map<String, dynamic>> runRoutine(String id) async {
    return _routineMutation(
      'POST',
      '/api/v1/routines/${Uri.encodeComponent(id)}/run',
      const <String, dynamic>{},
      '立即运行例程失败',
    );
  }

  Future<Map<String, dynamic>> updateMemory(
    String id, {
    String? content,
    String? category,
    int? importance,
    bool? pinned,
    Map<String, dynamic>? metadata,
  }) async {
    final body = <String, dynamic>{};
    if (content != null) body['content'] = content;
    if (category != null) body['category'] = category;
    if (importance != null) body['importance'] = importance;
    if (pinned != null) body['pinned'] = pinned;
    if (metadata != null) body['metadata'] = metadata;
    final response = await _client.patch(
      _uri('/api/v1/memories/${Uri.encodeComponent(id)}'),
      headers: _headers(json: true),
      body: jsonEncode(body),
    );
    _markUnauthorized(response);
    final decoded = _decode(response);
    if (response.statusCode < 200 || response.statusCode >= 300) {
      _throwResponse(response, decoded, '更新记忆失败');
    }
    if (decoded is! Map) throw const AssistantApiException('记忆接口返回了无效响应');
    return Map<String, dynamic>.from(decoded);
  }

  Future<Map<String, dynamic>> confirmMemory(
    String id, {
    String? category,
  }) async {
    final response = await _client.post(
      _uri('/api/v1/memories/${Uri.encodeComponent(id)}/confirm'),
      headers: _headers(json: true),
      body: jsonEncode(
        category == null ? const <String, dynamic>{} : {'category': category},
      ),
    );
    _markUnauthorized(response);
    final decoded = _decode(response);
    if (response.statusCode < 200 || response.statusCode >= 300) {
      _throwResponse(response, decoded, '确认记忆失败');
    }
    if (decoded is! Map) throw const AssistantApiException('记忆接口返回了无效响应');
    return Map<String, dynamic>.from(decoded);
  }

  Future<Map<String, dynamic>> _routineMutation(
    String method,
    String path,
    Map<String, dynamic> body,
    String fallback,
  ) async {
    final request = http.Request(method, _uri(path))
      ..headers.addAll(_headers(json: true))
      ..body = jsonEncode(body);
    final response = await _client.send(request);
    final materialized = await http.Response.fromStream(response);
    _markUnauthorized(materialized);
    final decoded = _decode(materialized);
    if (materialized.statusCode < 200 || materialized.statusCode >= 300) {
      _throwResponse(materialized, decoded, fallback);
    }
    if (decoded is! Map) throw const AssistantApiException('例程接口返回了无效响应');
    return Map<String, dynamic>.from(decoded);
  }
}

class AssistantApiException implements Exception {
  const AssistantApiException(this.message, {this.statusCode});

  final String message;
  final int? statusCode;

  String get detail => message;

  bool get isUnavailable => statusCode == 503;

  @override
  String toString() => message;
}

class AssistantSseEvent {
  const AssistantSseEvent(this.name, this.data, [this.id]);

  final String name;
  final Map<String, dynamic> data;
  final String? id;
}

class MessagePage {
  const MessagePage({
    required this.messages,
    required this.hasMore,
    this.oldestId,
  });

  final List<Map<String, dynamic>> messages;
  final bool hasMore;
  final String? oldestId;
}

class SearchResults {
  const SearchResults({
    required this.query,
    required this.sessions,
    required this.messages,
  });

  final String query;
  final List<Map<String, dynamic>> sessions;
  final List<Map<String, dynamic>> messages;
}

/// Small SSE parser that accepts fragmented UTF-8 input, multiline data, and
/// a final frame without a trailing blank line.
class _SseParser {
  String? _event;
  String? _id;
  final List<String> _data = [];

  Iterable<AssistantSseEvent> addLine(String rawLine) {
    final line = rawLine.endsWith('\r')
        ? rawLine.substring(0, rawLine.length - 1)
        : rawLine;
    if (line.isEmpty) return _dispatch();
    if (line.startsWith(':')) return const <AssistantSseEvent>[];

    final separator = line.indexOf(':');
    final field = separator < 0 ? line : line.substring(0, separator);
    var value = separator < 0 ? '' : line.substring(separator + 1);
    if (value.startsWith(' ')) value = value.substring(1);
    if (field == 'event') {
      _event = value;
    } else if (field == 'id') {
      _id = value;
    } else if (field == 'data') {
      _data.add(value);
    }
    return const <AssistantSseEvent>[];
  }

  Iterable<AssistantSseEvent> finish() => _dispatch();

  Iterable<AssistantSseEvent> _dispatch() {
    if (_data.isEmpty && _event == null) return const <AssistantSseEvent>[];
    final name = _event ?? 'message';
    final raw = _data.join('\n');
    dynamic decoded;
    try {
      decoded = jsonDecode(raw);
    } catch (_) {
      decoded = <String, dynamic>{'message': raw};
    }
    final data = decoded is Map
        ? Map<String, dynamic>.from(decoded)
        : <String, dynamic>{'value': decoded};
    final id = _id;
    _event = null;
    _id = null;
    _data.clear();
    return [AssistantSseEvent(name, data, id)];
  }
}
