import 'package:flutter/material.dart';

import 'auth.dart';
import 'home.dart';
import 'preferences.dart';
import 'theme.dart';

// Preserve the original package entrypoint's public surface for callers that
// imported these types from `main.dart` before the refactor.
export 'api.dart';
export 'auth.dart';
export 'home.dart';
export 'preferences.dart';
export 'theme.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await LumaPreferences.instance.load();
  runApp(const LumaApp());
}

// Kept as a compatibility name for the generated Flutter smoke test.
class MyApp extends LumaApp {
  const MyApp({super.key}) : super(requireAuth: false);
}

class LumaApp extends StatelessWidget {
  const LumaApp({super.key, this.requireAuth = true, this.preferences});

  final bool requireAuth;
  final LumaPreferences? preferences;

  @override
  Widget build(BuildContext context) {
    final controller = preferences ?? LumaPreferences.instance;
    return LumaPreferencesScope(
      preferences: controller,
      child: ValueListenableBuilder<AppearancePreferences>(
        valueListenable: controller,
        builder: (context, appearance, child) => MaterialApp(
          debugShowCheckedModeBanner: false,
          title: 'Luma',
          theme: buildLumaTheme(Brightness.light, accent: appearance.accent),
          darkTheme: buildLumaTheme(Brightness.dark, accent: appearance.accent),
          themeMode: appearance.themeMode,
          home: child,
        ),
        child: requireAuth ? const AuthGate() : const LumaHome(),
      ),
    );
  }
}
