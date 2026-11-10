import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:yggdrasil_console/features/environments/domain/environment.dart';
import 'package:yggdrasil_console/features/status/data/refresh_settings_store.dart';
import 'package:yggdrasil_console/features/status/data/status_source.dart';
import 'package:yggdrasil_console/features/status/domain/status.dart';
import 'package:yggdrasil_console/features/status/presentation/map/system_map.dart';
import 'package:yggdrasil_console/features/status/presentation/refresh_settings.dart';
import 'package:yggdrasil_console/features/status/presentation/widgets/application_detail.dart';

import '../../../helpers.dart';

Finder tile(String id) => find.byKey(ValueKey<String>('map-system-$id'));

final Finder map = find.byType(SystemMap);

final Finder hostSwitcher = find.byKey(
  const ValueKey<String>('environment-switcher'),
);

/// Picks [name] in the saved-host dropdown.
Future<void> switchHost(WidgetTester tester, String name) async {
  await tester.tap(hostSwitcher);
  await settle(tester);
  // The menu's entry, over the button's own copy.
  await tester.tap(find.text(name).last);
  await settle(tester);
}

const List<String> demoSystems = <String>[
  'heimdall',
  'hermes',
  'fortuna',
  'yggdrasil',
];

/// The demo with fortuna-api degraded in production.
HostStatus demoWithAProblem() {
  final json = jsonDecode(demoJson()) as Map<String, dynamic>;
  final fortuna = (json['systems'] as List<dynamic>)
      .cast<Map<String, dynamic>>()
      .firstWhere((system) => system['id'] == 'fortuna');
  final production = (fortuna['environments'] as List<dynamic>)
      .cast<Map<String, dynamic>>()
      .firstWhere((entry) => entry['environment'] == 'production');
  final api = (production['applications'] as List<dynamic>)
      .cast<Map<String, dynamic>>()
      .firstWhere((application) => application['id'] == 'fortuna-api');

  api['status'] = 'degraded';
  production['status'] = 'degraded';
  fortuna['status'] = 'degraded';
  json['status'] = 'degraded';
  (json['environments'] as List<dynamic>)
          .cast<Map<String, dynamic>>()
          .firstWhere(
            (environment) => environment['id'] == 'production',
          )['status'] =
      'degraded';

  return HostStatus.fromJson(json);
}

