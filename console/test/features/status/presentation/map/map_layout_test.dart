import 'package:flutter_test/flutter_test.dart';
import 'package:yggdrasil_console/features/status/domain/status.dart';
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

/// [id] calling every target in [calls], in production.
SystemStatus calling(String id, List<IntegrationStatus> calls) => system(
  id,
  integrations: <String, List<IntegrationStatus>>{'production': calls},
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
  calling('heimdall', <IntegrationStatus>[toSystem('heimdall-api', 'hermes')]),
  calling('hermes', <IntegrationStatus>[toExternal('hermes-api', 'Mailgun')]),
  calling('fortuna', <IntegrationStatus>[toSystem('fortuna-api', 'heimdall')]),
]);

bool adjacent((int, int) a, (int, int) b) =>
    (a.$1 - b.$1).abs() + (a.$2 - b.$2).abs() == 1;

void main() {
  group('computeMapLayout', () {
    for (final columns in <int>[3, 4, 5]) {
      test('given a chain of integrations and $columns columns, when laid '
          'out, then each link occupies adjacent cells', () {
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

    test('given integrations spread over environments, when laid out, then '
        'the layout is the same as with them all in one', () {
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

    test('given unconnected systems, when laid out, then they follow the '
        'connected ones, in catalog order, boustrophedon', () {
      final layout = computeMapLayout(
        hostWith(<SystemStatus>[
          system('a'),
          calling('b', <IntegrationStatus>[toSystem('b-api', 'c')]),
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

    test('given an external with several callers, when laid out, then it '
        'appears once', () {
      final layout = computeMapLayout(
        hostWith(<SystemStatus>[
          calling('a', <IntegrationStatus>[toExternal('a-api', 'Mailgun')]),
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

    test('given integrations with an end not on the host or a system calling '
        'itself, when laid out, then nothing is drawn for them', () {
      final host = hostWith(<SystemStatus>[
        calling('a', <IntegrationStatus>[
          toSystem('a-api', 'nowhere'),
          toSystem('gone-api', 'a'),
          toSystem('a-api', 'a'),
          // Neither a system nor an external.
          const IntegrationStatus(
            from: 'a-api',
            check: 'c',
            status: IntegrationHealth.up,
          ),
        ]),
      ]);
      final layout = computeMapLayout(host, 3);

      expect(layout.nodes, <MapNode>[const SystemNode('a')]);
      for (final integration
          in host.systems.single.environments.single.integrations) {
        expect(layout.endsOf(integration), isNull);
      }
    });

    test('given an integration between two systems and one to an external, '
        'when its ends are asked, then they are the caller and the target', () {
      final layout = computeMapLayout(chain(), 3);

      expect(layout.endsOf(toSystem('heimdall-api', 'hermes')), (
        heimdall,
        hermes,
      ));
      expect(layout.endsOf(toExternal('hermes-api', 'Mailgun')), (
        hermes,
        mailgun,
      ));
    });

    test(
      'given a cycle with no system that only calls, when laid out, then '
      'it starts at the first caller in catalog order and stays adjacent',
      () {
        final layout = computeMapLayout(
          hostWith(<SystemStatus>[
            calling('a', <IntegrationStatus>[toSystem('a-api', 'b')]),
            calling('b', <IntegrationStatus>[toSystem('b-api', 'c')]),
            calling('c', <IntegrationStatus>[toSystem('c-api', 'a')]),
          ]),
          3,
        );

        expect(layout.nodes, <MapNode>[
          const SystemNode('a'),
          const SystemNode('b'),
          const SystemNode('c'),
        ]);
        expect(layout.rows, 1);
      },
    );

    test('given two separate groups, when laid out, then each group is '
        'contiguous, in the catalog order of their first member', () {
      final layout = computeMapLayout(
        hostWith(<SystemStatus>[
          calling('a', <IntegrationStatus>[toSystem('a-api', 'c')]),
          calling('b', <IntegrationStatus>[toSystem('b-api', 'd')]),
          system('c'),
          system('d'),
        ]),
        5,
      );

      expect(layout.nodes, <MapNode>[
        const SystemNode('a'),
        const SystemNode('c'),
        const SystemNode('b'),
        const SystemNode('d'),
      ]);
    });

    test('given a host with no systems, when laid out, then it has no nodes '
        'and no rows', () {
      final layout = computeMapLayout(hostWith(const <SystemStatus>[]), 3);

      expect(layout.nodes, isEmpty);
      expect(layout.rows, 0);
      expect(layout.drawnRows(const <MapNode>[]), isEmpty);
    });

    test('given exactly one full row of systems, when laid out, then there '
        'is one row', () {
      final layout = computeMapLayout(
        hostWith(<SystemStatus>[for (final id in 'abc'.split('')) system(id)]),
        3,
      );

      expect(layout.rows, 1);
      expect(layout.cellOf(const SystemNode('c')), (0, 2));
    });

    test('given rows with nothing shown in them, when drawn, then only those '
        'rows collapse', () {
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

  group('MapNode', () {
    test('given nodes with the same id or name, when compared, then they are '
        'equal, and a system never equals an external', () {
      expect(const SystemNode('a'), SystemNode('a'.toLowerCase()));
      expect(
        const SystemNode('a').hashCode,
        SystemNode('a'.toLowerCase()).hashCode,
      );
      expect(const ExternalNode('a'), ExternalNode('a'.toLowerCase()));
      expect(const SystemNode('a'), isNot(const ExternalNode('a')));
      expect(const SystemNode('a'), isNot(const SystemNode('b')));
    });
  });

  group('distanceToSegment', () {
    const a = Offset.zero;
    const b = Offset(10, 0);

    test('given points beside and past a segment, when measured, then it is '
        'the distance to the nearest point of it', () {
      expect(distanceToSegment(const Offset(5, 3), a, b), 3);
      // Past either end, the distance is to that end.
      expect(distanceToSegment(const Offset(13, 4), a, b), 5);
      expect(distanceToSegment(const Offset(-3, 4), a, b), 5);
    });

    test('given a point on the segment, when measured, then it is 0', () {
      expect(distanceToSegment(const Offset(4, 0), a, b), 0);
    });

    test('given a segment of no length, when measured, then it is the '
        'distance to that point', () {
      expect(distanceToSegment(const Offset(3, 4), a, a), 5);
    });
  });
}
