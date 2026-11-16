import 'package:flutter_test/flutter_test.dart';
import 'package:yggdrasil_console/features/status/domain/status.dart';
import 'package:yggdrasil_console/features/status/domain/status_logic.dart';

import '../../../helpers.dart';

ApplicationStatus app(
  String id,
  Status status, [
  ApplicationKind kind = ApplicationKind.api,
  String? name,
]) => ApplicationStatus(id: id, name: name ?? id, kind: kind, status: status);

HostStatus hostOf({
  List<HostEnvironment> environments = const <HostEnvironment>[],
  List<SystemStatus> systems = const <SystemStatus>[],
}) => HostStatus(
  host: 'example.com',
  generatedAt: null,
  status: Status.up,
  environments: environments,
  systems: systems,
);

void main() {
  group('sortApplications', () {
    test('given applications of mixed statuses, when sorted, then the worst '
        'come first and stopped and not deployed last', () {
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

    test('given applications of the same status, when sorted, then by name '
        'ignoring case, then by id', () {
      final sorted = sortApplications(<ApplicationStatus>[
        app('3', Status.up, ApplicationKind.api, 'beta'),
        app('2', Status.up, ApplicationKind.api, 'Alpha'),
        app('1', Status.up, ApplicationKind.api, 'beta'),
      ]);

      expect(sorted.map((application) => application.id), <String>[
        '2',
        '1',
        '3',
      ]);
    });

    test('given applications, when sorted, then a new list is returned and '
        'the input is untouched', () {
      final input = <ApplicationStatus>[
        app('b', Status.up),
        app('a', Status.down),
      ];

      final sorted = sortApplications(input);

      expect(input.map((a) => a.id), <String>['b', 'a']);
      expect(sorted.map((a) => a.id), <String>['a', 'b']);
      expect(sortApplications(const <ApplicationStatus>[]), isEmpty);
    });
  });

  group('countByStatus', () {
    test('given applications, when counted, then each status present has its '
        'count, in severity order', () {
      final counts = countByStatus(<ApplicationStatus>[
        app('a', Status.up),
        app('b', Status.stopped),
        app('c', Status.down),
        app('d', Status.up),
        app('e', Status.degraded),
      ]);

      expect(counts.entries.map((e) => (e.key, e.value)), <(Status, int)>[
        (Status.down, 1),
        (Status.degraded, 1),
        (Status.up, 2),
        (Status.stopped, 1),
      ]);
    });

    test('given no applications, when counted, then there are no counts', () {
      expect(countByStatus(const <ApplicationStatus>[]), isEmpty);
    });
  });

  group('hostSummary', () {
    test('given the demo, when summarised, then it counts systems, '
        'environments and the degraded APIs', () {
      expect(
        hostSummary(demoStatus()),
        '4 systems · 3 environments · 2 degraded',
      );
    });

    test('given a v1 host, when summarised, then its problems are counted', () {
      expect(
        hostSummary(v1Status()),
        '4 systems · 1 environment · 1 down · 2 degraded',
      );
    });

    test('given a platform component in two environments and an application '
        'in two, when summarised, then the component counts once and the '
        'application per environment', () {
      SystemEnvironment entry(String environment, ApplicationStatus a) =>
          SystemEnvironment(
            environment: environment,
            status: a.status,
            applications: <ApplicationStatus>[a],
          );
      final host = hostOf(
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
    });

    test('given one system in one environment with nothing wrong, when '
        'summarised, then the words are singular and no problem is listed', () {
      final host = hostOf(
        environments: const <HostEnvironment>[
          HostEnvironment(
            id: 'production',
            name: 'Production',
            onDemand: false,
            status: Status.up,
          ),
        ],
        systems: <SystemStatus>[
          SystemStatus(
            id: 'a',
            name: 'A',
            status: Status.up,
            environments: <SystemEnvironment>[
              SystemEnvironment(
                environment: 'production',
                status: Status.up,
                applications: <ApplicationStatus>[
                  app('up', Status.up),
                  app('off', Status.stopped),
                  app('gone', Status.notDeployed),
                ],
              ),
            ],
          ),
        ],
      );

      expect(hostSummary(host), '1 system · 1 environment');
    });

    test('given an application of unknown status, when summarised, then it '
        'counts as a problem', () {
      final host = hostOf(
        systems: <SystemStatus>[
          SystemStatus(
            id: 'a',
            name: 'A',
            status: Status.unknown,
            environments: <SystemEnvironment>[
              SystemEnvironment(
                environment: 'production',
                status: Status.unknown,
                applications: <ApplicationStatus>[app('x', Status.unknown)],
              ),
            ],
          ),
        ],
      );

      expect(hostSummary(host), '1 system · 0 environments · 1 unknown');
    });

    test('given an empty host, when summarised, then it has zero systems and '
        'environments', () {
      expect(hostSummary(hostOf()), '0 systems · 0 environments');
    });
  });

  group('statusLabel', () {
    test('given every status, when labelled, then each has its own label', () {
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
  });

  group('resolveHostEnvironment', () {
    final host = hostOf(
      environments: const <HostEnvironment>[
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
    );

    test('given a remembered environment the host has, when resolved, then '
        'it is that one', () {
      expect(resolveHostEnvironment(host, 'development')?.id, 'development');
    });

    test('given nothing remembered, when resolved, then it is the last in '
        'catalog order', () {
      expect(resolveHostEnvironment(host, null)?.id, 'production');
    });

    test('given a remembered environment that is gone, when resolved, then '
        'it falls back to the last', () {
      expect(resolveHostEnvironment(host, 'staging')?.id, 'production');
    });

    test('given a host with no environments, when resolved, then there is '
        'none', () {
      expect(resolveHostEnvironment(hostOf(), 'production'), isNull);
      expect(resolveHostEnvironment(hostOf(), null), isNull);
    });
  });

  group('environmentSummaryLine', () {
    IntegrationStatus link(String from, IntegrationHealth status) =>
        IntegrationStatus(from: from, check: 'c', status: status);

    HostStatus hostWith(Map<String, List<IntegrationStatus>> byEnvironment) =>
        hostOf(
          systems: <SystemStatus>[
            for (final id in <String>['heimdall', 'fortuna'])
              SystemStatus(
                id: id,
                name: id,
                status: Status.up,
                environments: <SystemEnvironment>[
                  for (final entry in byEnvironment.entries)
                    SystemEnvironment(
                      environment: entry.key,
                      status: Status.up,
                      applications: const <ApplicationStatus>[],
                      integrations: id == 'heimdall'
                          ? entry.value
                          : const <IntegrationStatus>[],
                    ),
                ],
              ),
          ],
        );

    test('given failing integrations in two environments, when summarised, '
        'then only those of that environment count', () {
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

    test('given failing integrations in several systems, when summarised, '
        'then they all count', () {
      final host = hostOf(
        systems: <SystemStatus>[
          for (final id in <String>['a', 'b', 'c'])
            SystemStatus(
              id: id,
              name: id,
              status: Status.up,
              environments: <SystemEnvironment>[
                SystemEnvironment(
                  environment: 'production',
                  status: Status.up,
                  applications: const <ApplicationStatus>[],
                  integrations: <IntegrationStatus>[
                    link('$id-api', IntegrationHealth.down),
                  ],
                ),
              ],
            ),
        ],
      );

      expect(
        environmentSummaryLine(host, 'production'),
        '3 integrations failing',
      );
    });

    test('given stopped, not-configured and unknown integrations, when '
        'summarised, then none counts as failing', () {
      final host = hostWith(<String, List<IntegrationStatus>>{
        'production': <IntegrationStatus>[
          link('a', IntegrationHealth.stopped),
          link('b', IntegrationHealth.notConfigured),
          link('c', IntegrationHealth.unknown),
        ],
      });

      expect(environmentSummaryLine(host, 'production'), isEmpty);
    });

    test('given no integrations or an environment no system is in, when '
        'summarised, then it says nothing', () {
      expect(environmentSummaryLine(hostWith(const {}), 'production'), isEmpty);
      expect(
        environmentSummaryLine(
          hostWith(<String, List<IntegrationStatus>>{
            'production': <IntegrationStatus>[
              link('a', IntegrationHealth.down),
            ],
          }),
          'staging',
        ),
        isEmpty,
      );
    });
  });
}
