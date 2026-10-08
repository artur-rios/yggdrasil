import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_riverpod/misc.dart' show Override;
import 'package:flutter_test/flutter_test.dart';
import 'package:yggdrasil_console/app/console_app.dart';
import 'package:yggdrasil_console/core/config/app_config.dart';
import 'package:yggdrasil_console/features/environments/data/environment_store.dart';
import 'package:yggdrasil_console/features/environments/domain/environment.dart';
import 'package:yggdrasil_console/features/environments/presentation/environments_controller.dart';
import 'package:yggdrasil_console/features/status/data/status_repository.dart';
import 'package:yggdrasil_console/features/status/data/status_source.dart';
import 'package:yggdrasil_console/features/status/domain/status.dart';
import 'package:yggdrasil_console/features/status/presentation/status_controller.dart';

/// A v2 response like the example of `docs/status-api.md`: example.com with
/// development and homologation (on demand, stopped) and production (up), the
/// heimdall and fortuna systems and part of the yggdrasil platform.
String contractExampleJson() =>
    File('test/fixtures/contract_example.json').readAsStringSync();

/// A v1 response (one environment per host), from a status API older than
/// the v2 contract, with every status and kind.
String v1ExampleJson() =>
    File('test/fixtures/v1_example.json').readAsStringSync();

/// The bundled demo: the default setup, three environments with every
/// platform component.
String demoJson() => File('assets/demo/status.json').readAsStringSync();

HostStatus demoStatus() => parseStatusBody(demoJson());

HostStatus v1Status() => parseStatusBody(v1ExampleJson());

const Environment production = Environment(
  id: 'production',
  name: 'production',
  url: 'https://yggdrasil.example.com',
);

const Environment homologation = Environment(
  id: 'homologation',
  name: 'homologation',
  url: 'https://yggdrasil.hml.example.com',
);

/// A status source whose next answer the test decides.
class FakeStatusSource implements StatusSource {
  FakeStatusSource([this.respond]);

  Future<HostStatus> Function(Environment environment, String? token)? respond;

  final List<(Environment, String?)> calls = <(Environment, String?)>[];

  @override
  Future<HostStatus> fetch(Environment environment, String? token) {
    calls.add((environment, token));

    return respond?.call(environment, token) ?? Future.value(demoStatus());
  }
}

/// Pumps the whole app with in-memory stores and [source].
Future<void> pumpConsole(
  WidgetTester tester, {
  required StatusSource source,
  List<Environment>? environments = const <Environment>[
    production,
    homologation,
  ],
  Map<String, String>? tokens,
  Size size = const Size(1000, 900),
  String? webOrigin,
}) async {
  tester.view.physicalSize = size;
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.reset);

  await tester.pumpWidget(
    ProviderScope(
      // As in main.dart.
      retry: (_, _) => null,
      overrides: <Override>[
        appConfigProvider.overrideWithValue(const AppConfig()),
        environmentStoreProvider.overrideWithValue(
          InMemoryEnvironmentStore(environments: environments),
        ),
        tokenStoreProvider.overrideWithValue(InMemoryTokenStore(tokens)),
        webOriginProvider.overrideWithValue(webOrigin),
        statusRepositoryProvider.overrideWithValue(StatusRepository(source)),
      ],
      child: const ConsoleApp(),
    ),
  );
  await settle(tester);
}

/// Lets futures resolve and short animations finish. `pumpAndSettle` cannot
/// be used: the relative times tick every second and progress indicators
/// animate forever.
Future<void> settle(WidgetTester tester) async {
  for (var i = 0; i < 12; i++) {
    await tester.pump(const Duration(milliseconds: 50));
  }
}
