import 'package:flutter/material.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:luma_client/main.dart';
import 'package:luma_client/views/settings.dart';

Widget _settingsApp(
  LumaPreferences preferences,
  AssistantApi api, {
  VoidCallback? onLogout,
}) => LumaPreferencesScope(
  preferences: preferences,
  child: ValueListenableBuilder<AppearancePreferences>(
    valueListenable: preferences,
    builder: (context, appearance, _) => MaterialApp(
      theme: buildLumaTheme(Brightness.light, accent: appearance.accent),
      darkTheme: buildLumaTheme(Brightness.dark, accent: appearance.accent),
      themeMode: appearance.themeMode,
      home: Builder(
        builder: (context) => Scaffold(
          body: Column(
            children: [
              Text(
                '消息预览',
                key: const Key('message-preview'),
                style: TextStyle(fontSize: context.messageFontSize),
              ),
              TextButton(
                onPressed: () => Navigator.of(context).push<void>(
                  MaterialPageRoute(
                    builder: (_) => SettingsView(api: api, onLogout: onLogout),
                  ),
                ),
                child: const Text('打开设置'),
              ),
            ],
          ),
        ),
      ),
    ),
  ),
);

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUp(() => FlutterSecureStorage.setMockInitialValues({}));

  test(
    'appearance loads stored values and persists only preference keys',
    () async {
      FlutterSecureStorage.setMockInitialValues({
        'luma.pref.theme': 'dark',
        'luma.pref.accent': '#8b5cf6',
        'luma.pref.fontSize': 'large',
        'unrelated.setting': 'keep',
      });
      final preferences = LumaPreferences();
      addTearDown(preferences.dispose);
      await preferences.load();

      expect(preferences.value.themeMode, ThemeMode.dark);
      expect(preferences.value.accent, const Color(0xFF8B5CF6));
      expect(preferences.value.messageFontSize, 18);

      await preferences.setThemeMode(ThemeMode.light);
      await preferences.setAccent(lumaAccents[2].color);
      await preferences.setFontSize(MessageFontSize.small);
      final restored = LumaPreferences();
      addTearDown(restored.dispose);
      await restored.load();
      expect(restored.value.themeMode, ThemeMode.light);
      expect(restored.value.accent, const Color(0xFF22A559));
      expect(restored.value.messageFontSize, 14);
      expect(await FlutterSecureStorage().readAll(), {
        'luma.pref.theme': 'light',
        'luma.pref.accent': '#22a559',
        'luma.pref.fontSize': 'small',
        'unrelated.setting': 'keep',
      });
    },
  );

  test('invalid stored settings use safe default appearance', () async {
    FlutterSecureStorage.setMockInitialValues({
      'luma.pref.theme': 'unsupported',
      'luma.pref.accent': 'invalid',
      'luma.pref.fontSize': 'extra-large',
    });
    final preferences = LumaPreferences();
    addTearDown(preferences.dispose);
    await preferences.load();
    expect(preferences.value.themeMode, ThemeMode.system);
    expect(preferences.value.accent, lumaAccents.first.color);
    expect(preferences.value.messageFontSize, 16);
  });

  test('quick preference changes persist in their original order', () async {
    final preferences = LumaPreferences();
    addTearDown(preferences.dispose);
    final first = preferences.setFontSize(MessageFontSize.large);
    final second = preferences.setFontSize(MessageFontSize.small);
    expect(preferences.value.messageFontSize, 14);
    await Future.wait([first, second]);
    expect(
      await FlutterSecureStorage().read(key: 'luma.pref.fontSize'),
      'small',
    );
  });

  testWidgets('LumaApp applies theme and accent without restart', (
    tester,
  ) async {
    final preferences = LumaPreferences();
    addTearDown(preferences.dispose);
    await tester.pumpWidget(LumaApp(preferences: preferences));
    await tester.pumpAndSettle();
    expect(
      tester.widget<MaterialApp>(find.byType(MaterialApp)).themeMode,
      ThemeMode.system,
    );

    await preferences.setThemeMode(ThemeMode.dark);
    await preferences.setAccent(lumaAccents[1].color);
    await tester.pumpAndSettle();
    final app = tester.widget<MaterialApp>(find.byType(MaterialApp));
    expect(app.themeMode, ThemeMode.dark);
    expect(app.theme!.extension<MuseColors>()!.accent, lumaAccents[1].color);
    expect(app.darkTheme!.colorScheme.primary, lumaAccents[1].color);
  });

  testWidgets('appearance controls update theme, accent and message size', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(390, 844);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.reset);
    final preferences = LumaPreferences();
    addTearDown(preferences.dispose);
    final api = AssistantApi();
    addTearDown(api.close);
    await tester.pumpWidget(_settingsApp(preferences, api));
    await tester.tap(find.text('打开设置'));
    await tester.pumpAndSettle();

    await tester.tap(find.text('深色'));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('accent-#e5487f')));
    await tester.pumpAndSettle();
    await tester.tap(find.text('大'));
    await tester.pumpAndSettle();
    expect(preferences.value.themeMode, ThemeMode.dark);
    expect(preferences.value.accent, const Color(0xFFE5487F));
    expect(preferences.value.messageFontSize, 18);
    expect(tester.takeException(), isNull);

    await tester.pageBack();
    await tester.pumpAndSettle();
    final preview = tester.widget<Text>(
      find.byKey(const Key('message-preview')),
    );
    expect(preview.style!.fontSize, 18);
  });

  testWidgets('settings groups are ordered and logout requires confirmation', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(390, 1800);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.reset);
    final preferences = LumaPreferences();
    addTearDown(preferences.dispose);
    final api = AssistantApi();
    addTearDown(api.close);
    var logoutCount = 0;
    await tester.pumpWidget(
      _settingsApp(preferences, api, onLogout: () => logoutCount++),
    );
    await tester.tap(find.text('打开设置'));
    await tester.pumpAndSettle();

    var previousY = 0.0;
    final settingsScroll = find
        .descendant(
          of: find.byKey(const Key('settings-list')),
          matching: find.byType(Scrollable),
        )
        .first;
    for (final heading in [
      '外观',
      '账号',
      '权限',
      '沙箱工作区',
      '主动消息',
      '记忆',
      '用量',
      '关于',
    ]) {
      await tester.scrollUntilVisible(
        find.text(heading),
        240,
        scrollable: settingsScroll,
      );
      final scroll = tester.state<ScrollableState>(settingsScroll);
      final y =
          tester.getTopLeft(find.text(heading)).dy + scroll.position.pixels;
      expect(y, greaterThan(previousY));
      previousY = y;
    }
    await tester.ensureVisible(find.byKey(const Key('settings-logout')));
    await tester.tap(find.text('退出登录'));
    await tester.pumpAndSettle();
    expect(logoutCount, 0);
    expect(find.text('退出登录？'), findsOneWidget);
    await tester.tap(find.text('取消'));
    await tester.pumpAndSettle();
    expect(logoutCount, 0);

    await tester.tap(find.text('退出登录'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('退出'));
    await tester.pumpAndSettle();
    expect(logoutCount, 1);
    expect(find.text('打开设置'), findsOneWidget);
  });
}