void main() {
  testWidgets('the overview shows the host, its environments and its systems', (
    tester,
  ) async {
    final source = FakeStatusSource();
    await pumpConsole(tester, source: source);

    expect(source.calls.single.$1, production);
    expect(find.byKey(const ValueKey<String>('host-name')), findsOneWidget);
    expect(find.text('example.com'), findsOneWidget);
    // The demo's story: Hermes does not answer, so it and Heimdall (which
    // depends on it) are degraded.
    expect(
      find.text('4 systems · 3 environments · 2 degraded'),
      findsOneWidget,
    );
    // The refresh controls say when, not the banner.
    expect(find.textContaining('updated '), findsNothing);
    // The environment selector carries these now, not the banner.
    expect(
      find.byKey(const ValueKey<String>('host-environments')),
      findsNothing,
    );

    for (final id in demoSystems) {
      expect(tile(id), findsOneWidget, reason: id);
    }
  });

  for (final width in <double>[320, 360, 800, 1400]) {
    testWidgets('$width dp: the map fits and nothing overflows', (
      tester,
    ) async {
      await pumpConsole(
        tester,
        source: FakeStatusSource((_, _) async => demoWithAProblem()),
        size: Size(width, 1200),
      );

      expect(map, findsOneWidget);
      expect(tester.getRect(map).right, lessThanOrEqualTo(width));
      expect(tester.takeException(), isNull);
    });
  }

  testWidgets('there is no problems filter any more', (tester) async {
    await pumpConsole(tester, source: FakeStatusSource());

    expect(find.byKey(const ValueKey<String>('problems-only')), findsNothing);
    expect(find.textContaining('Problems only'), findsNothing);
  });

  testWidgets('a problem in the selected environment shows on its system', (
    tester,
  ) async {
    final handle = tester.ensureSemantics();

    await pumpConsole(
      tester,
      source: FakeStatusSource((_, _) async => demoWithAProblem()),
    );

    expect(
      find.text('4 systems · 3 environments · 3 degraded'),
      findsOneWidget,
    );
    expect(find.bySemanticsLabel(RegExp('^Fortuna, degraded')), findsOneWidget);

    handle.dispose();
  });

  testWidgets('a v1 host shows as one environment', (tester) async {
    await pumpConsole(
      tester,
      source: FakeStatusSource((_, _) async => v1Status()),
    );

    expect(find.byKey(const ValueKey<String>('host-name')), findsOneWidget);
    expect(
      find.text('4 systems · 1 environment · 1 down · 2 degraded'),
      findsOneWidget,
    );

    for (final id in <String>['huginn', 'fortuna', 'yggdrasil', 'heimdall']) {
      expect(tile(id), findsOneWidget, reason: id);
    }

    expect(find.text('on demand'), findsNothing);
  });

  testWidgets('switching host loads that host', (tester) async {
    final source = FakeStatusSource();
    await pumpConsole(tester, source: source);

    await switchHost(tester, 'homologation');

    expect(source.calls.last.$1, homologation);
  });

  for (final size in <Size>[const Size(360, 800), const Size(1000, 900)]) {
    testWidgets('at ${size.width} dp hosts switch with a compact dropdown', (
      tester,
    ) async {
      await pumpConsole(
        tester,
        source: FakeStatusSource(),
        environments: const <Environment>[
          production,
          homologation,
          Environment(
            id: 'third',
            name: 'a-host-with-a-long-name',
            url: 'https://third.example.com',
          ),
        ],
        size: size,
      );

      expect(tester.widget(hostSwitcher), isA<DropdownButton<String>>());
      expect(tester.getSize(hostSwitcher).height, lessThan(32));
      expect(tester.takeException(), isNull);
    });
  }

  testWidgets('a 401 asks for the token, then retries with it', (tester) async {
    final source = FakeStatusSource(
      (_, token) async => token == 'right'
          ? demoStatus()
          : throw const StatusException(
              StatusFailureKind.unauthorized,
              statusCode: 401,
            ),
    );
    await pumpConsole(tester, source: source);

    expect(find.text('Token for production'), findsOneWidget);
    expect(map, findsNothing);

    await tester.enterText(
      find.byKey(const ValueKey<String>('token-field')),
      'right',
    );
    await tester.tap(find.text('Save'));
    await settle(tester);

    expect(source.calls.last.$2, 'right');
    expect(map, findsOneWidget);
    expect(find.byKey(const ValueKey<String>('failure-banner')), findsNothing);
  });

  testWidgets('a network error keeps showing the last data with a warning', (
    tester,
  ) async {
    final source = FakeStatusSource();
    await pumpConsole(tester, source: source);
    expect(find.byKey(const ValueKey<String>('failure-banner')), findsNothing);

    source.respond = (_, _) =>
        throw const StatusException(StatusFailureKind.network);
    await tester.tap(find.byKey(const ValueKey<String>('refresh')));
    await settle(tester);

    expect(
      find.byKey(const ValueKey<String>('failure-banner')),
      findsOneWidget,
    );
    expect(find.text('The status API could not be reached.'), findsOneWidget);
    expect(
      find.textContaining('Showing the last data received'),
      findsOneWidget,
    );
    expect(map, findsOneWidget);
    expect(find.byType(ColorFiltered), findsWidgets);
  });

  testWidgets('a 401 says the host needs a valid token', (tester) async {
    final source = FakeStatusSource();
    await pumpConsole(tester, source: source);

    source.respond = (_, _) => throw const StatusException(
      StatusFailureKind.unauthorized,
      statusCode: 401,
    );
    await tester.tap(find.byKey(const ValueKey<String>('refresh')));
    await settle(tester);
    await tester.tap(find.text('Cancel'));
    await settle(tester);

    expect(
      find.text('The status API needs a valid token for this host.'),
      findsOneWidget,
    );
  });

  testWidgets('503 + Retry-After shows "starting" and retries after it', (
    tester,
  ) async {
    final source = FakeStatusSource(
      (_, _) async => throw const StatusException(
        StatusFailureKind.starting,
        statusCode: 503,
        retryAfter: Duration(seconds: 5),
      ),
    );
    await pumpConsole(tester, source: source);

    expect(
      find.byKey(const ValueKey<String>('starting-banner')),
      findsOneWidget,
    );
    expect(find.byKey(const ValueKey<String>('failure-banner')), findsNothing);
    expect(find.text('Token for production'), findsNothing);
    expect(source.calls, hasLength(1));

    source.respond = null;
    await tester.pump(const Duration(seconds: 5));
    await settle(tester);

    expect(source.calls, hasLength(2), reason: 'retried after Retry-After');
    expect(find.byKey(const ValueKey<String>('starting-banner')), findsNothing);
    expect(map, findsOneWidget);
    expect(find.byType(ColorFiltered), findsNothing, reason: 'not stale');
  });

  testWidgets('an error with no data yet shows the error and a retry', (
    tester,
  ) async {
    final source = FakeStatusSource(
      (_, _) async => throw const StatusException(
        StatusFailureKind.server,
        statusCode: 503,
      ),
    );
    await pumpConsole(tester, source: source);

    expect(find.text('The status API answered with HTTP 503.'), findsOneWidget);

    source.respond = null;
    await tester.tap(find.text('Retry'));
    await settle(tester);

    expect(map, findsOneWidget);
  });

  group('refreshing', () {
    RefreshSettingsStore autoRefresh([
      Duration interval = const Duration(minutes: 1),
    ]) => InMemoryRefreshSettingsStore(
      RefreshSettings(enabled: true, interval: interval),
    );

    void hide(WidgetTester tester) {
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.inactive);
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.hidden);
    }

    void show(WidgetTester tester) {
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.inactive);
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
    }

    testWidgets('loads once on start: one fetch and one refreshDetails', (
      tester,
    ) async {
      final source = FakeStatusSource();
      await pumpConsole(tester, source: source);

      expect(source.calls, hasLength(1));
      expect(source.detailsCalls, hasLength(1));
    });

    testWidgets('does not load again as time passes with auto-refresh off', (
      tester,
    ) async {
      final source = FakeStatusSource();
      await pumpConsole(tester, source: source);

      await tester.pump(const Duration(minutes: 10));
      await settle(tester);

      expect(source.calls, hasLength(1));
      expect(source.detailsCalls, hasLength(1));
    });

    testWidgets('Refresh now loads again', (tester) async {
      final source = FakeStatusSource();
      await pumpConsole(tester, source: source);

      await tester.tap(find.byKey(const ValueKey<String>('refresh')));
      await settle(tester);

      expect(source.calls, hasLength(2));
      expect(source.detailsCalls, hasLength(2));
    });

    testWidgets('with auto-refresh on, loads every minute by default', (
      tester,
    ) async {
      final source = FakeStatusSource();
      await pumpConsole(tester, source: source, refreshSettings: autoRefresh());
      expect(source.calls, hasLength(1));

      await tester.pump(const Duration(seconds: 59));
      expect(source.calls, hasLength(1));

      await tester.pump(const Duration(seconds: 1));
      await settle(tester);
      expect(source.calls, hasLength(2));
      expect(source.detailsCalls, hasLength(2));

      await tester.pump(const Duration(minutes: 1));
      await settle(tester);
      expect(source.calls, hasLength(3));
    });

    for (final interval in autoRefreshIntervals) {
      testWidgets('the ${interval.inMinutes} min interval is honoured', (
        tester,
      ) async {
        final source = FakeStatusSource();
        await pumpConsole(
          tester,
          source: source,
          refreshSettings: autoRefresh(interval),
        );

        await tester.pump(interval - const Duration(seconds: 1));
        expect(source.calls, hasLength(1));

        await tester.pump(const Duration(seconds: 1));
        await settle(tester);
        expect(source.calls, hasLength(2));
      });
    }

    testWidgets('turning auto-refresh on starts the timer', (tester) async {
      final source = FakeStatusSource();
      await pumpConsole(tester, source: source);

      await ProviderScope.containerOf(
            tester.element(find.byType(Scaffold).first),
          )
          .read(refreshSettingsProvider.notifier)
          .update(const RefreshSettings(enabled: true));
      await settle(tester);
      await tester.pump(const Duration(minutes: 1));
      await settle(tester);

      expect(source.calls, hasLength(2));
    });

    testWidgets('auto-refresh pauses while hidden and loads on return', (
      tester,
    ) async {
      final source = FakeStatusSource();
      await pumpConsole(tester, source: source, refreshSettings: autoRefresh());

      hide(tester);
      await tester.pump(const Duration(minutes: 5));
      expect(source.calls, hasLength(1));

      show(tester);
      await settle(tester);
      expect(source.calls, hasLength(2), reason: 'loads on return');

      await tester.pump(const Duration(minutes: 1));
      await settle(tester);
      expect(source.calls, hasLength(3));
    });

    testWidgets('with auto-refresh off, returning from hidden does not load', (
      tester,
    ) async {
      final source = FakeStatusSource();
      await pumpConsole(tester, source: source);

      hide(tester);
      await tester.pump(const Duration(minutes: 5));
      show(tester);
      await settle(tester);

      expect(source.calls, hasLength(1));
      expect(source.detailsCalls, hasLength(1));
    });

    testWidgets('closing the hosts screen loads with auto-refresh on', (
      tester,
    ) async {
      final source = FakeStatusSource();
      await pumpConsole(tester, source: source, refreshSettings: autoRefresh());

      await tester.tap(find.byKey(const ValueKey<String>('settings')));
      await settle(tester);
      await tester.pump(const Duration(minutes: 5));
      expect(source.calls, hasLength(1), reason: 'covered, so paused');

      await tester.pageBack();
      await settle(tester);

      expect(source.calls, hasLength(2));
    });

    testWidgets('closing the hosts screen does not load with it off', (
      tester,
    ) async {
      final source = FakeStatusSource();
      await pumpConsole(tester, source: source);

      await tester.tap(find.byKey(const ValueKey<String>('settings')));
      await settle(tester);
      await tester.pageBack();
      await settle(tester);

      expect(source.calls, hasLength(1));
      expect(source.detailsCalls, hasLength(1));
    });

    testWidgets('a failed details refresh keeps the map on screen', (
      tester,
    ) async {
      final source = FakeStatusSource()
        ..respondDetails = (_, _) =>
            throw const StatusException(StatusFailureKind.server);
      await pumpConsole(tester, source: source);

      expect(map, findsOneWidget);
      expect(
        find.byKey(const ValueKey<String>('failure-banner')),
        findsOneWidget,
      );
    });

    testWidgets('the one-shot retry still runs after a starting answer', (
      tester,
    ) async {
      final source = FakeStatusSource(
        (_, _) async => throw const StatusException(
          StatusFailureKind.starting,
          statusCode: 503,
          retryAfter: Duration(seconds: 5),
        ),
      );
      await pumpConsole(tester, source: source);
      expect(source.calls, hasLength(1));
      expect(source.detailsCalls, isEmpty);

      source.respond = null;
      await tester.pump(const Duration(seconds: 5));
      await settle(tester);

      expect(source.calls, hasLength(2));
      expect(source.detailsCalls, hasLength(1));
      expect(map, findsOneWidget);

      await tester.pump(const Duration(minutes: 10));
      expect(source.calls, hasLength(2), reason: 'a one-shot, not a poll');
    });
  });

  testWidgets('the settings screen manages hosts', (tester) async {
    await pumpConsole(tester, source: FakeStatusSource());

    expect(find.byTooltip('Hosts'), findsOneWidget);

    await tester.tap(find.byKey(const ValueKey<String>('settings')));
    await settle(tester);

    expect(find.text('Hosts'), findsOneWidget);
    expect(find.textContaining('nvironment'), findsNothing);

    await tester.tap(find.text('Add host'));
    await settle(tester);

    expect(find.widgetWithText(AlertDialog, 'Add host'), findsOneWidget);

    await tester.tap(find.text('Cancel'));
    await settle(tester);
    await tester.tap(find.byTooltip('Actions for production'));
    await settle(tester);
    await tester.tap(find.text('Remove'));
    await settle(tester);

    expect(
      find.text('The host and its saved token are removed from this device.'),
      findsOneWidget,
    );
  });

  testWidgets('a host chosen in the settings loads on return', (tester) async {
    // The overview is covered meanwhile, so its listeners are paused.
    final source = FakeStatusSource();
    await pumpConsole(tester, source: source);
    expect(source.calls.single.$1, production);

    await tester.tap(find.byKey(const ValueKey<String>('settings')));
    await settle(tester);
    await tester.tap(find.text('Offline demo'));
    await settle(tester);

    expect(source.calls.last.$1.id, Environment.demoId);
    expect(
      source.calls.skip(1).map((call) => call.$1.id),
      everyElement(Environment.demoId),
    );
    expect(map, findsOneWidget);
  });

  testWidgets('with no host, the demo can be opened', (tester) async {
    await pumpConsole(
      tester,
      source: FakeStatusSource(),
      environments: null,
      size: const Size(320, 700),
    );

    expect(find.text('No hosts yet'), findsOneWidget);
    expect(find.text('Add host'), findsOneWidget);
    expect(find.text('Manage hosts'), findsOneWidget);

    await tester.tap(find.byKey(const ValueKey<String>('open-demo')));
    await settle(tester);

    expect(map, findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('on the web, the page origin is used and named by the host', (
    tester,
  ) async {
    final source = FakeStatusSource();
    await pumpConsole(
      tester,
      source: source,
      environments: null,
      webOrigin: 'https://yggdrasil.example.com',
    );

    expect(source.calls.single.$1.url, 'https://yggdrasil.example.com');
    expect(source.calls.single.$1.nameFromResponse, isTrue);

    await tester.tap(find.byKey(const ValueKey<String>('settings')));
    await settle(tester);

    // The settings list shows the adopted name: the response's host.
    expect(find.text('example.com'), findsOneWidget);
    expect(find.text(Environment.demoUrl), findsNothing);
  });

  testWidgets('a v1 host is named after its environment', (tester) async {
    await pumpConsole(
      tester,
      source: FakeStatusSource((_, _) async => v1Status()),
      environments: null,
      webOrigin: 'https://yggdrasil.example.com',
    );

    await tester.tap(find.byKey(const ValueKey<String>('settings')));
    await settle(tester);

    expect(find.text('Production'), findsOneWidget);
  });

  group('environment selector', () {
    final selector = find.byKey(const ValueKey<String>('environment-selector'));

    Set<String> chosen(WidgetTester tester) =>
        tester.widget<SegmentedButton<String>>(selector).selected;

    HostStatus withFailingIntegration() {
      final base = demoStatus();

      return HostStatus(
        host: base.host,
        generatedAt: base.generatedAt,
        status: base.status,
        environments: base.environments,
        systems: <SystemStatus>[
          for (final system in base.systems)
            SystemStatus(
              id: system.id,
              name: system.name,
              status: system.status,
              description: system.description,
              icon: system.icon,
              environments: <SystemEnvironment>[
                for (final environment in system.environments)
                  SystemEnvironment(
                    environment: environment.environment,
                    status: environment.status,
                    applications: environment.applications,
                    onDemand: environment.onDemand,
                    integrations:
                        system.id == 'heimdall' &&
                            environment.environment == 'production'
                        ? const <IntegrationStatus>[
                            IntegrationStatus(
                              from: 'heimdall-api',
                              system: 'fortuna',
                              check: 'fortuna',
                              status: IntegrationHealth.down,
                            ),
                          ]
                        : environment.integrations,
                  ),
              ],
            ),
        ],
      );
    }

    testWidgets(
      "the selector lists the host's environments with a status mark",
      (tester) async {
        await pumpConsole(tester, source: FakeStatusSource());

        for (final name in <String>[
          'Development',
          'Homologation',
          'Production',
        ]) {
          expect(
            find.descendant(of: selector, matching: find.text(name)),
            findsOneWidget,
          );
        }

        // The mark is the status's own shape, with the status in words for
        // screen readers.
        final marks = tester
            .widgetList<Icon>(
              find.descendant(of: selector, matching: find.byType(Icon)),
            )
            .map((icon) => (icon.icon, icon.semanticLabel))
            .toList();
        expect(marks, <(IconData?, String?)>[
          (Icons.pause_circle_outline, 'Stopped'),
          (Icons.pause_circle_outline, 'Stopped'),
          (Icons.warning_rounded, 'Degraded'),
        ]);
      },
    );

    testWidgets('the first time, the last environment is selected', (
      tester,
    ) async {
      await pumpConsole(tester, source: FakeStatusSource());

      expect(chosen(tester), <String>{'production'});
    });

    testWidgets('the choice is remembered per host', (tester) async {
      final store = InMemoryRefreshSettingsStore();
      await pumpConsole(
        tester,
        source: FakeStatusSource(),
        refreshSettings: store,
      );

      await tester.tap(
        find.descendant(of: selector, matching: find.text('Development')),
      );
      await settle(tester);
      expect(chosen(tester), <String>{'development'});
      expect(await store.readEnvironmentFor(production.id), 'development');

      // Another host starts from its own memory: the last environment.
      await switchHost(tester, 'homologation');
      expect(chosen(tester), <String>{'production'});

      await switchHost(tester, 'production');
      expect(chosen(tester), <String>{'development'});
    });

    testWidgets('a saved choice is used on start', (tester) async {
      final store = InMemoryRefreshSettingsStore();
      await store.writeEnvironmentFor(production.id, 'homologation');
      await pumpConsole(
        tester,
        source: FakeStatusSource(),
        refreshSettings: store,
      );

      expect(chosen(tester), <String>{'homologation'});
    });

    testWidgets(
      'a remembered environment that is gone falls back to the last',
      (tester) async {
        final store = InMemoryRefreshSettingsStore();
        await store.writeEnvironmentFor(production.id, 'staging');
        await pumpConsole(
          tester,
          source: FakeStatusSource(),
          refreshSettings: store,
        );

        expect(chosen(tester), <String>{'production'});
      },
    );

    testWidgets('no environments shows the empty state', (tester) async {
      await pumpConsole(
        tester,
        source: FakeStatusSource(
          (_, _) async => const HostStatus(
            host: 'example.com',
            generatedAt: null,
            status: Status.up,
            environments: <HostEnvironment>[],
            systems: <SystemStatus>[],
          ),
        ),
      );

      expect(selector, findsNothing);
      expect(find.text('This host reports no environments.'), findsOneWidget);
      expect(find.text('This host has no systems.'), findsOneWidget);
    });

    testWidgets('narrow screens get a dropdown', (tester) async {
      await pumpConsole(
        tester,
        source: FakeStatusSource(),
        size: const Size(320, 900),
      );

      expect(
        find.descendant(
          of: selector,
          matching: find.byType(DropdownButton<String>),
        ),
        findsOneWidget,
      );
      expect(
        find.descendant(
          of: selector,
          matching: find.byType(SegmentedButton<String>),
        ),
        findsNothing,
      );
    });

    testWidgets('the summary names the failing integrations', (tester) async {
      await pumpConsole(
        tester,
        source: FakeStatusSource((_, _) async => withFailingIntegration()),
      );

      expect(find.text('1 integration failing'), findsOneWidget);

      await tester.tap(
        find.descendant(of: selector, matching: find.text('Development')),
      );
      await settle(tester);

      expect(find.textContaining('failing'), findsNothing);
    });

    testWidgets('changing environment does not load', (tester) async {
      final source = FakeStatusSource();
      await pumpConsole(tester, source: source);

      await tester.tap(
        find.descendant(of: selector, matching: find.text('Development')),
      );
      await settle(tester);

      expect(source.calls, hasLength(1));
      expect(source.detailsCalls, hasLength(1));
    });

    testWidgets('the saved-host switcher is still offered with two hosts', (
      tester,
    ) async {
      await pumpConsole(tester, source: FakeStatusSource());

      expect(hostSwitcher, findsOneWidget);
    });
  });

  group('refresh controls', () {
    testWidgets('the auto-refresh menu offers 1, 5, 10, 15 and 30 minutes', (
      tester,
    ) async {
      await pumpConsole(tester, source: FakeStatusSource());

      await tester.tap(
        find.byKey(const ValueKey<String>('auto-refresh-interval')),
      );
      await settle(tester);

      for (final label in <String>[
        'Every 5 minutes',
        'Every 10 minutes',
        'Every 15 minutes',
        'Every 30 minutes',
      ]) {
        expect(find.text(label), findsOneWidget);
      }
      // The current value on the button, and its entry in the menu.
      expect(find.text('Every 1 minute'), findsNWidgets(2));
    });

    testWidgets('the switch and the menu are saved', (tester) async {
      final store = InMemoryRefreshSettingsStore();
      await pumpConsole(
        tester,
        source: FakeStatusSource(),
        refreshSettings: store,
      );

      await tester.tap(find.byKey(const ValueKey<String>('auto-refresh')));
      await settle(tester);
      expect(store.settings, const RefreshSettings(enabled: true));

      await tester.tap(
        find.byKey(const ValueKey<String>('auto-refresh-interval')),
      );
      await settle(tester);
      await tester.tap(find.text('Every 10 minutes'));
      await settle(tester);

      expect(
        store.settings,
        const RefreshSettings(enabled: true, interval: Duration(minutes: 10)),
      );
    });

    testWidgets('updated time is visible beside Refresh now', (tester) async {
      await pumpConsole(tester, source: FakeStatusSource());

      final updated = find.textContaining('Updated ');
      final refresh = find.byKey(const ValueKey<String>('refresh'));

      expect(updated, findsOneWidget);
      expect(refresh, findsOneWidget);
      expect(find.text('Refresh now'), findsOneWidget);
      expect(
        (tester.getCenter(updated).dy - tester.getCenter(refresh).dy).abs(),
        lessThan(24),
      );
    });
  });

  group('system panel', () {
    final panel = find.byKey(const ValueKey<String>('system-panel'));

    Finder inPanel(String text) =>
        find.descendant(of: panel, matching: find.text(text));

    String ago(Duration age) =>
        DateTime.now().toUtc().subtract(age).toIso8601String();

    Map<String, dynamic> api(Map<String, dynamic> production) =>
        (production['applications'] as List<dynamic>)
            .cast<Map<String, dynamic>>()
            .firstWhere((application) => application['id'] == 'heimdall-api');

    /// The demo with [edit] applied to Heimdall in production.
    HostStatus heimdallWith(
      void Function(Map<String, dynamic> production) edit,
    ) {
      final json = jsonDecode(demoJson()) as Map<String, dynamic>;
      final systems = (json['systems'] as List<dynamic>)
          .cast<Map<String, dynamic>>();
      final heimdall = systems.firstWhere(
        (system) => system['id'] == 'heimdall',
      );
      final production = (heimdall['environments'] as List<dynamic>)
          .cast<Map<String, dynamic>>()
          .firstWhere((entry) => entry['environment'] == 'production');

      // Start from a Heimdall with nothing reported, and without the demo's
      // Hermes, which the stub below stands in for: each test declares the
      // details and integrations it looks at.
      systems.removeWhere((system) => system['id'] == 'hermes');
      heimdall['status'] = 'up';
      production
        ..['status'] = 'up'
        ..['integrations'] = <Map<String, dynamic>>[];
      api(production)
        ..['status'] = 'up'
        ..['details'] = null;

      edit(production);
      // The target of the integrations the tests declare.
      (json['systems'] as List<dynamic>).add(<String, dynamic>{
        'id': 'hermes',
        'name': 'Hermes mail',
        'status': 'up',
        'environments': <Map<String, dynamic>>[
          <String, dynamic>{
            'environment': 'production',
            'status': 'up',
            'applications': <Map<String, dynamic>>[],
          },
        ],
      });

      return HostStatus.fromJson(json);
    }

    Future<void> openHeimdall(WidgetTester tester, HostStatus host) async {
      await pumpConsole(tester, source: FakeStatusSource((_, _) async => host));
      await tester.tap(tile('heimdall'));
      await settle(tester);
    }

    Map<String, dynamic> hermesLink({String status = 'up', String? reason}) =>
        <String, dynamic>{
          'from': 'heimdall-api',
          'system': 'hermes',
          'external': null,
          'check': 'Hermes',
          'status': status,
          'reason': reason,
        };

    testWidgets('the panel lists applications with status, version and '
        'deploy time', (tester) async {
      await openHeimdall(tester, heimdallWith((_) {}));

      expect(panel, findsOneWidget);
      expect(
        find.descendant(of: panel, matching: find.text('Heimdall')),
        findsOneWidget,
      );
      expect(
        find.descendant(
          of: panel,
          matching: find.text('Identity and access management'),
        ),
        findsOneWidget,
      );
      expect(
        find.descendant(of: panel, matching: find.text('Production')),
        findsOneWidget,
      );
      expect(find.text('Heimdall API'), findsOneWidget);
      expect(find.text('Heimdall web'), findsOneWidget);
      expect(find.text('v1.4.0 · 3f2a9c1'), findsOneWidget);
      expect(find.textContaining('deployed '), findsWidgets);
      expect(find.text('Up'), findsWidgets);
    });

    testWidgets('an application row opens the existing application detail', (
      tester,
    ) async {
      await openHeimdall(tester, heimdallWith((_) {}));

      await tester.tap(
        find.byKey(
          const ValueKey<String>('application-production-heimdall-api'),
        ),
      );
      await settle(tester);

      expect(find.byType(ApplicationDetail), findsOneWidget);
      expect(find.text('Deployment'), findsOneWidget);
      expect(
        find.text('heimdall-api:production-1.4.0-3f2a9c1'),
        findsOneWidget,
      );
    });

    testWidgets('reported services show status, reason and checked time', (
      tester,
    ) async {
      await openHeimdall(
        tester,
        heimdallWith((production) {
          api(production)['details'] = <String, dynamic>{
            'state': 'reported',
            'checkedAt': ago(const Duration(minutes: 3)),
            'status': 'Degraded',
            'services': <Map<String, dynamic>>[
              <String, dynamic>{'name': 'Database', 'status': 'Healthy'},
              <String, dynamic>{
                'name': 'Cache',
                'status': 'Degraded',
                'reason': 'slow replies',
              },
            ],
          };
        }),
      );

      expect(
        find.text('Reported by Heimdall API · checked 3 min ago'),
        findsOneWidget,
      );
      expect(find.text('Database'), findsOneWidget);
      expect(find.text('Healthy'), findsOneWidget);
      expect(find.text('Cache'), findsOneWidget);
      // Other systems on the map are degraded in the demo; this is the panel's.
      expect(inPanel('Degraded'), findsOneWidget);
      expect(find.text('slow replies'), findsOneWidget);
    });

    testWidgets('an unavailable report says it could not get more details, '
        'with the reason', (tester) async {
      await openHeimdall(
        tester,
        heimdallWith((production) {
          api(production)['details'] = <String, dynamic>{
            'state': 'unavailable',
            'checkedAt': ago(const Duration(seconds: 20)),
            'reason': 'connection refused',
          };
        }),
      );

      expect(
        find.text(
          'Could not get more details. The detailed health endpoint did not '
          'answer: connection refused.',
        ),
        findsOneWidget,
      );
    });

    testWidgets('integrations of an unavailable report read Not checked', (
      tester,
    ) async {
      await openHeimdall(
        tester,
        heimdallWith((production) {
          production['integrations'] = <Map<String, dynamic>>[hermesLink()];
          api(production)['details'] = <String, dynamic>{
            'state': 'unavailable',
            'checkedAt': ago(const Duration(seconds: 20)),
            'reason': 'connection refused',
          };
        }),
      );

      // The check's name, and the target system's.
      expect(find.text('Hermes'), findsOneWidget);
      expect(find.text('Hermes mail'), findsWidgets);
      expect(find.text('Not checked'), findsOneWidget);
      expect(find.text('integration'), findsOneWidget);
    });

    testWidgets('an unsupported report says this version does not report '
        'detailed health', (tester) async {
      await openHeimdall(
        tester,
        heimdallWith((production) {
          api(production)['details'] = <String, dynamic>{
            'state': 'unsupported',
            'checkedAt': ago(const Duration(seconds: 20)),
          };
        }),
      );

      expect(
        find.text('This version does not report detailed health'),
        findsOneWidget,
      );
    });

    testWidgets('a report not checked yet says so, without a time', (
      tester,
    ) async {
      await openHeimdall(
        tester,
        heimdallWith((production) {
          api(production)['details'] = <String, dynamic>{
            'state': 'not_checked',
          };
        }),
      );

      expect(find.text('Reported by Heimdall API'), findsOneWidget);
      expect(find.text('Not checked yet'), findsOneWidget);
    });

    testWidgets('no detailedHealth means no Reported by section', (
      tester,
    ) async {
      await openHeimdall(tester, heimdallWith((_) {}));

      expect(find.textContaining('Reported by'), findsNothing);
    });

    testWidgets('a stopped dependency reads stopped, not failing', (
      tester,
    ) async {
      await openHeimdall(
        tester,
        heimdallWith((production) {
          production['integrations'] = <Map<String, dynamic>>[
            hermesLink(status: 'stopped', reason: 'not running'),
          ];
          api(production)['details'] = <String, dynamic>{
            'state': 'reported',
            'checkedAt': ago(const Duration(minutes: 1)),
            'status': 'Unhealthy',
            'services': <Map<String, dynamic>>[
              <String, dynamic>{
                'name': 'Hermes',
                'status': 'Unhealthy',
                'reason': 'connection refused',
              },
            ],
          };
        }),
      );

      expect(inPanel('Stopped'), findsOneWidget);
      expect(inPanel('Unhealthy'), findsNothing);
      expect(inPanel('connection refused'), findsNothing);
      expect(inPanel('not running'), findsNothing);
    });

    testWidgets('a failing dependency shows its reason', (tester) async {
      await openHeimdall(
        tester,
        heimdallWith((production) {
          production['integrations'] = <Map<String, dynamic>>[
            hermesLink(status: 'down', reason: 'connection refused'),
          ];
          api(production)['details'] = <String, dynamic>{
            'state': 'reported',
            'checkedAt': ago(const Duration(minutes: 1)),
            'status': 'Unhealthy',
            'services': <Map<String, dynamic>>[
              <String, dynamic>{'name': 'Hermes', 'status': 'Unhealthy'},
            ],
          };
        }),
      );

      expect(inPanel('Failing'), findsOneWidget);
      expect(inPanel('connection refused'), findsOneWidget);
    });

    testWidgets('an external target is named as written', (tester) async {
      await openHeimdall(
        tester,
        heimdallWith((production) {
          production['integrations'] = <Map<String, dynamic>>[
            <String, dynamic>{
              'from': 'heimdall-api',
              'system': null,
              'external': 'Mailgun',
              'check': 'Mail',
              'status': 'up',
            },
          ];
          api(production)['details'] = <String, dynamic>{
            'state': 'reported',
            'checkedAt': ago(const Duration(minutes: 1)),
            'status': 'Healthy',
            'services': <Map<String, dynamic>>[
              <String, dynamic>{'name': 'Mail', 'status': 'Healthy'},
            ],
          };
        }),
      );

      expect(
        find.descendant(of: panel, matching: find.text('Mailgun')),
        findsOneWidget,
      );
      expect(
        find.descendant(of: panel, matching: find.byIcon(Icons.cloud)),
        findsOneWidget,
      );
    });

    testWidgets('the panel shows on demand when it applies', (tester) async {
      await openHeimdall(
        tester,
        heimdallWith((production) => production['onDemand'] = true),
      );

      expect(
        find.descendant(of: panel, matching: find.text('on demand')),
        findsOneWidget,
      );
    });

    testWidgets('the panel does not say on demand when it does not apply', (
      tester,
    ) async {
      await openHeimdall(tester, heimdallWith((_) {}));

      expect(
        find.descendant(of: panel, matching: find.text('on demand')),
        findsNothing,
      );

      await tester.tap(find.byTooltip('Close'));
      await settle(tester);

      expect(panel, findsNothing);
    });

    testWidgets('a phone gets a bottom sheet', (tester) async {
      await pumpConsole(
        tester,
        source: FakeStatusSource((_, _) async => heimdallWith((_) {})),
        size: const Size(390, 900),
      );
      await tester.tap(tile('heimdall'));
      await settle(tester);

      expect(find.byType(BottomSheet), findsOneWidget);
      expect(panel, findsOneWidget);
    });

    testWidgets('a computer gets a side panel with the map still visible', (
      tester,
    ) async {
      await openHeimdall(tester, heimdallWith((_) {}));

      expect(find.byType(BottomSheet), findsNothing);
      expect(tester.getSize(panel).width, 420);
      expect(tester.getRect(panel).right, 1000);
      expect(tile('fortuna'), findsOneWidget);
      expect(tester.getRect(tile('fortuna')).right, lessThan(580));
    });
  });
}
