import 'package:flutter_test/flutter_test.dart';
import 'package:yggdrasil_console/features/status/domain/status.dart';
import 'package:yggdrasil_console/features/status/presentation/layout.dart';
import 'package:yggdrasil_console/features/status/presentation/map/map_layout.dart';

const SystemNode heimdall = SystemNode('heimdall');
const SystemNode hermes = SystemNode('hermes');
const SystemNode fortuna = SystemNode('fortuna');
const ExternalNode mailgun = ExternalNode('Mailgun');

IntegrationStatus toSystem(String from, String system) => IntegrationStatus(
  from: from,
  system: system,
  check: system,
  status: IntegrationHealth.up,
);

IntegrationStatus toExternal(String from, String name) => IntegrationStatus(
  from: from,
  external: name,
  check: name,
  status: IntegrationHealth.up,
);

/// A system with one application, `<id>-api`, whose integrations are given
/// per environment.
SystemStatus system(
  String id, {
  Map<String, List<IntegrationStatus>> integrations =
      const <String, List<IntegrationStatus>>{'production': []},
}) => SystemStatus(
  id: id,
  name: id,
  status: Status.up,
  environments: <SystemEnvironment>[
    for (final entry in integrations.entries)
      SystemEnvironment(
        environment: entry.key,
        status: Status.up,
        applications: <ApplicationStatus>[
          ApplicationStatus(
            id: '$id-api',
            name: id,
            kind: ApplicationKind.api,
            status: Status.up,
          ),
        ],
        integrations: entry.value,
      ),
  ],
);

HostStatus hostWith(List<SystemStatus> systems) => HostStatus(
  host: 'example.com',
  generatedAt: null,
  status: Status.up,
  environments: const <HostEnvironment>[],
  systems: systems,
);

/// Catalog order, as in `catalog.yaml`: the chain does not start at the first
/// system.
HostStatus chain() => hostWith(<SystemStatus>[
  system(
    'heimdall',
    integrations: <String, List<IntegrationStatus>>{
      'production': <IntegrationStatus>[toSystem('heimdall-api', 'hermes')],
    },
  ),
  system(
    'hermes',
    integrations: <String, List<IntegrationStatus>>{
      'production': <IntegrationStatus>[toExternal('hermes-api', 'Mailgun')],
    },
  ),
  system(
    'fortuna',
    integrations: <String, List<IntegrationStatus>>{
      'production': <IntegrationStatus>[toSystem('fortuna-api', 'heimdall')],
    },
  ),
]);

bool adjacent((int, int) a, (int, int) b) =>
    (a.$1 - b.$1).abs() + (a.$2 - b.$2).abs() == 1;

