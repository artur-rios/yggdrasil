import 'package:flutter_test/flutter_test.dart';
import 'package:material_ui/material_ui.dart';
import 'package:yggdrasil_console/features/status/domain/status.dart';
import 'package:yggdrasil_console/features/status/presentation/widgets/application_detail.dart';

import '../../../../helpers.dart';

const SystemStatus heimdall = SystemStatus(
  id: 'heimdall',
  name: 'Heimdall',
  status: Status.up,
  environments: <SystemEnvironment>[],
);

ApplicationStatus application({
  Status status = Status.up,
  ApplicationKind kind = ApplicationKind.api,
  String? kindWire,
  String? url,
  String? repository,
  Deployment? deployment,
  ContainerInfo? container,
  Probe? probe,
}) => ApplicationStatus(
  id: 'heimdall-api',
  name: 'Heimdall API',
  kind: kind,
  kindWire: kindWire,
  status: status,
  url: url,
  repository: repository,
  deployment: deployment,
  container: container,
  probe: probe,
);

Future<void> pumpDetail(
  WidgetTester tester,
  ApplicationStatus application, {
  String? environmentName,
}) {
  // Tall enough for the list to build every section.
  tester.view.physicalSize = const Size(800, 2000);
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.reset);

  return tester.pumpWidget(
    MaterialApp(
      home: Scaffold(
        body: ApplicationDetail(
          system: heimdall,
          application: application,
          environmentName: environmentName,
        ),
      ),
    ),
  );
}

/// The value shown beside [label].
String valueOf(WidgetTester tester, String label) {
  final row = find
      .ancestor(of: find.text(label), matching: find.byType(Row))
      .first;

  return tester
      .widget<SelectableText>(
        find.descendant(of: row, matching: find.byType(SelectableText)),
      )
      .data!;
}

