import 'dart:convert';

import '../../features/environments/domain/environment.dart';

/// Values supplied at build time with `--dart-define`.
///
/// Everything here is compiled into the bundle and readable by anyone who
/// loads it, so it never carries a token.
class AppConfig {
  const AppConfig({
    this.environments = const <Environment>[],
    this.refreshInterval = const Duration(seconds: 30),
  });

  /// Reads `YGGDRASIL_ENVIRONMENTS`, a JSON list of hosts, `{name, url}` (the
  /// name predates the UI calling the saved connections hosts):
  ///
  ///     --dart-define=YGGDRASIL_ENVIRONMENTS=[{"name":"vps","url":"https://yggdrasil.example.com"}]
  factory AppConfig.fromEnvironment() => AppConfig(
    environments: parseEnvironments(
      const String.fromEnvironment('YGGDRASIL_ENVIRONMENTS'),
    ),
  );

  /// The hosts offered when the user has not saved a list of their own.
  final List<Environment> environments;

  /// How often the overview polls while it is visible; the contract's default.
  final Duration refreshInterval;

  /// Parses the `YGGDRASIL_ENVIRONMENTS` value. Entries without a valid name
  /// and URL are skipped, and an unreadable value yields no environments
  /// rather than an app that cannot start.
  static List<Environment> parseEnvironments(String raw) {
    if (raw.trim().isEmpty) {
      return const <Environment>[];
    }

    final Object? decoded;

    try {
      decoded = jsonDecode(raw);
    } on FormatException {
      return const <Environment>[];
    }

    if (decoded is! List<dynamic>) {
      return const <Environment>[];
    }

    final environments = <Environment>[];

    for (final entry in decoded) {
      if (entry is! Map<String, dynamic>) {
        continue;
      }

      final name = entry['name'];
      final url = entry['url'];

      if (name is! String ||
          name.trim().isEmpty ||
          url is! String ||
          validateBaseUrl(url) != null) {
        continue;
      }

      final baseUrl = normalizeBaseUrl(url);

      if (environments.any((environment) => environment.baseUrl == baseUrl)) {
        continue;
      }

      // Derived from the URL, so a compiled-in environment keeps its token
      // across launches without the list ever being saved.
      environments.add(
        Environment(id: 'url:$baseUrl', name: name.trim(), url: baseUrl),
      );
    }

    return environments;
  }
}
