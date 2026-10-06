import 'package:flutter/material.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

@immutable
class LumaAccent {
  const LumaAccent(this.label, this.color, this.value);

  final String label;
  final Color color;
  final String value;
}

// These values match the corresponding presets in the web settings.
const lumaAccents = [
  LumaAccent('蓝', Color(0xFF1F7AEC), '#1f7aec'),
  LumaAccent('紫', Color(0xFF8B5CF6), '#8b5cf6'),
  LumaAccent('绿', Color(0xFF22A559), '#22a559'),
  LumaAccent('橙', Color(0xFFEA6A2A), '#ea6a2a'),
  LumaAccent('玫红', Color(0xFFE5487F), '#e5487f'),
  LumaAccent('石墨', Color(0xFF3F4650), '#3f4650'),
];

enum MessageFontSize { small, standard, large }

@immutable
class AppearancePreferences {
  const AppearancePreferences({
    this.themeMode = ThemeMode.system,
    this.accent = const Color(0xFF1F7AEC),
    this.fontSize = MessageFontSize.standard,
  });

  final ThemeMode themeMode;
  final Color accent;
  final MessageFontSize fontSize;

  double get messageFontSize {
    switch (fontSize) {
      case MessageFontSize.small:
        return 14;
      case MessageFontSize.standard:
        return 16;
      case MessageFontSize.large:
        return 18;
    }
  }

  AppearancePreferences copyWith({
    ThemeMode? themeMode,
    Color? accent,
    MessageFontSize? fontSize,
  }) => AppearancePreferences(
    themeMode: themeMode ?? this.themeMode,
    accent: accent ?? this.accent,
    fontSize: fontSize ?? this.fontSize,
  );
}

class LumaPreferences extends ValueNotifier<AppearancePreferences> {
  LumaPreferences({FlutterSecureStorage? storage})
    : _storage = storage ?? FlutterSecureStorage(),
      super(const AppearancePreferences());

  static final instance = LumaPreferences();
  static const _prefix = 'luma.pref.';

  final FlutterSecureStorage _storage;
  Future<void> _writes = Future<void>.value();

  Future<void> load() async {
    try {
      final theme = await _storage.read(key: '${_prefix}theme');
      final accent = await _storage.read(key: '${_prefix}accent');
      final fontSize = await _storage.read(key: '${_prefix}fontSize');
      value = AppearancePreferences(
        themeMode: ThemeMode.values.firstWhere(
          (item) => item.name == theme,
          orElse: () => ThemeMode.system,
        ),
        accent: lumaAccents
            .firstWhere(
              (item) => item.value == accent,
              orElse: () => lumaAccents.first,
            )
            .color,
        fontSize: MessageFontSize.values.firstWhere(
          (item) => item.name == fontSize,
          orElse: () => MessageFontSize.standard,
        ),
      );
    } catch (_) {
      // A missing platform store can still use the default appearance.
    }
  }

  Future<void> setThemeMode(ThemeMode mode) {
    value = value.copyWith(themeMode: mode);
    return _save('theme', mode.name);
  }

  Future<void> setAccent(Color accent) {
    final preset = lumaAccents.firstWhere((item) => item.color == accent);
    value = value.copyWith(accent: preset.color);
    return _save('accent', preset.value);
  }

  Future<void> setFontSize(MessageFontSize size) {
    value = value.copyWith(fontSize: size);
    return _save('fontSize', size.name);
  }

  Future<void> _save(String key, String setting) {
    // Keep quick successive changes in order, including after a failed write.
    _writes = _writes
        .catchError((Object _) {})
        .then((_) => _storage.write(key: '$_prefix$key', value: setting));
    return _writes;
  }
}

class LumaPreferencesScope extends InheritedNotifier<LumaPreferences> {
  const LumaPreferencesScope({
    super.key,
    required LumaPreferences preferences,
    required super.child,
  }) : super(notifier: preferences);

  static LumaPreferences controllerOf(BuildContext context) =>
      context
          .dependOnInheritedWidgetOfExactType<LumaPreferencesScope>()
          ?.notifier ??
      LumaPreferences.instance;

  static AppearancePreferences of(BuildContext context) =>
      controllerOf(context).value;
}

extension LumaPreferencesContext on BuildContext {
  double get messageFontSize => LumaPreferencesScope.of(this).messageFontSize;
}