void main() {
  group('mapColumnsFor', () {
    test('3 on a phone, 4 on a tablet, 5 on a computer', () {
      expect(mapColumnsFor(320), 3);
      expect(mapColumnsFor(599), 3);
      expect(mapColumnsFor(600), 4);
      expect(mapColumnsFor(1199), 4);
      expect(mapColumnsFor(1200), 5);
    });
  });

  group('computeMapLayout', () {
    for (final columns in <int>[3, 4, 5]) {
      test('a chain of integrations occupies adjacent cells at every column '
          'count ($columns)', () {
        final layout = computeMapLayout(chain(), columns);
        final path = <MapNode>[fortuna, heimdall, hermes, mailgun];

        for (var i = 0; i + 1 < path.length; i++) {
          expect(
            adjacent(layout.cellOf(path[i]), layout.cellOf(path[i + 1])),
            isTrue,
            reason: '${path[i]} next to ${path[i + 1]}',
          );
        }
      });
    }

    test('layout depends on the host, not the selected environment', () {
      final together = hostWith(<SystemStatus>[
        system(
          'a',
          integrations: <String, List<IntegrationStatus>>{
            'development': <IntegrationStatus>[
              toSystem('a-api', 'c'),
              toExternal('a-api', 'Mailgun'),
            ],
          },
        ),
        system('b'),
        system('c'),
      ]);
      final spread = hostWith(<SystemStatus>[
        system(
          'a',
          integrations: <String, List<IntegrationStatus>>{
            'development': <IntegrationStatus>[toSystem('a-api', 'c')],
            'production': <IntegrationStatus>[toExternal('a-api', 'Mailgun')],
          },
        ),
        system('b'),
        system('c'),
      ]);

      final expected = computeMapLayout(together, 3);
      final actual = computeMapLayout(spread, 3);

      expect(actual.nodes, unorderedEquals(expected.nodes));
      for (final node in expected.nodes) {
        expect(actual.cellOf(node), expected.cellOf(node), reason: '$node');
      }
    });

    test('unconnected systems follow, in catalog order', () {
      final layout = computeMapLayout(
        hostWith(<SystemStatus>[
          system('a'),
          system(
            'b',
            integrations: <String, List<IntegrationStatus>>{
              'production': <IntegrationStatus>[toSystem('b-api', 'c')],
            },
          ),
          system('c'),
          system('d'),
        ]),
        3,
      );

      expect(layout.cellOf(const SystemNode('b')), (0, 0));
      expect(layout.cellOf(const SystemNode('c')), (0, 1));
      expect(layout.cellOf(const SystemNode('a')), (0, 2));
      // Row 1 runs right to left.
      expect(layout.cellOf(const SystemNode('d')), (1, 2));
      expect(layout.rows, 2);
    });

    test('an external appears once however many callers it has', () {
      final layout = computeMapLayout(
        hostWith(<SystemStatus>[
          system(
            'a',
            integrations: <String, List<IntegrationStatus>>{
              'production': <IntegrationStatus>[toExternal('a-api', 'Mailgun')],
            },
          ),
          system(
            'b',
            integrations: <String, List<IntegrationStatus>>{
              'production': <IntegrationStatus>[toExternal('b-api', 'Mailgun')],
              'development': <IntegrationStatus>[
                toExternal('b-api', 'Mailgun'),
              ],
            },
          ),
        ]),
        3,
      );

      expect(layout.nodes.whereType<ExternalNode>(), <MapNode>[mailgun]);
      expect(layout.nodes, hasLength(3));
    });

    test('an integration with an end not on the host draws nothing', () {
      final host = hostWith(<SystemStatus>[
        system(
          'a',
          integrations: <String, List<IntegrationStatus>>{
            'production': <IntegrationStatus>[
              toSystem('a-api', 'nowhere'),
              toSystem('gone-api', 'a'),
              toSystem('a-api', 'a'),
            ],
          },
        ),
      ]);
      final layout = computeMapLayout(host, 3);

      expect(layout.nodes, <MapNode>[const SystemNode('a')]);
      for (final integration
          in host.systems.single.environments.single.integrations) {
        expect(layout.endsOf(integration), isNull);
      }
    });

    test('empty rows collapse only when every cell is empty', () {
      // 3 columns: a b c / f e d / g
      final layout = computeMapLayout(
        hostWith(<SystemStatus>[
          for (final id in 'abcdefg'.split('')) system(id),
        ]),
        3,
      );

      expect(
        layout.drawnRows(<MapNode>[
          const SystemNode('a'),
          const SystemNode('g'),
        ]),
        <int, int>{0: 0, 2: 1},
      );
      expect(
        layout.drawnRows(<MapNode>[
          const SystemNode('a'),
          const SystemNode('e'),
          const SystemNode('g'),
        ]),
        <int, int>{0: 0, 1: 1, 2: 2},
      );
    });
  });

  group('distanceToSegment', () {
    const a = Offset.zero;
    const b = Offset(10, 0);

    test('distanceToSegment measures to the nearest point', () {
      expect(distanceToSegment(const Offset(5, 3), a, b), 3);
      // Past either end, the distance is to that end.
      expect(distanceToSegment(const Offset(13, 4), a, b), 5);
      expect(distanceToSegment(const Offset(-3, 4), a, b), 5);
    });

    test('a segment of no length is a point', () {
      expect(distanceToSegment(const Offset(3, 4), a, a), 5);
    });
  });
}
