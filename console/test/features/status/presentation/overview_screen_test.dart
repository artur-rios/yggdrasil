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

const List<String> demoSystems = <String>['heimdall', 'fortuna', 'yggdrasil'];

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
    expect(find.text('3 systems · 3 environments'), findsOneWidget);
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
      find.text('3 systems · 3 environments · 1 degraded'),
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
          (Icons.check_circle, 'Up'),
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
}
