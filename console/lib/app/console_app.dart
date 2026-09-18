import 'package:flutter/material.dart';

import '../features/status/presentation/overview_screen.dart';
import 'theme.dart';

class ConsoleApp extends StatelessWidget {
  const ConsoleApp({super.key});

  @override
  Widget build(BuildContext context) => MaterialApp(
    title: 'Yggdrasil',
    debugShowCheckedModeBanner: false,
    theme: buildLightTheme(),
    darkTheme: buildDarkTheme(),
    themeMode: ThemeMode.system,
    home: const OverviewScreen(),
  );
}
