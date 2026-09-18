import 'package:flutter_test/flutter_test.dart';
import 'package:yggdrasil_console/features/status/domain/status.dart';
import 'package:yggdrasil_console/features/status/domain/status_logic.dart';

import '../../../helpers.dart';

ApplicationStatus app(String id, Status status) => ApplicationStatus(
  id: id,
  name: id,
  kind: ApplicationKind.api,
  status: status,
);

SystemStatus system(
  String name,
  Status status, [
  List<ApplicationStatus> applications = const <ApplicationStatus>[],
]) => SystemStatus(
  id: name.toLowerCase(),
  name: name,
  status: status,
  applications: applications,
);

void main() {
  group('Status.severity', () {
    test('ranks down > degraded > unknown > up > not deployed', () {
      final sorted = Status.values.toList()
        ..sort((a, b) => b.severity.compareTo(a.severity));

      expect(sorted, <Status>[
        Status.down,
        Status.degraded,
        Status.unknown,
        Status.up,
        Status.notDeployed,
      ]);
    });

    test('problems are down, degraded and unknown', () {
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
        'Idle',
      ]);
    });

    test('orders the demo fixture worst first', () {
      expect(
        sortSystems(demoStatus().systems).map((system) => system.id),
        <String>['huginn', 'fortuna', 'yggdrasil', 'heimdall'],
      );
    });

    test('sorts applications the same way', () {
      final sorted = sortApplications(<ApplicationStatus>[
        app('b', Status.up),
        app('a', Status.notDeployed),
        app('c', Status.degraded),
      ]);

      expect(sorted.map((application) => application.id), <String>[
        'c',
        'b',
        'a',
      ]);
    });
  });

  group('visibleSystems', () {
    final systems = <SystemStatus>[
      system('Fine', Status.up, <ApplicationStatus>[app('a', Status.up)]),
      system('Bad', Status.down, <ApplicationStatus>[app('b', Status.down)]),
      system('Idle', Status.notDeployed),
    ];

    test('shows everything, sorted, without the filter', () {
      expect(
        visibleSystems(systems, problemsOnly: false).map((s) => s.name),
        <String>['Bad', 'Fine', 'Idle'],
      );
    });

    test('shows only systems with a problem with the filter', () {
      expect(
        visibleSystems(systems, problemsOnly: true).map((s) => s.name),
        <String>['Bad'],
      );
    });

    test('a system with a problem application counts as a problem', () {
      expect(
        hasProblem(
          system('Odd', Status.up, <ApplicationStatus>[
            app('x', Status.unknown),
          ]),
        ),
        isTrue,
      );
    });
  });

  group('systemSummary', () {
    test('counts applications and each non-up status', () {
      expect(
        systemSummary(
          system('S', Status.degraded, <ApplicationStatus>[
            app('a', Status.up),
            app('b', Status.degraded),
          ]),
        ),
        '2 applications · 1 degraded',
      );
    });

    test('lists statuses worst first', () {
      expect(
        systemSummary(
          system('S', Status.down, <ApplicationStatus>[
            app('a', Status.notDeployed),
            app('b', Status.degraded),
            app('c', Status.down),
            app('d', Status.up),
          ]),
        ),
        '4 applications · 1 down · 1 degraded · 1 not deployed',
      );
    });

    test('says when all are up', () {
      expect(
        systemSummary(
          system('S', Status.up, <ApplicationStatus>[
            app('a', Status.up),
            app('b', Status.up),
          ]),
        ),
        '2 applications · all up',
      );
      expect(
        systemSummary(
          system('S', Status.up, <ApplicationStatus>[app('a', Status.up)]),
        ),
        '1 application · up',
      );
    });

    test('handles a system with no applications', () {
      expect(systemSummary(system('S', Status.unknown)), '0 applications');
    });

    test('summarises the demo environment', () {
      expect(
        environmentSummary(demoStatus()),
        '4 systems · 13 applications · 1 down · 2 degraded · 1 not deployed',
      );
    });
  });

  test('every status has a label', () {
    expect(Status.values.map(statusLabel), <String>[
      'Up',
      'Degraded',
      'Down',
      'Not deployed',
      'Unknown',
    ]);
  });
}
