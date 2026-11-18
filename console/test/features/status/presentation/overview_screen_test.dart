import 'dart:async';
import 'dart:convert';

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:material_ui/material_ui.dart';
import 'package:yggdrasil_console/features/environments/data/environment_store.dart';
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

/// A store whose saved hosts cannot be read.
class FailingEnvironmentStore extends InMemoryEnvironmentStore {
  @override
  Future<List<Environment>?> readEnvironments() async =>
      throw StateError('damaged');
}

void main() {
  testWidgets(
    'given a host, when the overview opens, then it shows the host, its '
    'summary and its systems',
    (tester) async {
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
    },
  );

  for (final width in <double>[320, 360, 800, 1400]) {
    testWidgets(
      'given $width dp of width, when the overview opens, then the map fits '
      'and nothing overflows',
      (tester) async {
        await pumpConsole(
          tester,
          source: FakeStatusSource((_, _) async => demoWithAProblem()),
          size: Size(width, 1200),
        );

        expect(map, findsOneWidget);
        expect(tester.getRect(map).right, lessThanOrEqualTo(width));
        expect(tester.takeException(), isNull);
      },
    );
  }

  testWidgets(
    'given the overview, when it opens, then there is no problems filter any '
    'more',
    (tester) async {
      await pumpConsole(tester, source: FakeStatusSource());

      expect(find.byKey(const ValueKey<String>('problems-only')), findsNothing);
      expect(find.textContaining('Problems only'), findsNothing);
    },
  );

  testWidgets(
    'given a problem in the selected environment, when the overview opens, '
    'then it shows on its system and in the summary',
    (tester) async {
      final handle = tester.ensureSemantics();

      await pumpConsole(
        tester,
        source: FakeStatusSource((_, _) async => demoWithAProblem()),
      );

      expect(
        find.text('4 systems · 3 environments · 3 degraded'),
        findsOneWidget,
      );
      expect(
        find.bySemanticsLabel(RegExp('^Fortuna, degraded')),
        findsOneWidget,
      );

      handle.dispose();
    },
  );

  testWidgets('given a v1 host, when the overview opens, then it shows as one '
      'environment', (tester) async {
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

  testWidgets(
    'given two hosts, when the other is picked, then that host loads',
    (tester) async {
      final source = FakeStatusSource();
      await pumpConsole(tester, source: source);

      await switchHost(tester, 'homologation');

      expect(source.calls.last.$1, homologation);
    },
  );

  for (final size in <Size>[const Size(360, 800), const Size(1000, 900)]) {
    testWidgets(
      'given three hosts at ${size.width} dp, when the overview opens, then '
      'they switch with a compact dropdown',
      (tester) async {
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
      },
    );
  }

  testWidgets(
    'given a 401, when the token is entered, then it retries with it',
    (tester) async {
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
      expect(
        find.byKey(const ValueKey<String>('failure-banner')),
        findsNothing,
      );
    },
  );

  testWidgets(
    'given data on screen, when a refresh fails with a network error, then '
    'the last data stays, greyed, with a warning',
    (tester) async {
      final source = FakeStatusSource();
      await pumpConsole(tester, source: source);
      expect(
        find.byKey(const ValueKey<String>('failure-banner')),
        findsNothing,
      );

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
    },
  );

  testWidgets(
    'given data on screen, when a refresh is refused with a 401 and the token '
    'dialog cancelled, then it says the host needs a valid token',
    (tester) async {
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
    },
  );

  testWidgets(
    'given a 503 with Retry-After, when the overview opens, then it shows '
    '"starting" and retries after it',
    (tester) async {
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
      expect(
        find.byKey(const ValueKey<String>('failure-banner')),
        findsNothing,
      );
      expect(find.text('Token for production'), findsNothing);
      expect(source.calls, hasLength(1));

      source.respond = null;
      await tester.pump(const Duration(seconds: 5));
      await settle(tester);

      expect(source.calls, hasLength(2), reason: 'retried after Retry-After');
      expect(
        find.byKey(const ValueKey<String>('starting-banner')),
        findsNothing,
      );
      expect(map, findsOneWidget);
      expect(find.byType(ColorFiltered), findsNothing, reason: 'not stale');
    },
  );

  testWidgets(
    'given an error and no data yet, when Retry is tapped, then it loads '
    'again',
    (tester) async {
      final source = FakeStatusSource(
        (_, _) async => throw const StatusException(
          StatusFailureKind.server,
          statusCode: 503,
        ),
      );
      await pumpConsole(tester, source: source);

      expect(
        find.text('The status API answered with HTTP 503.'),
        findsOneWidget,
      );

      source.respond = null;
      await tester.tap(find.text('Retry'));
      await settle(tester);

      expect(map, findsOneWidget);
    },
  );

  testWidgets('given an error and no data yet, when the overview shows, then '
      'there is neither a map nor a spinner', (tester) async {
    await pumpConsole(
      tester,
      source: FakeStatusSource(
        (_, _) async => throw const StatusException(StatusFailureKind.network),
      ),
    );

    expect(
      find.byKey(const ValueKey<String>('failure-banner')),
      findsOneWidget,
    );
    expect(find.byIcon(Icons.cloud_off), findsOneWidget);
    expect(map, findsNothing);
    expect(find.byType(CircularProgressIndicator), findsNothing);
    // Nothing was received, so there is no "last data" to speak of.
    expect(find.textContaining('Showing the last data'), findsNothing);
  });

  for (final (kind, icon) in <(StatusFailureKind, IconData)>[
    (StatusFailureKind.network, Icons.cloud_off),
    (StatusFailureKind.server, Icons.error_outline),
    (StatusFailureKind.invalidResponse, Icons.error_outline),
  ]) {
    testWidgets('given a ${kind.name} failure, when the banner shows, then it '
        'has its own icon and no token button', (tester) async {
      await pumpConsole(
        tester,
        source: FakeStatusSource((_, _) async => throw StatusException(kind)),
      );

      final banner = find.byKey(const ValueKey<String>('failure-banner'));
      expect(
        find.descendant(of: banner, matching: find.byIcon(icon)),
        findsOneWidget,
      );
      expect(find.text('Enter token'), findsNothing);
    });
  }

  group('a refused token', () {
    FakeStatusSource refusing() => FakeStatusSource(
      (_, token) async => token == 'right'
          ? demoStatus()
          : throw const StatusException(
              StatusFailureKind.unauthorized,
              statusCode: 401,
            ),
    );

    testWidgets('given the token dialog cancelled, when Enter token is '
        'tapped, then the dialog opens again and the token is used', (
      tester,
    ) async {
      final source = refusing();
      await pumpConsole(tester, source: source);
      await tester.tap(find.text('Cancel'));
      await settle(tester);

      final banner = find.byKey(const ValueKey<String>('failure-banner'));
      expect(
        find.descendant(of: banner, matching: find.byIcon(Icons.lock_outline)),
        findsOneWidget,
      );

      await tester.tap(find.text('Enter token'));
      await settle(tester);

      // Asked for, not refused: the plain wording.
      expect(
        find.text(
          'The YGGDRASIL_STATUS_TOKEN of https://yggdrasil.example.com.',
        ),
        findsOneWidget,
      );

      await tester.enterText(
        find.byKey(const ValueKey<String>('token-field')),
        'right',
      );
      await tester.tap(find.text('Save'));
      await settle(tester);

      expect(source.calls.last.$2, 'right');
      expect(map, findsOneWidget);
      expect(banner, findsNothing);
    });

    testWidgets('given the token dialog cancelled, when Retry is refused '
        'again, then the dialog does not open by itself', (tester) async {
      final source = refusing();
      await pumpConsole(tester, source: source);
      await tester.tap(find.text('Cancel'));
      await settle(tester);

      await tester.tap(find.text('Retry'));
      await settle(tester);

      expect(source.calls, hasLength(2));
      expect(find.byType(AlertDialog), findsNothing);
      expect(find.text('Enter token'), findsOneWidget);
    });
  });

  group('the last data received', () {
    /// The demo as generated [age] ago, or with no time when [age] is null.
    HostStatus generated(Duration? age) {
      final demo = demoStatus();

      return HostStatus(
        host: demo.host,
        // Real time: the banner reads DateTime.now().
        generatedAt: age == null ? null : DateTime.now().toUtc().subtract(age),
        status: demo.status,
        environments: demo.environments,
        systems: demo.systems,
      );
    }

    Future<void> failAfter(WidgetTester tester, HostStatus host) async {
      final source = FakeStatusSource((_, _) async => host);
      await pumpConsole(tester, source: source);

      source.respond = (_, _) =>
          throw const StatusException(StatusFailureKind.network);
      await tester.tap(find.byKey(const ValueKey<String>('refresh')));
      await settle(tester);
    }

    testWidgets('given data without a generation time, when a refresh fails, '
        'then it only says the last data is shown', (tester) async {
      await failAfter(tester, generated(null));

      expect(find.text('Showing the last data received.'), findsOneWidget);
    });

    // Mid-unit ages, so a slow run cannot cross into the next unit.
    for (final (age, pattern) in <(Duration, String)>[
      (const Duration(seconds: 20), r'generated \d\d s ago\.$'),
      (const Duration(minutes: 5, seconds: 30), r'generated 5 min ago\.$'),
      (const Duration(hours: 2, minutes: 30), r'generated 2 h ago\.$'),
    ]) {
      testWidgets('given data generated ${age.inSeconds} s ago, when a '
          'refresh fails, then the banner says how old it is', (tester) async {
        await failAfter(tester, generated(age));

        expect(
          find.textContaining(
            RegExp('^Showing the last data received, $pattern'),
          ),
          findsOneWidget,
        );
      });
    }
  });

  group('starting', () {
    testWidgets('given a Retry-After of 12 s and no data, when the overview '
        'shows, then it says when it tries again and spins', (tester) async {
      await pumpConsole(
        tester,
        source: FakeStatusSource(
          (_, _) async => throw const StatusException(
            StatusFailureKind.starting,
            statusCode: 503,
            retryAfter: Duration(seconds: 12),
          ),
        ),
      );

      expect(
        find.text(
          'The status API is starting and has no results yet. '
          'Trying again in 12 s.',
        ),
        findsOneWidget,
      );
      expect(find.byType(CircularProgressIndicator), findsOneWidget);
    });

    testWidgets('given no Retry-After, when the overview shows, then it '
        'tries again in 5 s', (tester) async {
      final source = FakeStatusSource(
        (_, _) async => throw const StatusException(StatusFailureKind.starting),
      );
      await pumpConsole(tester, source: source);

      expect(find.textContaining('Trying again in 5 s.'), findsOneWidget);

      await tester.pump(const Duration(seconds: 5));
      await settle(tester);

      expect(source.calls, hasLength(2));
    });
  });

  testWidgets('given a load in flight, when the overview shows, then a '
      'progress bar runs under the app bar until it ends', (tester) async {
    final details = Completer<HostStatus?>();
    final source = FakeStatusSource()
      ..respondDetails = (_, _) => details.future;
    await pumpConsole(tester, source: source);

    expect(find.byType(LinearProgressIndicator), findsOneWidget);
    expect(
      tester
          .widget<ButtonStyleButton>(
            find.byKey(const ValueKey<String>('refresh')),
          )
          .onPressed,
      isNull,
      reason: 'Refresh now waits for the load',
    );

    details.complete(demoStatus());
    await settle(tester);

    expect(find.byType(LinearProgressIndicator), findsNothing);
  });

  testWidgets('given a loaded host, when the overview is pulled down, then '
      'it loads again', (tester) async {
    final source = FakeStatusSource();
    await pumpConsole(tester, source: source);

    await tester.fling(map, const Offset(0, 400), 1000);
    await settle(tester);

    expect(source.calls, hasLength(2));
  });

  testWidgets('given one host only, when the overview opens, then there is '
      'no host switcher', (tester) async {
    await pumpConsole(
      tester,
      source: FakeStatusSource(),
      environments: const <Environment>[production],
    );

    expect(hostSwitcher, findsNothing);
    expect(map, findsOneWidget);
  });

  testWidgets('given the hosts cannot be read, when the console opens, then '
      'it says so', (tester) async {
    await pumpConsole(
      tester,
      source: FakeStatusSource(),
      environmentStore: FailingEnvironmentStore(),
    );

    expect(
      find.textContaining('Could not load the hosts: Bad state: damaged'),
      findsOneWidget,
    );
  });

  testWidgets('given no host, when Add host is used, then the new host '
      'loads', (tester) async {
    final source = FakeStatusSource();
    await pumpConsole(
      tester,
      source: source,
      environments: const <Environment>[],
    );

    await tester.tap(find.text('Add host'));
    await settle(tester);
    await tester.enterText(
      find.byKey(const ValueKey<String>('environment-name')),
      'vps',
    );
    await tester.enterText(
      find.byKey(const ValueKey<String>('environment-url')),
      'https://vps.example.com',
    );
    await tester.tap(find.text('Save'));
    await settle(tester);

    expect(source.calls.single.$1.url, 'https://vps.example.com');
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

    testWidgets(
      'given the overview, when it starts, then it loads once: one fetch and '
      'one refreshDetails',
      (tester) async {
        final source = FakeStatusSource();
        await pumpConsole(tester, source: source);

        expect(source.calls, hasLength(1));
        expect(source.detailsCalls, hasLength(1));
      },
    );

    testWidgets(
      'given auto-refresh off, when time passes, then it does not load again',
      (tester) async {
        final source = FakeStatusSource();
        await pumpConsole(tester, source: source);

        await tester.pump(const Duration(minutes: 10));
        await settle(tester);

        expect(source.calls, hasLength(1));
        expect(source.detailsCalls, hasLength(1));
      },
    );

    testWidgets(
      'given a loaded host, when Refresh now is tapped, then it loads again',
      (tester) async {
        final source = FakeStatusSource();
        await pumpConsole(tester, source: source);

        await tester.tap(find.byKey(const ValueKey<String>('refresh')));
        await settle(tester);

        expect(source.calls, hasLength(2));
        expect(source.detailsCalls, hasLength(2));
      },
    );

    testWidgets(
      'given auto-refresh on with the default interval, when minutes pass, '
      'then it loads every minute',
      (tester) async {
        final source = FakeStatusSource();
        await pumpConsole(
          tester,
          source: source,
          refreshSettings: autoRefresh(),
        );
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
      },
    );

    for (final interval in autoRefreshIntervals) {
      testWidgets(
        'given auto-refresh every ${interval.inMinutes} min, when that time '
        'passes, then it loads again, not before',
        (tester) async {
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
        },
      );
    }

    testWidgets(
      'given auto-refresh off, when it is turned on, then the timer starts',
      (tester) async {
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
      },
    );

    testWidgets(
      'given auto-refresh on, when the app is hidden and shown again, then it '
      'pauses meanwhile and loads on return',
      (tester) async {
        final source = FakeStatusSource();
        await pumpConsole(
          tester,
          source: source,
          refreshSettings: autoRefresh(),
        );

        hide(tester);
        await tester.pump(const Duration(minutes: 5));
        expect(source.calls, hasLength(1));

        show(tester);
        await settle(tester);
        expect(source.calls, hasLength(2), reason: 'loads on return');

        await tester.pump(const Duration(minutes: 1));
        await settle(tester);
        expect(source.calls, hasLength(3));
      },
    );

    testWidgets(
      'given auto-refresh off, when the app is hidden and shown again, then it '
      'does not load',
      (tester) async {
        final source = FakeStatusSource();
        await pumpConsole(tester, source: source);

        hide(tester);
        await tester.pump(const Duration(minutes: 5));
        show(tester);
        await settle(tester);

        expect(source.calls, hasLength(1));
        expect(source.detailsCalls, hasLength(1));
      },
    );

    testWidgets(
      'given auto-refresh on, when the hosts screen is closed, then it pauses '
      'meanwhile and loads on return',
      (tester) async {
        final source = FakeStatusSource();
        await pumpConsole(
          tester,
          source: source,
          refreshSettings: autoRefresh(),
        );

        await tester.tap(find.byKey(const ValueKey<String>('settings')));
        await settle(tester);
        await tester.pump(const Duration(minutes: 5));
        expect(source.calls, hasLength(1), reason: 'covered, so paused');

        await tester.pageBack();
        await settle(tester);

        expect(source.calls, hasLength(2));
      },
    );

    testWidgets(
      'given auto-refresh off, when the hosts screen is closed, then it does '
      'not load',
      (tester) async {
        final source = FakeStatusSource();
        await pumpConsole(tester, source: source);

        await tester.tap(find.byKey(const ValueKey<String>('settings')));
        await settle(tester);
        await tester.pageBack();
        await settle(tester);

        expect(source.calls, hasLength(1));
        expect(source.detailsCalls, hasLength(1));
      },
    );

    testWidgets(
      'given the details fail, when the overview loads, then the map stays '
      'on screen with the failure',
      (tester) async {
        final source = FakeStatusSource()
          ..respondDetails = (_, _) =>
              throw const StatusException(StatusFailureKind.server);
        await pumpConsole(tester, source: source);

        expect(map, findsOneWidget);
        expect(
          find.byKey(const ValueKey<String>('failure-banner')),
          findsOneWidget,
        );
      },
    );

    testWidgets(
      'given a starting answer, when the retry time passes, then it retries '
      'once and does not poll',
      (tester) async {
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
      },
    );

    testWidgets(
      'given a starting retry pending, when the hosts screen covers the '
      'overview and closes, then the retry is cancelled and re-armed on '
      'return',
      (tester) async {
        final source = FakeStatusSource(
          (_, _) async => throw const StatusException(
            StatusFailureKind.starting,
            statusCode: 503,
            retryAfter: Duration(seconds: 5),
          ),
        );
        await pumpConsole(tester, source: source);
        expect(source.calls, hasLength(1));

        await tester.tap(find.byKey(const ValueKey<String>('settings')));
        await settle(tester);
        await tester.pump(const Duration(seconds: 10));
        expect(source.calls, hasLength(1), reason: 'the retry was cancelled');

        source.respond = null;
        await tester.pageBack();
        await settle(tester);

        expect(source.calls, hasLength(2), reason: 'loads on return');
        expect(map, findsOneWidget);
      },
    );
  });

  testWidgets(
    'given a host chosen on the hosts screen, when it closes, then that host '
    'loads',
    (tester) async {
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
    },
  );

  testWidgets('given no host, when the demo is opened, then its map shows', (
    tester,
  ) async {
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

  testWidgets(
    'given nothing saved on the web, when the overview loads, then the page '
    'origin is used and named after the response',
    (tester) async {
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
    },
  );

  testWidgets(
    'given a v1 host at the page origin, when it loads, then it is named '
    'after its environment',
    (tester) async {
      await pumpConsole(
        tester,
        source: FakeStatusSource((_, _) async => v1Status()),
        environments: null,
        webOrigin: 'https://yggdrasil.example.com',
      );

      await tester.tap(find.byKey(const ValueKey<String>('settings')));
      await settle(tester);

      expect(find.text('Production'), findsOneWidget);
    },
  );

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
      "given a host, when the overview opens, then the selector lists the "
      "host's environments, each with a status mark",
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

    testWidgets(
      'given nothing remembered, when the overview opens, then the last '
      'environment is selected',
      (tester) async {
        await pumpConsole(tester, source: FakeStatusSource());

        expect(chosen(tester), <String>{'production'});
      },
    );

    testWidgets(
      'given an environment chosen, when hosts are switched, then the choice '
      'is remembered per host',
      (tester) async {
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
      },
    );

    testWidgets(
      'given a saved choice, when the overview opens, then it is selected',
      (tester) async {
        final store = InMemoryRefreshSettingsStore();
        await store.writeEnvironmentFor(production.id, 'homologation');
        await pumpConsole(
          tester,
          source: FakeStatusSource(),
          refreshSettings: store,
        );

        expect(chosen(tester), <String>{'homologation'});
      },
    );

    testWidgets(
      'given a remembered environment that is gone, when the overview opens, '
      'then the last one is selected',
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

    testWidgets(
      'given a host with no environments, when it loads, then the empty state '
      'shows',
      (tester) async {
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
      },
    );

    testWidgets(
      'given a narrow screen, when the overview opens, then the selector is a '
      'dropdown',
      (tester) async {
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
      },
    );

    testWidgets(
      'given a failing integration in production, when each environment is '
      'shown, then only production names it',
      (tester) async {
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
      },
    );

    testWidgets(
      'given a loaded host, when another environment is chosen, then nothing '
      'loads',
      (tester) async {
        final source = FakeStatusSource();
        await pumpConsole(tester, source: source);

        await tester.tap(
          find.descendant(of: selector, matching: find.text('Development')),
        );
        await settle(tester);

        expect(source.calls, hasLength(1));
        expect(source.detailsCalls, hasLength(1));
      },
    );

    testWidgets(
      'given two hosts, when the overview opens, then the host switcher is '
      'offered',
      (tester) async {
        await pumpConsole(tester, source: FakeStatusSource());

        expect(hostSwitcher, findsOneWidget);
      },
    );
  });

  group('refresh controls', () {
    testWidgets(
      'given the auto-refresh menu, when opened, then it offers 1, 5, 10, 15 '
      'and 30 minutes',
      (tester) async {
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
      },
    );

    testWidgets(
      'given the switch and the menu, when used, then each choice is saved',
      (tester) async {
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
      },
    );

    testWidgets(
      'given a loaded host, when the overview shows, then the updated time is '
      'beside Refresh now',
      (tester) async {
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
      },
    );
  });
}
