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

  group('sortSystems', () {
    test('puts the worst status first, then sorts by name', () {
      final sorted = sortSystems(<SystemStatus>[
        system('Zeta', Status.up),
        system('alpha', Status.up),
        system('Idle', Status.notDeployed),
        system('Asleep', Status.stopped),
        system('Mystery', Status.unknown),
        system('Broken', Status.down),
        system('Slow', Status.degraded),
        system('Awful', Status.down),
      ]);

      expect(sorted.map((system) => system.name), <String>[
        'Awful',
        'Broken',
        'Slow',
        'Mystery',
        'alpha',
        'Zeta',
        'Asleep',
        'Idle',
      ]);
    });

    test('sorts by the overall status, not by any one environment', () {
      // Heimdall is up overall although two of its environments are stopped.
      expect(
        sortSystems(demoStatus().systems).map((system) => system.id),
        <String>['fortuna', 'heimdall', 'yggdrasil'],
      );
      expect(
        sortSystems(v1Status().systems).map((system) => system.id),
        <String>['huginn', 'fortuna', 'yggdrasil', 'heimdall'],
      );
    });

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

  group('visibleSystems', () {
    final systems = <SystemStatus>[
      system('Fine', Status.up, <ApplicationStatus>[app('a', Status.up)]),
      system('Bad', Status.down, <ApplicationStatus>[app('b', Status.down)]),
      system('Idle', Status.notDeployed),
      system('Asleep', Status.stopped, <ApplicationStatus>[
        app('c', Status.stopped),
      ]),
    ];

    test('shows everything, sorted, without the filter', () {
      expect(
        visibleSystems(systems, problemsOnly: false).map((s) => s.name),
        <String>['Bad', 'Fine', 'Asleep', 'Idle'],
      );
    });

    test('shows only systems with a problem with the filter', () {
      expect(
        visibleSystems(systems, problemsOnly: true).map((s) => s.name),
        <String>['Bad'],
      );
    });

    test('the overall status decides, not the environments', () {
      expect(
        hasProblem(
          system('Odd', Status.unknown, <ApplicationStatus>[
            app('x', Status.up),
          ]),
        ),
        isTrue,
      );
      // Stopped environments of a system that is up: not a problem.
      expect(demoStatus().systems.where(hasProblem), isEmpty);
      expect(v1Status().systems.where(hasProblem).map((s) => s.id), <String>[
        'fortuna',
        'huginn',
        'yggdrasil',
      ]);
    });
  });

  group('applicationsSummary', () {
    test('counts applications and each non-up status', () {
      expect(
        applicationsSummary(<ApplicationStatus>[
          app('a', Status.up),
          app('b', Status.degraded),
        ]),
        '2 applications · 1 degraded',
      );
    });

    test('lists statuses worst first', () {
      expect(
        applicationsSummary(<ApplicationStatus>[
          app('a', Status.notDeployed),
          app('b', Status.degraded),
          app('e', Status.stopped),
          app('c', Status.down),
          app('d', Status.up),
        ]),
        '5 applications · 1 down · 1 degraded · 1 stopped · 1 not deployed',
      );
    });

    test('says when all are up', () {
      expect(
        applicationsSummary(<ApplicationStatus>[
          app('a', Status.up),
          app('b', Status.up),
        ]),
        '2 applications · all up',
      );
      expect(
        applicationsSummary(<ApplicationStatus>[app('a', Status.up)]),
        '1 application · up',
      );
    });

    test('says when all are stopped', () {
      expect(
        applicationsSummary(<ApplicationStatus>[
          app('a', Status.stopped),
          app('b', Status.stopped),
        ]),
        '2 applications · all stopped',
      );
    });

    test('handles no applications', () {
      expect(
        applicationsSummary(const <ApplicationStatus>[]),
        '0 applications',
      );
    });

    test('summarises a system in one environment', () {
      final heimdall = demoStatus().systems.first;

      expect(heimdall.environments.map(environmentSummary), <String>[
        '2 applications · all stopped',
        '2 applications · all stopped',
        '2 applications · all up',
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
}
