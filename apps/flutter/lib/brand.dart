import 'package:flutter/material.dart';

import 'api.dart';

/// Build-time brand defaults. `--dart-define=BRAND_PRODUCT_NAME` (default
/// `Luma`), `BRAND_TAGLINE` (default empty) and `BRAND_PRIMARY_COLOR`
/// (default `#2563EB`). A successful `GET /api/v1/brand` overrides them.
class BrandConfig {
  const BrandConfig({
    required this.name,
    required this.tagline,
    required this.primary,
    this.logoUrl,
  });

  static const defaultName = 'Luma';
  static const defaultPrimary = Color(0xFF2563EB);

  static const _nameEnv = String.fromEnvironment(
    'BRAND_PRODUCT_NAME',
    defaultValue: defaultName,
  );
  static const _taglineEnv = String.fromEnvironment('BRAND_TAGLINE');
  static const _colorEnv = String.fromEnvironment(
    'BRAND_PRIMARY_COLOR',
    defaultValue: '#2563EB',
  );

  static final BrandConfig buildTime = BrandConfig(
    name: _nameEnv.trim().isEmpty ? defaultName : _nameEnv.trim(),
    tagline: _taglineEnv.trim(),
    primary: parseBrandColor(_colorEnv) ?? defaultPrimary,
  );

  final String name;
  final String tagline;
  final Color primary;
  final String? logoUrl;

  /// The handwritten mark is the Luma wordmark. Any other product name uses
  /// a bold text logo in the primary color.
  bool get showsScriptLogo => name == defaultName;

  /// About row. An empty tagline keeps the historical 「个人助理」 suffix.
  String get aboutTitle {
    final line = tagline.trim();
    if (line.isEmpty) return '$name 个人助理';
    return '$name $line';
  }

  /// Server fields replace build-time defaults. Blank or invalid fields stay.
  BrandConfig merge(Map<String, dynamic> json) {
    final name = _stringField(json, const [
      'name',
      'product_name',
      'productName',
    ]);
    final tagline = _stringField(json, const ['tagline']);
    final colorText = _stringField(json, const [
      'primary_color',
      'primaryColor',
    ]);
    final logo = _stringField(json, const ['logo_url', 'logoUrl']);
    return BrandConfig(
      name: name ?? this.name,
      tagline: tagline ?? this.tagline,
      primary: colorText == null
          ? primary
          : (parseBrandColor(colorText) ?? primary),
      logoUrl: logo ?? logoUrl,
    );
  }
}

String? _stringField(Map<String, dynamic> json, List<String> keys) {
  for (final key in keys) {
    final value = json[key];
    if (value is String && value.trim().isNotEmpty) return value.trim();
  }
  return null;
}

/// `#RGB`, `#RRGGBB` and `#AARRGGBB`. A missing `#` is accepted. Invalid
/// input returns null so callers keep the previous color.
Color? parseBrandColor(String raw) {
  var text = raw.trim();
  if (text.startsWith('#')) text = text.substring(1);
  if (text.length == 3) {
    final expanded = StringBuffer();
    for (final unit in text.split('')) {
      expanded
        ..write(unit)
        ..write(unit);
    }
    text = expanded.toString();
  }
  if (text.length == 6) text = 'FF$text';
  if (text.length != 8 || !RegExp(r'^[0-9a-fA-F]+$').hasMatch(text)) {
    return null;
  }
  final value = int.tryParse(text, radix: 16);
  if (value == null) return null;
  return Color(value);
}

typedef BrandLoader = Future<Map<String, dynamic>> Function();

class BrandController extends ChangeNotifier {
  BrandController({BrandConfig? initial, this.load})
    : _value = initial ?? BrandConfig.buildTime;

  static final BrandController instance = BrandController(
    load: _fetchRemoteBrand,
  );

  BrandConfig _value;
  final BrandLoader? load;
  var _disposed = false;

  BrandConfig get value => _value;

  /// Applies a successful payload onto the build-time defaults. A failed
  /// request drops any previous override and restores those defaults.
  Future<void> refresh() async {
    final fetch = load;
    if (fetch == null) return;
    try {
      _value = BrandConfig.buildTime.merge(await fetch());
    } catch (_) {
      _value = BrandConfig.buildTime;
    }
    if (!_disposed) notifyListeners();
  }

  @override
  void dispose() {
    _disposed = true;
    super.dispose();
  }
}

