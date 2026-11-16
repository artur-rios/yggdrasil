import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:material_ui/material_ui.dart';
import 'package:yggdrasil_console/features/status/domain/status.dart';
import 'package:yggdrasil_console/features/status/presentation/widgets/application_detail.dart';
import 'package:yggdrasil_console/features/status/presentation/widgets/system_panel.dart';

import '../../../../helpers.dart';

Finder tile(String id) => find.byKey(ValueKey<String>('map-system-$id'));

final Finder panel = find.byKey(const ValueKey<String>('system-panel'));

Finder inPanel(String text) =>
    find.descendant(of: panel, matching: find.text(text));

/// An ISO timestamp [age] before now. The panel reads `DateTime.now()`, so
/// ages sit mid-unit (3.5 min reads "3 min") and a slow run cannot cross into
/// the next one.
String ago(Duration age) =>
    DateTime.now().toUtc().subtract(age).toIso8601String();

Map<String, dynamic> api(Map<String, dynamic> production) =>
    (production['applications'] as List<dynamic>)
        .cast<Map<String, dynamic>>()
        .firstWhere((application) => application['id'] == 'heimdall-api');

/// The demo with [edit] applied to Heimdall in production.
HostStatus heimdallWith(void Function(Map<String, dynamic> production) edit) {
  final json = jsonDecode(demoJson()) as Map<String, dynamic>;
  final systems = (json['systems'] as List<dynamic>)
      .cast<Map<String, dynamic>>();
  final heimdall = systems.firstWhere((system) => system['id'] == 'heimdall');
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

/// Opens Heimdall's panel from the map of the whole console.
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

Map<String, dynamic> reported(List<Map<String, dynamic>> services) =>
    <String, dynamic>{
      'state': 'reported',
      'checkedAt': ago(const Duration(minutes: 1, seconds: 30)),
      'status': 'Healthy',
      'services': services,
    };

/// Pumps the panel alone, for Heimdall's production in [host].
Future<void> pumpPanel(WidgetTester tester, HostStatus host) {
  final heimdall = host.systems.firstWhere((system) => system.id == 'heimdall');

  return tester.pumpWidget(
    MaterialApp(
      home: Scaffold(
        body: SystemPanel(
          system: heimdall,
          environment: heimdall.environments.firstWhere(
            (entry) => entry.environment == 'production',
          ),
          host: host,
        ),
      ),
    ),
  );
}

ApplicationStatus apiWith({
  Deployment? deployment,
  ContainerInfo? container,
  Probe? probe,
}) => ApplicationStatus(
  id: 'heimdall-api',
  name: 'Heimdall API',
  kind: ApplicationKind.api,
  status: Status.up,
  deployment: deployment,
  container: container,
  probe: probe,
);

/// Pumps one application row.
Future<void> pumpRow(WidgetTester tester, ApplicationStatus application) =>
    tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: ApplicationRow(
            system: const SystemStatus(
              id: 'heimdall',
              name: 'Heimdall',
              status: Status.up,
              environments: <SystemEnvironment>[],
            ),
            application: application,
            environmentId: 'production',
          ),
        ),
      ),
    );

