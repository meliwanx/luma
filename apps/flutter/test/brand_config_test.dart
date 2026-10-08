import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:luma_client/api.dart';
import 'package:luma_client/auth.dart';
import 'package:luma_client/brand.dart';
import 'package:luma_client/home.dart';
import 'package:luma_client/preferences.dart';
import 'package:luma_client/theme.dart';
import 'package:luma_client/views/chat.dart';
import 'package:luma_client/views/feed.dart';
import 'package:luma_client/views/ideas.dart';
import 'package:luma_client/views/library.dart';
import 'package:luma_client/views/memory.dart';
import 'package:luma_client/views/proactive_settings.dart';
import 'package:luma_client/views/search.dart';
import 'package:luma_client/views/settings.dart';
import 'package:luma_client/views/side_menu.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test('build-time defaults stay when fields are blank or invalid', () {
    final merged = BrandConfig.buildTime.merge({
      'name': '  ',
      'tagline': '',
      'primary_color': 'not-a-color',
      'logo_url': '   ',
    });
    expect(merged.name, 'Luma');
    expect(merged.tagline, BrandConfig.defaultTagline);
    expect(merged.primary, BrandConfig.defaultPrimary);
    expect(merged.logoUrl, isNull);
    expect(merged.aboutTitle, 'Luma ${BrandConfig.defaultTagline}');
    expect(merged.showsScriptLogo, isTrue);
    expect(parseBrandColor('#abc'), const Color(0xFFAABBCC));
    expect(parseBrandColor('112233'), const Color(0xFF112233));
  });

  test('remote brand merges over build-time defaults', () {
    final merged = BrandConfig.buildTime.merge({
      'name': ' 测试品牌 ',
      'tagline': '副标题',
      'primaryColor': '#112233',
      'logo_url': ' https://example.com/logo.png ',
    });
    expect(merged.name, '测试品牌');
    expect(merged.tagline, '副标题');
    expect(merged.primary, const Color(0xFF112233));
    expect(merged.logoUrl, 'https://example.com/logo.png');
    expect(merged.aboutTitle, '测试品牌 副标题');
    expect(merged.showsScriptLogo, isFalse);
    final partial = BrandConfig.buildTime.merge({'tagline': '仅标语'});
    expect(partial.name, 'Luma');
    expect(partial.tagline, '仅标语');
    expect(partial.primary, BrandConfig.defaultPrimary);
  });

  test('failed brand request keeps the last merged brand', () async {
    final api = AssistantApi(
      token: 'stale-token',
      client: MockClient((request) async {
        expect(request.method, 'GET');
        expect(request.url.path, '/api/v1/brand');
        expect(request.headers, isNot(contains('authorization')));
        return http.Response('nope', 503);
      }),
    );
    addTearDown(api.close);
    final controller = BrandController(
      initial: const BrandConfig(
        name: '旧品牌',
        tagline: '旧',
        primary: Color(0xFF010101),
      ),
      load: api.brand,
    );
    addTearDown(controller.dispose);
    await controller.refresh();
    expect(controller.value.name, '旧品牌');
    expect(controller.value.tagline, '旧');
    expect(controller.value.primary, const Color(0xFF010101));
  });

  test(
    'successful brand payload overrides and a later failure keeps it',
    () async {
      var fail = false;
      final api = AssistantApi(
        client: MockClient((request) async {
          if (fail) return http.Response('[]', 200);
          return http.Response(
            jsonEncode({'name': '测试品牌', 'primary_color': '#336699'}),
            200,
            headers: {'content-type': 'application/json'},
          );
        }),
      );
      addTearDown(api.close);
      final controller = BrandController(load: api.brand);
      addTearDown(controller.dispose);
      await controller.refresh();
      expect(controller.value.name, '测试品牌');
      expect(controller.value.primary, const Color(0xFF336699));
      expect(controller.value.tagline, BrandConfig.buildTime.tagline);
      fail = true;
      await controller.refresh();
      expect(controller.value.name, '测试品牌');
      expect(controller.value.primary, const Color(0xFF336699));
    },
  );

  testWidgets('non-Luma names use a bold primary-color text logo', (
    tester,
  ) async {
    final semantics = tester.ensureSemantics();
    final brand = _testBrand();
    addTearDown(brand.dispose);
    try {
      await tester.pumpWidget(
        BrandScope(
          controller: brand,
          child: const MaterialApp(home: Scaffold(body: LumaLogo(width: 120))),
        ),
      );
      final text = tester.widget<Text>(find.text('测试品牌'));
      expect(text.style?.fontWeight, FontWeight.w700);
      expect(text.style?.color, const Color(0xFF112233));
      expect(find.bySemanticsLabel('测试品牌'), findsOneWidget);
      expect(find.bySemanticsLabel('Luma'), findsNothing);
      expect(
        find.descendant(
          of: find.byType(LumaLogo),
          matching: find.byType(CustomPaint),
        ),
        findsNothing,
      );
      expect(tester.getSize(find.byType(LumaLogo)).width, 120);
    } finally {
      semantics.dispose();
    }
  });

  testWidgets('测试品牌 does not show Luma on the primary screens', (tester) async {
    final semantics = tester.ensureSemantics();
    final brand = _testBrand();
    addTearDown(brand.dispose);
    final homeApi = AssistantApi(
      client: MockClient((_) async => http.Response('{}', 404)),
    );

    Widget host(Widget child) => BrandScope(
      controller: brand,
      child: MaterialApp(
        theme: buildLumaTheme(Brightness.light),
        home: Scaffold(body: child),
      ),
    );

    void expectNoLuma() {
      expect(find.textContaining('Luma'), findsNothing);
      expect(find.bySemanticsLabel(RegExp('Luma')), findsNothing);
    }

    try {
      await tester.pumpWidget(
        host(
          LoginPage(
            busy: false,
            onLogin: (_, _) async {},
            onRegister: (_, _, _, _, _) async {},
          ),
        ),
      );
      expect(find.text('登录 测试品牌'), findsOneWidget);
      expect(find.text('测试品牌'), findsWidgets);
      expectNoLuma();

      await tester.pumpWidget(host(LumaHome(api: homeApi)));
      await tester.pump();
      expect(find.text('早上好，我是 测试品牌'), findsOneWidget);
      expectNoLuma();
      await tester.tap(find.byTooltip('资源库'));
      await tester.pump();
      expect(find.text('你的文件和 测试品牌 产出，都在这里。'), findsOneWidget);
      expectNoLuma();

      final api = AssistantApi(
        client: MockClient((_) async => http.Response('{}', 404)),
      );
      addTearDown(api.close);
      await tester.pumpWidget(host(SettingsView(api: api)));
      await tester.pump();
      await tester.scrollUntilVisible(find.text('查看和确认 测试品牌 记住的内容'), 400);
      expect(find.text('查看和确认 测试品牌 记住的内容'), findsOneWidget);
      await tester.scrollUntilVisible(find.text('测试品牌 副标题'), 400);
      expect(find.text('测试品牌 副标题'), findsOneWidget);
      expectNoLuma();

      tester.view.physicalSize = const Size(390, 844);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.reset);
      await tester.pumpWidget(
        host(
          SideMenu(
            tab: 0,
            sessions: const [],
            sessionId: 'main',
            onTabChanged: (_) {},
            onMainChat: () {},
            onSelectSession: (_) {},
            onDeleteSession: (_) async => false,
            onRenameSession: (_) async => false,
            onSettings: () {},
            onSearch: () {},
            onNewSession: () {},
          ),
        ),
      );
      expect(find.byKey(const ValueKey('drawer-logo')), findsOneWidget);
      expect(find.text('测试品牌'), findsOneWidget);
      expectNoLuma();

      await tester.pumpWidget(
        host(
          LibraryView(api: api, onFile: (_) async {}, onSelectSession: (_) {}),
        ),
      );
      await tester.pumpAndSettle();
      expect(find.text('还没有资源，上传文件或让 测试品牌 帮你创作吧。'), findsOneWidget);
      expectNoLuma();

      await tester.pumpWidget(
        host(
          IdeasView(
            api: api,
            onStart: (_, _) async {},
            onSelectSession: (_) {},
          ),
        ),
      );
      await tester.pumpAndSettle();
      expect(find.text('和 测试品牌 聊聊你想做的事，灵感会在这里出现。'), findsOneWidget);
      expectNoLuma();

      await tester.pumpWidget(
        host(FeedView(api: api, onDiscuss: (_) async {})),
      );
      await tester.pumpAndSettle();
      expect(find.text('测试品牌 会围绕你关心的话题整理资讯。'), findsOneWidget);
      expectNoLuma();

      await tester.pumpWidget(host(MemoryView(memories: const [])));
      await tester.pump();
      expect(find.text('你可以随时查看、编辑或删除 测试品牌 记住的内容。'), findsOneWidget);
      expect(find.text('当 测试品牌 记住新的内容时，它们会显示在这里。'), findsOneWidget);
      expectNoLuma();

      await tester.pumpWidget(host(ProactiveSettingsView(api: api)));
      await tester.pumpAndSettle();
      expect(find.text('让 测试品牌 在合适的时间主动联系你'), findsOneWidget);
      expectNoLuma();

      await tester.pumpWidget(
        host(
          SearchView(
            api: api,
            sessions: const [
              {'id': 's1'},
            ],
            onSelectSession: (_) {},
          ),
        ),
      );
      expect(find.text('测试品牌'), findsOneWidget);
      expectNoLuma();

      final input = TextEditingController();
      final scroll = ScrollController();
      addTearDown(input.dispose);
      addTearDown(scroll.dispose);
      await tester.pumpWidget(
        host(
          ChatView(
            messages: const [],
            tasks: const [],
            memories: const [],
            sending: false,
            input: input,
            scrollController: scroll,
            onRefresh: () async {},
            onSend: () {},
            onWidgetEvent: (_, _, _) async {},
          ),
        ),
      );
      expect(find.text('早上好，我是 测试品牌'), findsOneWidget);
      expectNoLuma();

      await tester.pumpWidget(
        host(
          ChatView(
            messages: const [
              {'role': 'user', 'content': '你好'},
            ],
            tasks: const [],
            memories: const [],
            sending: true,
            input: input,
            scrollController: scroll,
            onRefresh: () async {},
            onSend: () {},
            onWidgetEvent: (_, _, _) async {},
          ),
        ),
      );
      await tester.pump(const Duration(milliseconds: 50));
      expect(find.bySemanticsLabel('测试品牌 正在思考'), findsOneWidget);
      expectNoLuma();

      await tester.pumpWidget(
        host(
          ChatView(
            messages: const [
              {
                'role': 'assistant',
                'content': '跟进',
                'metadata': {'proactive': {}},
              },
            ],
            tasks: const [],
            memories: const [],
            sending: false,
            input: input,
            scrollController: scroll,
            onRefresh: () async {},
            onSend: () {},
            onWidgetEvent: (_, _, _) async {},
          ),
        ),
      );
      await tester.pump();
      await tester.tap(find.text('主动'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 400));
      expect(find.text('测试品牌 想主动帮你跟进关心的事。'), findsOneWidget);
      expectNoLuma();
      await tester.pumpWidget(const SizedBox.shrink());
    } finally {
      semantics.dispose();
    }
  });

  testWidgets('聚光测试 shows the title, remote logo and brand accent', (
    tester,
  ) async {
    final brand = BrandController(
      initial: const BrandConfig(
        name: '聚光测试',
        tagline: '你的个人 AI 助理',
        primary: Color(0xFF1E66F5),
        logoUrl: 'https://example.com/juguang-logo.png',
      ),
    );
    addTearDown(brand.dispose);
    final preferences = LumaPreferences();
    addTearDown(preferences.dispose);
    await tester.pumpWidget(
      BrandScope(
        controller: brand,
        child: LumaPreferencesScope(
          preferences: preferences,
          child: Builder(
            builder: (context) {
              final accent = preferences.value.accent ?? context.brand.primary;
              return MaterialApp(
                title: context.brand.name,
                theme: buildLumaTheme(Brightness.light, accent: accent),
                home: Scaffold(
                  appBar: AppBar(title: Text(context.brand.aboutTitle)),
                  body: const LumaLogo(width: 120),
                ),
              );
            },
          ),
        ),
      ),
    );
    await tester.pump();
    final app = tester.widget<MaterialApp>(find.byType(MaterialApp));
    expect(app.title, '聚光测试');
    expect(app.theme!.colorScheme.primary, const Color(0xFF1E66F5));
    expect(find.text('聚光测试 你的个人 AI 助理'), findsOneWidget);
    final image = tester.widget<Image>(find.byType(Image));
    expect(image.image, isA<NetworkImage>());
    expect(
      (image.image as NetworkImage).url,
      'https://example.com/juguang-logo.png',
    );
    final fallback = image.errorBuilder!(
      tester.element(find.byType(Image)),
      Exception('offline'),
      StackTrace.empty,
    );
    await tester.pumpWidget(
      BrandScope(
        controller: brand,
        child: MaterialApp(home: Scaffold(body: fallback)),
      ),
    );
    expect(find.text('聚光测试'), findsOneWidget);
    expect(
      find.byWidgetPredicate(
        (widget) =>
            widget is CustomPaint &&
            widget.painter.runtimeType.toString().contains('LumaLogo'),
      ),
      findsNothing,
    );
    expect(find.text('Luma'), findsNothing);

    FlutterSecureStorage.setMockInitialValues({});
    await preferences.setAccent(const Color(0xFF8B5CF6));
    await tester.pumpWidget(
      BrandScope(
        controller: brand,
        child: LumaPreferencesScope(
          preferences: preferences,
          child: Builder(
            builder: (context) => MaterialApp(
              theme: buildLumaTheme(
                Brightness.light,
                accent: preferences.value.accent ?? context.brand.primary,
              ),
              home: const SizedBox.shrink(),
            ),
          ),
        ),
      ),
    );
    expect(
      tester
          .widget<MaterialApp>(find.byType(MaterialApp))
          .theme!
          .colorScheme
          .primary,
      const Color(0xFF8B5CF6),
    );
  });
}

BrandController _testBrand() => BrandController(
  initial: const BrandConfig(
    name: '测试品牌',
    tagline: '副标题',
    primary: Color(0xFF112233),
  ),
);