void main() {
  group('describeProbe', () {
    test('given a healthy 2xx probe, when described, then only its latency '
        'shows', () {
      expect(
        describeProbe(
          const Probe(healthy: true, statusCode: 200, latencyMs: 12),
        ),
        '12 ms',
      );
    });

    test('given a failing HTTP answer, when described, then its status code '
        'shows', () {
      expect(
        describeProbe(
          const Probe(healthy: false, statusCode: 503, latencyMs: 8),
        ),
        'HTTP 503 · 8 ms',
      );
      expect(
        describeProbe(
          const Probe(healthy: false, statusCode: 302, latencyMs: 3),
        ),
        'HTTP 302 · 3 ms',
      );
      expect(
        describeProbe(const Probe(healthy: false, statusCode: 101)),
        'HTTP 101',
      );
    });

    test(
      'given a slow 2xx, when described, then the latency is in seconds',
      () {
        expect(
          describeProbe(
            const Probe(healthy: false, statusCode: 200, latencyMs: 2380),
          ),
          '2.4 s',
        );
      },
    );

    test('given no answer, when described, then the error shows', () {
      expect(
        describeProbe(const Probe(healthy: false, error: 'connection refused')),
        'connection refused',
      );
      expect(
        describeProbe(
          const Probe(healthy: false, latencyMs: 5000, error: 'timeout'),
        ),
        '5.0 s · timeout',
      );
    });

    test('given nothing else known, when described, then it falls back to '
        'healthy or unhealthy', () {
      expect(describeProbe(const Probe(healthy: true)), 'healthy');
      expect(describeProbe(const Probe(healthy: false)), 'unhealthy');
      expect(describeProbe(const Probe()), 'unhealthy');
    });

    test('given only a 2xx status code, when described, then it is empty', () {
      expect(describeProbe(const Probe(healthy: true, statusCode: 204)), '');
    });
  });

  group('ApplicationDetail', () {
    testWidgets('given every field, when shown, then each section lists them', (
      tester,
    ) async {
      await pumpDetail(
        tester,
        application(
          url: 'https://heimdall-api.example.com',
          repository: 'https://github.com/artur-rios/heimdall-api',
          deployment: const Deployment(
            version: '1.4.0',
            commit: '3f2a9c1',
            image: 'heimdall-api:1.4.0',
          ),
          container: const ContainerInfo(
            state: 'running',
            health: 'healthy',
            restartCount: 2,
          ),
          probe: const Probe(healthy: true, statusCode: 200, latencyMs: 12),
        ),
        environmentName: 'Production',
      );

      expect(find.text('Heimdall · Production · heimdall-api'), findsOneWidget);
      expect(valueOf(tester, 'Environment'), 'Production');
      expect(valueOf(tester, 'Kind'), 'API');
      expect(valueOf(tester, 'Status'), 'Up');
      expect(valueOf(tester, 'URL'), 'https://heimdall-api.example.com');
      expect(valueOf(tester, 'Version'), '1.4.0');
      expect(valueOf(tester, 'Image'), 'heimdall-api:1.4.0');
      expect(valueOf(tester, 'State'), 'running');
      expect(valueOf(tester, 'Health'), 'healthy');
      expect(valueOf(tester, 'Restarts'), '2');
      expect(valueOf(tester, 'Healthy'), 'yes');
      expect(valueOf(tester, 'HTTP status'), '200');
      expect(valueOf(tester, 'Latency'), '12 ms');
      expect(valueOf(tester, 'Error'), '—');
      expect(find.text('Open'), findsOneWidget);
      expect(find.text('Repository'), findsNWidgets(2));
    });

    testWidgets('given no environment name, when shown, then there is no '
        'environment row and the subtitle skips it', (tester) async {
      await pumpDetail(tester, application());

      expect(find.text('Heimdall · heimdall-api'), findsOneWidget);
      expect(find.text('Environment'), findsNothing);
    });

    testWidgets('given no url and no repository, when shown, then there are '
        'no link buttons and the rows read as a dash', (tester) async {
      await pumpDetail(tester, application());

      expect(find.text('Open'), findsNothing);
      expect(valueOf(tester, 'URL'), '—');
      expect(valueOf(tester, 'Repository'), '—');
    });

    testWidgets('given an unknown kind, when shown, then the kind is as '
        'written', (tester) async {
      await pumpDetail(
        tester,
        application(kind: ApplicationKind.other, kindWire: 'cron'),
      );

      expect(valueOf(tester, 'Kind'), 'cron');
    });

    testWidgets('given no container, when shown, then deployment and '
        'container say so', (tester) async {
      await pumpDetail(tester, application());

      expect(find.text('No container on this host.'), findsNWidgets(2));
    });

    testWidgets('given a container without a health check, when shown, then '
        'it says so', (tester) async {
      await pumpDetail(
        tester,
        application(container: const ContainerInfo(state: 'running')),
      );

      expect(valueOf(tester, 'Health'), 'no health check');
      expect(valueOf(tester, 'Restarts'), '—');
    });

    testWidgets('given a deployedAt that is not a date, when shown, then it '
        'is shown as written', (tester) async {
      await pumpDetail(
        tester,
        application(
          deployment: const Deployment(deployedAtText: 'last tuesday'),
        ),
      );

      expect(valueOf(tester, 'Deployed'), 'last tuesday');
    });

    testWidgets('given a deployment date, when shown, then it reads relative '
        'and absolute', (tester) async {
      final deployedAt = DateTime.now().toUtc().subtract(
        const Duration(days: 3, hours: 12),
      );

      await pumpDetail(
        tester,
        application(deployment: Deployment(deployedAt: deployedAt)),
      );

      expect(valueOf(tester, 'Deployed'), startsWith('3 d ago · '));
      expect(valueOf(tester, 'Deployed'), contains('(UTC'));
    });

    for (final (status, text) in <(Status, String)>[
      (Status.notDeployed, 'Not probed: not deployed on this host.'),
      (Status.stopped, 'Not probed: stopped (an on-demand environment).'),
      (Status.down, 'No probe result.'),
    ]) {
      testWidgets('given no probe for a ${status.wire} application, when '
          'shown, then the probe section says why', (tester) async {
        await pumpDetail(tester, application(status: status));

        expect(find.text(text), findsOneWidget);
      });
    }

    testWidgets('given a probe with no answer, when shown, then the error '
        'shows and unknown fields read as a dash', (tester) async {
      await pumpDetail(
        tester,
        application(
          probe: const Probe(healthy: false, error: 'connection refused'),
        ),
      );

      expect(valueOf(tester, 'Healthy'), 'no');
      expect(valueOf(tester, 'HTTP status'), '—');
      expect(valueOf(tester, 'Latency'), '—');
      expect(valueOf(tester, 'Error'), 'connection refused');
    });

    testWidgets('given a probe whose health is unknown, when shown, then it '
        'reads as a dash', (tester) async {
      await pumpDetail(tester, application(probe: const Probe()));

      expect(valueOf(tester, 'Healthy'), '—');
    });
  });

  group('showApplicationDetail', () {
    Future<void> open(WidgetTester tester, Size size) async {
      tester.view.physicalSize = size;
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.reset);

      await tester.pumpWidget(
        MaterialApp(
          home: Builder(
            builder: (context) => Scaffold(
              body: TextButton(
                onPressed: () => showApplicationDetail(
                  context,
                  system: heimdall,
                  application: application(),
                ),
                child: const Text('open'),
              ),
            ),
          ),
        ),
      );
      await tester.tap(find.text('open'));
      await settle(tester);
    }

    testWidgets('given a phone, when opened, then it is a bottom sheet '
        'without a close button', (tester) async {
      await open(tester, const Size(390, 800));

      expect(find.byType(BottomSheet), findsOneWidget);
      expect(find.byType(ApplicationDetail), findsOneWidget);
      expect(find.byTooltip('Close'), findsNothing);
    });

    testWidgets('given a wider screen, when opened, then it is a dialog that '
        'Close dismisses', (tester) async {
      await open(tester, const Size(1000, 800));

      expect(find.byType(Dialog), findsOneWidget);
      expect(find.byType(BottomSheet), findsNothing);

      await tester.tap(find.byTooltip('Close'));
      await settle(tester);

      expect(find.byType(ApplicationDetail), findsNothing);
    });
  });
}