void main() {
  group('the panel in the console', () {
    testWidgets('given a system, when its panel opens, then it lists the '
        'applications with status, version and deploy time', (tester) async {
      await openHeimdall(tester, heimdallWith((_) {}));

      expect(panel, findsOneWidget);
      expect(inPanel('Heimdall'), findsOneWidget);
      expect(inPanel('Identity and access management'), findsOneWidget);
      expect(inPanel('Production'), findsOneWidget);
      expect(find.text('Heimdall API'), findsOneWidget);
      expect(find.text('Heimdall web'), findsOneWidget);
      expect(find.text('v1.4.0 · 3f2a9c1'), findsOneWidget);
      expect(find.textContaining('deployed '), findsWidgets);
      expect(find.text('Up'), findsWidgets);
    });

    testWidgets('given the panel, when an application row is tapped, then the '
        'application detail opens', (tester) async {
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

    testWidgets('given a reported report, when the panel opens, then the '
        'services show status, reason and the checked time', (tester) async {
      await openHeimdall(
        tester,
        heimdallWith((production) {
          api(production)['details'] = <String, dynamic>{
            'state': 'reported',
            'checkedAt': ago(const Duration(minutes: 3, seconds: 30)),
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

    testWidgets('given an unavailable report, when the panel opens, then it '
        'says it could not get more details, with the reason', (tester) async {
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

    testWidgets('given an unavailable report with integrations, when the '
        'panel opens, then they read Not checked', (tester) async {
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

    testWidgets('given an unsupported report, when the panel opens, then it '
        'says this version does not report detailed health', (tester) async {
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

    testWidgets('given a report not checked yet, when the panel opens, then '
        'it says so, without a time', (tester) async {
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

    testWidgets('given no detailed health, when the panel opens, then there '
        'is no Reported by section', (tester) async {
      await openHeimdall(tester, heimdallWith((_) {}));

      expect(find.textContaining('Reported by'), findsNothing);
    });

    testWidgets('given a stopped dependency the report calls unhealthy, when '
        'the panel opens, then it reads stopped, not failing', (tester) async {
      await openHeimdall(
        tester,
        heimdallWith((production) {
          production['integrations'] = <Map<String, dynamic>>[
            hermesLink(status: 'stopped', reason: 'not running'),
          ];
          api(production)['details'] = reported(<Map<String, dynamic>>[
            <String, dynamic>{
              'name': 'Hermes',
              'status': 'Unhealthy',
              'reason': 'connection refused',
            },
          ]);
        }),
      );

      expect(inPanel('Stopped'), findsOneWidget);
      expect(inPanel('Unhealthy'), findsNothing);
      expect(inPanel('connection refused'), findsNothing);
      expect(inPanel('not running'), findsNothing);
    });

    testWidgets('given a failing dependency, when the panel opens, then it '
        'shows its reason', (tester) async {
      await openHeimdall(
        tester,
        heimdallWith((production) {
          production['integrations'] = <Map<String, dynamic>>[
            hermesLink(status: 'down', reason: 'connection refused'),
          ];
          api(production)['details'] = reported(<Map<String, dynamic>>[
            <String, dynamic>{'name': 'Hermes', 'status': 'Unhealthy'},
          ]);
        }),
      );

      expect(inPanel('Failing'), findsOneWidget);
      expect(inPanel('connection refused'), findsOneWidget);
    });

    testWidgets('given an external target, when the panel opens, then it is '
        'named as written with the external icon', (tester) async {
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
          api(production)['details'] = reported(<Map<String, dynamic>>[
            <String, dynamic>{'name': 'Mail', 'status': 'Healthy'},
          ]);
        }),
      );

      expect(inPanel('Mailgun'), findsOneWidget);
      expect(
        find.descendant(of: panel, matching: find.byIcon(Icons.cloud)),
        findsOneWidget,
      );
    });

    testWidgets('given an on-demand system, when the panel opens, then it '
        'says on demand', (tester) async {
      await openHeimdall(
        tester,
        heimdallWith((production) => production['onDemand'] = true),
      );

      expect(inPanel('on demand'), findsOneWidget);
    });

    testWidgets('given an always-on system, when the panel opens, then it '
        'does not say on demand, and Close closes it', (tester) async {
      await openHeimdall(tester, heimdallWith((_) {}));

      expect(inPanel('on demand'), findsNothing);

      await tester.tap(find.byTooltip('Close'));
      await settle(tester);

      expect(panel, findsNothing);
    });

    testWidgets('given a phone, when a system is opened, then the panel is a '
        'bottom sheet', (tester) async {
      await pumpConsole(
        tester,
        source: FakeStatusSource((_, _) async => heimdallWith((_) {})),
        size: const Size(390, 900),
      );
      await tester.tap(tile('heimdall'));
      await settle(tester);

      expect(find.byType(BottomSheet), findsOneWidget);
      expect(panel, findsOneWidget);
      expect(find.byTooltip('Close'), findsNothing);
    });

    testWidgets('given a computer, when a system is opened, then the panel is '
        'at the side with the map still visible', (tester) async {
      await openHeimdall(tester, heimdallWith((_) {}));

      expect(find.byType(BottomSheet), findsNothing);
      expect(tester.getSize(panel).width, 420);
      expect(tester.getRect(panel).right, 1000);
      expect(tile('fortuna'), findsOneWidget);
      expect(tester.getRect(tile('fortuna')).right, lessThan(580));
    });
  });

  group('SystemPanel', () {
    testWidgets('given no applications in the environment, when shown, then '
        'it says so', (tester) async {
      await pumpPanel(
        tester,
        heimdallWith(
          (production) => production['applications'] = <Map<String, dynamic>>[],
        ),
      );

      expect(find.text('No applications in Production.'), findsOneWidget);
    });

    testWidgets('given a report listing nothing, when shown, then it says so', (
      tester,
    ) async {
      await pumpPanel(
        tester,
        heimdallWith(
          (production) => api(production)['details'] = reported(
            const <Map<String, dynamic>>[],
          ),
        ),
      );

      expect(find.text('The report lists nothing.'), findsOneWidget);
    });

    testWidgets('given an unavailable report without a reason, when shown, '
        'then it says no reason was given', (tester) async {
      await pumpPanel(
        tester,
        heimdallWith(
          (production) => api(production)['details'] = <String, dynamic>{
            'state': 'unavailable',
          },
        ),
      );

      expect(
        find.text(
          'Could not get more details. The detailed health endpoint did not '
          'answer: no reason given.',
        ),
        findsOneWidget,
      );
    });

    testWidgets("given services with the report's own words, when shown, "
        'then NotConfigured and an empty word are spelled out and others '
        'kept', (tester) async {
      await pumpPanel(
        tester,
        heimdallWith(
          (production) =>
              api(production)['details'] = reported(<Map<String, dynamic>>[
                <String, dynamic>{'name': 'Smtp', 'status': 'NotConfigured'},
                <String, dynamic>{'name': 'Cache'},
                <String, dynamic>{'name': 'Queue', 'status': 'Sleepy'},
              ]),
        ),
      );

      expect(inPanel('Not configured'), findsOneWidget);
      expect(inPanel('Unknown'), findsOneWidget);
      expect(inPanel('Sleepy'), findsOneWidget);
    });

    testWidgets('given an integration to a system the host does not have, '
        'when shown, then the target is named by its id', (tester) async {
      await pumpPanel(
        tester,
        heimdallWith((production) {
          production['integrations'] = <Map<String, dynamic>>[
            <String, dynamic>{
              'from': 'heimdall-api',
              'system': 'odin',
              'check': 'Odin',
              'status': 'up',
            },
          ];
          api(production)['details'] = reported(<Map<String, dynamic>>[
            <String, dynamic>{'name': 'Odin', 'status': 'Healthy'},
          ]);
        }),
      );

      expect(inPanel('odin'), findsOneWidget);
      expect(inPanel('OK'), findsOneWidget);
    });

    testWidgets('given an integration of another application, when shown, '
        "then it is not part of this application's report", (tester) async {
      await pumpPanel(
        tester,
        heimdallWith((production) {
          production['integrations'] = <Map<String, dynamic>>[
            <String, dynamic>{
              'from': 'heimdall-ui',
              'system': 'hermes',
              'check': 'Hermes',
              'status': 'down',
              'reason': 'connection refused',
            },
          ];
          api(production)['details'] = reported(<Map<String, dynamic>>[
            <String, dynamic>{'name': 'Hermes', 'status': 'Healthy'},
          ]);
        }),
      );

      expect(inPanel('Failing'), findsNothing);
      expect(inPanel('Healthy'), findsOneWidget);
    });
  });

  group('ApplicationRow', () {
    testWidgets('given no container, when shown, then it says so', (
      tester,
    ) async {
      await pumpRow(tester, apiWith());

      expect(find.text('no container'), findsOneWidget);
    });

    testWidgets('given a container with one restart and no state, when '
        'shown, then it reads unknown state and 1 restart', (tester) async {
      await pumpRow(
        tester,
        apiWith(
          container: const ContainerInfo(health: 'healthy', restartCount: 1),
        ),
      );

      expect(find.text('unknown state · healthy · 1 restart'), findsOneWidget);
    });

    testWidgets('given a container with several restarts, when shown, then '
        'the word is plural', (tester) async {
      await pumpRow(
        tester,
        apiWith(
          container: const ContainerInfo(state: 'running', restartCount: 3),
        ),
      );

      expect(find.text('running · 3 restarts'), findsOneWidget);
    });

    testWidgets('given only a commit, when shown, then the version line is '
        'the commit', (tester) async {
      await pumpRow(
        tester,
        apiWith(deployment: const Deployment(commit: '3f2a9c1')),
      );

      expect(find.text('3f2a9c1'), findsOneWidget);
    });

    testWidgets('given a probe, when shown, then it is described with its '
        'HTTP code in the tooltip', (tester) async {
      await pumpRow(
        tester,
        apiWith(
          probe: const Probe(healthy: false, statusCode: 503, latencyMs: 8),
        ),
      );

      expect(find.text('HTTP 503 · 8 ms'), findsOneWidget);
      expect(find.byTooltip('Health probe: HTTP 503'), findsOneWidget);
    });

    testWidgets('given a probe with no answer, when shown, then the tooltip '
        'has no HTTP code', (tester) async {
      await pumpRow(
        tester,
        apiWith(probe: const Probe(healthy: false, error: 'timeout')),
      );

      expect(find.text('timeout'), findsOneWidget);
      expect(find.byTooltip('Health probe'), findsOneWidget);
    });
  });
}
