import 'package:flutter_test/flutter_test.dart';
import 'package:yggdrasil_console/features/status/domain/status.dart';
import 'package:yggdrasil_console/features/status/domain/status_logic.dart';

import '../../../helpers.dart';

ApplicationStatus app(
  String id,
  Status status, [
  ApplicationKind kind = ApplicationKind.api,
]) => ApplicationStatus(id: id, name: id, kind: kind, status: status);

SystemStatus system(
  String name,
  Status status, [
  List<ApplicationStatus> applications = const <ApplicationStatus>[],
]) => SystemStatus(
  id: name.toLowerCase(),
  name: name,
  status: status,
  environments: <SystemEnvironment>[
    SystemEnvironment(
      environment: 'production',
      status: status,
      applications: applications,
    ),
  ],
);

void main() {
  group('Status.severity', () {
    test('ranks down > degraded > unknown > up > stopped > not deployed', () {
      final sorted = Status.values.toList()
        ..sort((a, b) => b.severity.compareTo(a.severity));

      expect(sorted, <Status>[
        Status.down,
        Status.degraded,
        Status.unknown,
        Status.up,
        Status.stopped,
        Status.notDeployed,
      ]);
    });

    test('problems are down, degraded and unknown; stopped is not', () {
      expect(
        Status.values.where((status) => status.isProblem),
        unorderedEquals(<Status>[Status.down, Status.degraded, Status.unknown]),
      );
    });
  });

  group('sortApplications', () {
    test('sorts applications the same way', () {
      final sorted = sortApplications(<ApplicationStatus>[
        app('b', Status.up),
        app('a', Status.notDeployed),
        app('d', Status.stopped),
        app('c', Status.degraded),
      ]);

      expect(sorted.map((application) => application.id), <String>[
        'c',
        'b',
        'd',
        'a',
      ]);
    });
  });

  group('hostSummary', () {
    test('counts systems and environments; all is well on the demo', () {
      expect(hostSummary(demoStatus()), '3 systems · 3 environments');
    });

    test('counts the problems of a v1 host', () {
      expect(
        hostSummary(v1Status()),
        '4 systems · 1 environment · 1 down · 2 degraded',
      );
    });

    test(
      'counts a platform component once, an application per environment',
      () {
        SystemEnvironment entry(String environment, ApplicationStatus a) =>
            SystemEnvironment(
              environment: environment,
              status: a.status,
              applications: <ApplicationStatus>[a],
            );
        final host = HostStatus(
          host: 'example.com',
          generatedAt: null,
          status: Status.degraded,
          environments: const <HostEnvironment>[],
          systems: <SystemStatus>[
            SystemStatus(
              id: 'app',
              name: 'App',
              status: Status.degraded,
              environments: <SystemEnvironment>[
                entry('development', app('api', Status.degraded)),
                entry('production', app('api', Status.degraded)),
              ],
            ),
            SystemStatus(
              id: 'platform',
              name: 'Platform',
              status: Status.down,
              environments: <SystemEnvironment>[
                for (final environment in <String>['development', 'production'])
                  entry(
                    environment,
                    app('loki', Status.down, ApplicationKind.platform),
                  ),
              ],
            ),
          ],
        );

        expect(
          hostSummary(host),
          '2 systems · 0 environments · 1 down · 2 degraded',
        );
      },
    );
  });

  test('every status has a label', () {
    expect(
      <Status, String>{for (final s in Status.values) s: statusLabel(s)},
      <Status, String>{
        Status.up: 'Up',
        Status.degraded: 'Degraded',
        Status.down: 'Down',
        Status.stopped: 'Stopped',
        Status.notDeployed: 'Not deployed',
        Status.unknown: 'Unknown',
      },
    );
  });

  group('resolveHostEnvironment', () {
    const host = HostStatus(
      host: 'example.com',
      generatedAt: null,
      status: Status.up,
      environments: <HostEnvironment>[
        HostEnvironment(
          id: 'development',
          name: 'Development',
          onDemand: true,
          status: Status.stopped,
        ),
        HostEnvironment(
          id: 'production',
          name: 'Production',
          onDemand: false,
          status: Status.up,
        ),
      ],
      systems: <SystemStatus>[],
    );

    test('a remembered environment that exists is the one', () {
      expect(resolveHostEnvironment(host, 'development')?.id, 'development');
    });

    test('with nothing remembered, the last in catalog order', () {
      expect(resolveHostEnvironment(host, null)?.id, 'production');
    });

    test('a remembered environment that is gone falls back to the last', () {
      expect(resolveHostEnvironment(host, 'staging')?.id, 'production');
    });

    test('a host with no environments resolves to none', () {
      final empty = const HostStatus(
        host: 'example.com',
        generatedAt: null,
        status: Status.up,
        environments: <HostEnvironment>[],
        systems: <SystemStatus>[],
      );

      expect(resolveHostEnvironment(empty, 'production'), isNull);
    });
  });

  group('environmentSummaryLine', () {
    IntegrationStatus link(String from, IntegrationHealth status) =>
        IntegrationStatus(from: from, check: 'c', status: status);

    HostStatus hostWith(Map<String, List<IntegrationStatus>> byEnvironment) =>
        HostStatus(
          host: 'example.com',
          generatedAt: null,
          status: Status.up,
          environments: const <HostEnvironment>[],
          systems: <SystemStatus>[
            SystemStatus(
              id: 'heimdall',
              name: 'Heimdall',
              status: Status.up,
              environments: <SystemEnvironment>[
                for (final entry in byEnvironment.entries)
                  SystemEnvironment(
                    environment: entry.key,
                    status: Status.up,
                    applications: const <ApplicationStatus>[],
                    integrations: entry.value,
                  ),
              ],
            ),
          ],
        );

    test('summary counts failing integrations in that environment only', () {
      final host = hostWith(<String, List<IntegrationStatus>>{
        'production': <IntegrationStatus>[
          link('a', IntegrationHealth.down),
          link('b', IntegrationHealth.up),
        ],
        'development': <IntegrationStatus>[
          link('a', IntegrationHealth.down),
          link('b', IntegrationHealth.down),
        ],
      });

      expect(
        environmentSummaryLine(host, 'production'),
        '1 integration failing',
      );
      expect(
        environmentSummaryLine(host, 'development'),
        '2 integrations failing',
      );
    });

    test('summary ignores stopped and not-configured integrations', () {
      final host = hostWith(<String, List<IntegrationStatus>>{
        'production': <IntegrationStatus>[
          link('a', IntegrationHealth.stopped),
          link('b', IntegrationHealth.notConfigured),
          link('c', IntegrationHealth.unknown),
        ],
      });

      expect(environmentSummaryLine(host, 'production'), isEmpty);
    });

    test('says nothing when there are no integrations', () {
      expect(environmentSummaryLine(hostWith(const {}), 'production'), isEmpty);
    });
  });
}
