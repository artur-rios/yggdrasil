import 'package:flutter/material.dart';

/// The world tree's green. Both schemes derive from it, and the Android icon
/// and web manifest use the same colour.
const Color yggdrasilSeedColor = Color(0xFF2E6B3F);

ThemeData _themeFor(Brightness brightness) {
  final scheme = ColorScheme.fromSeed(
    seedColor: yggdrasilSeedColor,
    brightness: brightness,
  );

  return ThemeData(
    useMaterial3: true,
    colorScheme: scheme,
    visualDensity: VisualDensity.standard,
    inputDecorationTheme: const InputDecorationTheme(
      border: OutlineInputBorder(),
    ),
    cardTheme: CardThemeData(
      elevation: 0,
      margin: EdgeInsets.zero,
      clipBehavior: Clip.antiAlias,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(12),
        side: BorderSide(color: scheme.outlineVariant),
      ),
    ),
  );
}

ThemeData buildLightTheme() => _themeFor(Brightness.light);

ThemeData buildDarkTheme() => _themeFor(Brightness.dark);
