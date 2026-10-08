import 'package:flutter/material.dart';

import 'brand.dart';
import 'preferences.dart';

/// Muse-style design tokens shared with the web client (`--mc-*` in
/// apps/web/src/styles.css). Read them with `context.muse` so every view
/// follows the light and dark themes.
@immutable
class MuseColors extends ThemeExtension<MuseColors> {
  const MuseColors({
    required this.bg,
    required this.rail,
    required this.line,
    required this.text,
    required this.muted,
    required this.faint,
    required this.bubble,
    required this.chip,
    required this.chipHover,
    required this.tabActive,
    required this.composer,
    required this.composerLine,
    required this.focus,
    required this.link,
    required this.green,
    required this.accent,
    required this.danger,
    required this.warn,
  });

  final Color bg;
  final Color rail;
  final Color line;
  final Color text;
  final Color muted;
  final Color faint;
  final Color bubble;
  final Color chip;
  final Color chipHover;
  final Color tabActive;
  final Color composer;
  final Color composerLine;
  final Color focus;
  final Color link;
  final Color green;

  /// User bubbles, primary buttons, send button, progress.
  final Color accent;
  final Color danger;
  final Color warn;

  static const light = MuseColors(
    bg: Color(0xFFFFFFFF),
    rail: Color(0xFFFAFAFA),
    line: Color(0xFFECECEE),
    text: Color(0xFF1F2328),
    muted: Color(0xFF868C95),
    faint: Color(0xFFB2B7BE),
    bubble: Color(0xFFF2F3F5),
    chip: Color(0xFFF2F3F5),
    chipHover: Color(0xFFE8EAED),
    tabActive: Color(0xFFFFFFFF),
    composer: Color(0xFFF4F5F7),
    composerLine: Color(0xFFECEEF1),
    focus: Color(0xFFCFD4DA),
    link: Color(0xFF1F6FD6),
    green: Color(0xFF22A559),
    accent: BrandConfig.defaultPrimary,
    danger: Color(0xFFE5484D),
    warn: Color(0xFFF59E0B),
  );

  static const dark = MuseColors(
    bg: Color(0xFF161616),
    rail: Color(0xFF1B1B1B),
    line: Color(0xFF2A2A2A),
    text: Color(0xFFECECEC),
    muted: Color(0xFF8D8D8D),
    faint: Color(0xFF5F5F5F),
    bubble: Color(0xFF262626),
    chip: Color(0xFF262626),
    chipHover: Color(0xFF303030),
    tabActive: Color(0xFF3A3A3A),
    composer: Color(0xFF262626),
    composerLine: Color(0xFF262626),
    focus: Color(0xFF3A3A3A),
    link: Color(0xFF6AA8FF),
    green: Color(0xFF2FBF5B),
    accent: BrandConfig.defaultPrimary,
    danger: Color(0xFFE5484D),
    warn: Color(0xFFF59E0B),
  );

  @override
  MuseColors copyWith({Color? accent}) => MuseColors(
    bg: bg,
    rail: rail,
    line: line,
    text: text,
    muted: muted,
    faint: faint,
    bubble: bubble,
    chip: chip,
    chipHover: chipHover,
    tabActive: tabActive,
    composer: composer,
    composerLine: composerLine,
    focus: focus,
    link: link,
    green: green,
    accent: accent ?? this.accent,
    danger: danger,
    warn: warn,
  );

  @override
  MuseColors lerp(ThemeExtension<MuseColors>? other, double t) {
    if (other is! MuseColors) return this;
    Color mix(Color a, Color b) => Color.lerp(a, b, t)!;
    return MuseColors(
      bg: mix(bg, other.bg),
      rail: mix(rail, other.rail),
      line: mix(line, other.line),
      text: mix(text, other.text),
      muted: mix(muted, other.muted),
      faint: mix(faint, other.faint),
      bubble: mix(bubble, other.bubble),
      chip: mix(chip, other.chip),
      chipHover: mix(chipHover, other.chipHover),
      tabActive: mix(tabActive, other.tabActive),
      composer: mix(composer, other.composer),
      composerLine: mix(composerLine, other.composerLine),
      focus: mix(focus, other.focus),
      link: mix(link, other.link),
      green: mix(green, other.green),
      accent: mix(accent, other.accent),
      danger: mix(danger, other.danger),
      warn: mix(warn, other.warn),
    );
  }
}

/// Muse proportions shared by every view.
class MuseMetrics {
  static const railWidth = 72.0;
  static const railWidthCompact = 56.0;
  static const railButton = 44.0;
  static const chatColumn = 720.0;
  static const readingColumn = 760.0;
  static const composerWidth = 768.0;
  static const composerHeight = 52.0;
  static const contextPanel = 360.0;
  static const bubbleRadius = 16.0;
  static const userBubbleRadius = 18.0;
  static const cardRadius = 16.0;
  static const pillHeight = 34.0;

  /// Below this width the rail becomes a bottom navigation bar.
  static const mobileBreakpoint = 640.0;
}

extension MuseThemeContext on BuildContext {
  MuseColors get muse =>
      Theme.of(this).extension<MuseColors>() ?? MuseColors.light;
}

// Compatibility constants used by code that predates the token extension.
class LumaColors {
  static const lime = BrandConfig.defaultPrimary;
  static const canvas = Color(0xFFFFFFFF);
  static const card = Color(0xFFF2F3F5);
  static const muted = Color(0xFF868C95);
}

const lime = LumaColors.lime;
const canvas = LumaColors.canvas;
const card = LumaColors.card;
const muted = LumaColors.muted;

ThemeData buildLumaTheme(Brightness brightness, {Color? accent}) {
  // A stored swatch wins. Otherwise follow the current brand primary.
  final resolved =
      accent ??
      LumaPreferences.instance.value.accent ??
      BrandController.instance.value.primary;
  final colors =
      (brightness == Brightness.dark ? MuseColors.dark : MuseColors.light)
          .copyWith(accent: resolved);
  final base = ThemeData(useMaterial3: true, brightness: brightness);
  return base.copyWith(
    scaffoldBackgroundColor: colors.bg,
    canvasColor: colors.bg,
    dividerColor: colors.line,
    colorScheme:
        ColorScheme.fromSeed(
          seedColor: colors.accent,
          brightness: brightness,
        ).copyWith(
          primary: colors.accent,
          onPrimary: Colors.white,
          surface: colors.bg,
          onSurface: colors.text,
          outline: colors.line,
          error: colors.danger,
        ),
    textTheme: base.textTheme.apply(
      bodyColor: colors.text,
      displayColor: colors.text,
    ),
    dividerTheme: DividerThemeData(color: colors.line, thickness: 1, space: 1),
    splashFactory: NoSplash.splashFactory,
    extensions: [colors],
  );
}

ThemeData get lumaTheme => buildLumaTheme(Brightness.light);
ThemeData get lumaDarkTheme => buildLumaTheme(Brightness.dark);