Future<Map<String, dynamic>> _fetchRemoteBrand() async {
  if (!AssistantApi.hasConfiguredBase) {
    throw const AssistantApiException('未配置服务器地址');
  }
  final api = AssistantApi();
  try {
    return await api.brand();
  } finally {
    api.close();
  }
}

class BrandScope extends InheritedNotifier<BrandController> {
  const BrandScope({
    super.key,
    required BrandController controller,
    required super.child,
  }) : super(notifier: controller);

  static BrandConfig of(BuildContext context) {
    final scope = context.dependOnInheritedWidgetOfExactType<BrandScope>();
    return scope?.notifier?.value ?? BrandConfig.buildTime;
  }
}

extension BrandContext on BuildContext {
  BrandConfig get brand => BrandScope.of(this);
}

/// The supplied wordmark, cropped around its strokes instead of the square artboard.
class LumaLogo extends StatelessWidget {
  const LumaLogo({super.key, this.width = 100, this.color});

  static const blue = Color(0xFF2563EB);
  static const aspectRatio = 1020 / 480;

  final double width;
  final Color? color;

  @override
  Widget build(BuildContext context) {
    final brand = BrandScope.of(context);
    if (!brand.showsScriptLogo) {
      final ink = color ?? brand.primary;
      return Semantics(
        label: brand.name,
        image: true,
        excludeSemantics: true,
        child: SizedBox(
          width: width,
          height: width / aspectRatio,
          child: FittedBox(
            fit: BoxFit.contain,
            alignment: Alignment.center,
            child: Text(
              brand.name,
              maxLines: 1,
              style: TextStyle(
                color: ink,
                fontWeight: FontWeight.w700,
                fontSize: 64,
                height: 1,
              ),
            ),
          ),
        ),
      );
    }
    final ink =
        color ??
        (Theme.of(context).brightness == Brightness.dark
            ? Colors.white
            : brand.primary);
    return Semantics(
      label: brand.name,
      image: true,
      child: SizedBox(
        width: width,
        height: width / aspectRatio,
        child: CustomPaint(painter: _LumaLogoPainter(ink)),
      ),
    );
  }
}

class _LumaLogoPainter extends CustomPainter {
  const _LumaLogoPainter(this.color);

  final Color color;

  // Exact path from the supplied SVG, with the original round caps and stroke.
  static final _path = Path()
    ..moveTo(1049.5, 648)
    ..cubicTo(1030.5, 676, 987.5, 731, 955, 724)
    ..cubicTo(897.909, 711.704, 980.5, 600.5, 896, 579)
    ..cubicTo(860.193, 569.89, 831.748, 590.946, 807.811, 619)
    ..cubicTo(775.262, 657.15, 751.05, 708.238, 728, 714)
    ..cubicTo(688, 724, 705, 581.5, 669, 579)
    ..cubicTo(633, 576.5, 595.5, 704, 585.5, 704)
    ..cubicTo(575.5, 704, 597, 593, 551, 593)
    ..cubicTo(505, 593, 489.5, 719.5, 455, 724)
    ..cubicTo(427.4, 727.6, 423, 655, 430.5, 600.5)
    ..cubicTo(430.5, 587, 377.5, 745, 343, 740)
    ..cubicTo(308.5, 735, 343, 593, 324, 593)
    ..cubicTo(312.5, 593, 262.5, 760.75, 205, 740)
    ..cubicTo(90, 698.5, 273.5, 233, 360, 342)
    ..cubicTo(405.5, 411.5, 186.333, 599.667, 93, 648)
    ..moveTo(896, 579)
    ..cubicTo(977, 609, 891.5, 740, 827, 740)
    ..cubicTo(784.079, 740, 751.261, 700.6, 807.811, 619);

  @override
  void paint(Canvas canvas, Size size) {
    final scale = (size.width / 1020).clamp(0.0, size.height / 480).toDouble();
    canvas.save();
    canvas.translate(
      (size.width - 1020 * scale) / 2,
      (size.height - 480 * scale) / 2,
    );
    canvas.scale(scale);
    canvas.translate(-60, -290);
    canvas.drawPath(
      _path,
      Paint()
        ..color = color
        ..style = PaintingStyle.stroke
        ..strokeWidth = 51
        ..strokeCap = StrokeCap.round,
    );
    canvas.restore();
  }

  @override
  bool shouldRepaint(_LumaLogoPainter oldDelegate) =>
      color != oldDelegate.color;
}
