import 'dart:async';

import 'package:flutter/material.dart';

import 'auth.dart';
import 'brand.dart';
import 'browser_tools.dart';
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
  unawaited(BrandController.instance.refresh());
  unawaited(BrowserLiveHosts.instance.refresh());
}

// Kept as a compatibility name for the generated Flutter smoke test.
class MyApp extends LumaApp {
  const MyApp({super.key}) : super(requireAuth: false);
}

class LumaApp extends StatelessWidget {
  const LumaApp({
    super.key,
    this.requireAuth = true,
    this.preferences,
    this.brand,
  });

  final bool requireAuth;
  final LumaPreferences? preferences;
  final BrandController? brand;

  @override
  Widget build(BuildContext context) {
    final controller = preferences ?? LumaPreferences.instance;
    final brandController = brand ?? BrandController.instance;
    return BrandScope(
      controller: brandController,
      child: ListenableBuilder(
        listenable: brandController,
        builder: (context, child) {
          return LumaPreferencesScope(
            preferences: controller,
            child: ValueListenableBuilder<AppearancePreferences>(
              valueListenable: controller,
              builder: (context, appearance, child) => MaterialApp(
                debugShowCheckedModeBanner: false,
                title: context.brand.name,
                theme: buildLumaTheme(
                  Brightness.light,
                  accent: appearance.accent ?? context.brand.primary,
                ),
                darkTheme: buildLumaTheme(
                  Brightness.dark,
                  accent: appearance.accent ?? context.brand.primary,
                ),
                themeMode: appearance.themeMode,
                home: child,
              ),
              child: child,
            ),
          );
        },
        child: requireAuth ? const AuthGate() : const LumaHome(),
      ),
    );
  }
}
