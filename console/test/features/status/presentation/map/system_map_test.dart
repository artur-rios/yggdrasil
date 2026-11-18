import 'package:flutter_test/flutter_test.dart';
import 'package:material_ui/material_ui.dart';
import 'package:yggdrasil_console/features/status/domain/status.dart';
import 'package:yggdrasil_console/features/status/domain/status_logic.dart';
import 'package:yggdrasil_console/features/status/presentation/layout.dart';
import 'package:yggdrasil_console/features/status/presentation/map/system_map.dart';
import 'package:yggdrasil_console/features/status/presentation/widgets/status_visuals.dart';

const String production = 'production';
const String development = 'development';

IntegrationStatus link(
  String from,
  String system, {
  IntegrationHealth status = IntegrationHealth.up,
  String? reason,
}) => IntegrationStatus(
  from: from,
  system: system,
  check: system,
  status: status,
  reason: reason,
);

IntegrationStatus linkOut(
  String from,
  String external, {
  IntegrationHealth status = IntegrationHealth.up,
}) => IntegrationStatus(
  from: from,
  external: external,
  check: external,
  status: status,
);

SystemEnvironment deployed(
  String environment,
  String application, {
  Status status = Status.up,
  bool onDemand = false,
  List<IntegrationStatus> integrations = const <IntegrationStatus>[],
}) => SystemEnvironment(
  environment: environment,
  status: status,
  onDemand: onDemand,
  integrations: integrations,
  applications: <ApplicationStatus>[
    ApplicationStatus(
      id: application,
      name: application,
      kind: ApplicationKind.api,
      status: status,
    ),
  ],
);

SystemStatus sys(
  String id,
  String name,
  List<SystemEnvironment> environments, {
  String? icon,
}) => SystemStatus(
  id: id,
  name: name,
  icon: icon,
  status: Status.up,
  environments: environments,
);

HostStatus hostOf(List<SystemStatus> systems) => HostStatus(
  host: 'example.com',
  generatedAt: null,
  status: Status.up,
  environments: const <HostEnvironment>[
    HostEnvironment(
      id: development,
      name: 'Development',
      onDemand: true,
      status: Status.stopped,
    ),
    HostEnvironment(
      id: production,
      name: 'Production',
      onDemand: false,
      status: Status.up,
    ),
  ],
  systems: systems,
);

/// Heimdall calling Hermes, with the status the test picks.
HostStatus heimdallToHermes({
  IntegrationHealth status = IntegrationHealth.up,
}) => hostOf(<SystemStatus>[
  sys('heimdall', 'Heimdall', <SystemEnvironment>[
    deployed(
      production,
      'heimdall-api',
      integrations: <IntegrationStatus>[
        link('heimdall-api', 'hermes', status: status),
      ],
    ),
  ]),
  sys('hermes', 'Hermes', <SystemEnvironment>[
    deployed(production, 'hermes-api'),
  ]),
]);

/// Hermes calling Mailgun.
HostStatus hermesToMailgun() => hostOf(<SystemStatus>[
  sys('hermes', 'Hermes', <SystemEnvironment>[
    deployed(
      production,
      'hermes-api',
      integrations: <IntegrationStatus>[linkOut('hermes-api', 'Mailgun')],
    ),
  ]),
]);

Finder tile(String id) => find.byKey(ValueKey<String>('map-system-$id'));

Finder line(String from, String to) =>
    find.byKey(ValueKey<String>('map-line-$from-$to'));

final Finder anyLine = find.byWidgetPredicate((widget) {
  final key = widget.key;

  return key is ValueKey<String> && key.value.startsWith('map-line-');
});

/// Pumps the map alone; returns the ids `onOpenSystem` received.
Future<List<String>> pumpMap(
  WidgetTester tester,
  HostStatus host, {
  String environment = production,
  Size size = const Size(1000, 900),
}) async {
  tester.view.physicalSize = size;
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.reset);

  final opened = <String>[];

  await tester.pumpWidget(
    MaterialApp(
      home: Scaffold(
        body: SingleChildScrollView(
          child: SystemMap(
            host: host,
            environmentId: environment,
            onOpenSystem: opened.add,
          ),
        ),
      ),
    ),
  );

  return opened;
}

void main() {
  testWidgets(
    'given systems deployed in some environments, when another environment '
    'is drawn, then only its systems show, each in its fixed cell',
    (tester) async {
      final host = hostOf(<SystemStatus>[
        sys('heimdall', 'Heimdall', <SystemEnvironment>[
          deployed(development, 'heimdall-api', status: Status.stopped),
          deployed(production, 'heimdall-api'),
        ]),
        sys('fortuna', 'Fortuna', <SystemEnvironment>[
          deployed(production, 'fortuna-api'),
        ]),
        sys('hermes', 'Hermes', <SystemEnvironment>[
          deployed(development, 'hermes-api', status: Status.stopped),
          deployed(production, 'hermes-api'),
        ]),
      ]);

      await pumpMap(tester, host);
      final heimdallAt = tester.getCenter(tile('heimdall'));
      final hermesAt = tester.getCenter(tile('hermes'));
      expect(tile('fortuna'), findsOneWidget);

      await pumpMap(tester, host, environment: development);

      expect(tile('fortuna'), findsNothing);
      expect(find.text('Fortuna'), findsNothing);
      expect(tester.getCenter(tile('heimdall')), heimdallAt);
      expect(tester.getCenter(tile('hermes')), hermesAt);
    },
  );

  testWidgets(
    'given a system with no container in the environment, when drawn, then '
    'its cell is empty',
    (tester) async {
      await pumpMap(
        tester,
        hostOf(<SystemStatus>[
          sys('heimdall', 'Heimdall', <SystemEnvironment>[
            deployed(production, 'heimdall-api', status: Status.notDeployed),
          ]),
          sys('hermes', 'Hermes', <SystemEnvironment>[
            deployed(production, 'hermes-api'),
          ]),
        ]),
      );

      expect(tile('heimdall'), findsNothing);
      expect(tile('hermes'), findsOneWidget);
    },
  );

  testWidgets(
    'given a row with nothing deployed in the environment, when drawn, then '
    'it collapses',
    (tester) async {
      final host = hostOf(<SystemStatus>[
        for (final id in <String>['a', 'b', 'c', 'd'])
          sys(id, id, <SystemEnvironment>[deployed(production, '$id-api')]),
        sys('e', 'e', <SystemEnvironment>[
          deployed(development, 'e-api'),
          deployed(production, 'e-api'),
        ]),
      ]);

      await pumpMap(tester, host, size: const Size(320, 900));
      final inFull = tester.getCenter(tile('e')).dy;
      await pumpMap(
        tester,
        host,
        environment: development,
        size: const Size(320, 900),
      );

      expect(tester.getCenter(tile('e')).dy, lessThan(inFull));
    },
  );

  testWidgets(
    'given an integration listed in one environment, when each is drawn, '
    'then its line shows only there',
    (tester) async {
      final host = hostOf(<SystemStatus>[
        sys('heimdall', 'Heimdall', <SystemEnvironment>[
          deployed(development, 'heimdall-api'),
          deployed(
            production,
            'heimdall-api',
            integrations: <IntegrationStatus>[link('heimdall-api', 'hermes')],
          ),
        ]),
        sys('hermes', 'Hermes', <SystemEnvironment>[
          deployed(development, 'hermes-api'),
          deployed(production, 'hermes-api'),
        ]),
      ]);

      await pumpMap(tester, host);
      expect(line('heimdall', 'hermes'), findsOneWidget);

      await pumpMap(tester, host, environment: development);
      expect(anyLine, findsNothing);
    },
  );

  for (final (health, word, glyph) in <(IntegrationHealth, String, IconData)>[
    (IntegrationHealth.up, 'OK', Icons.check),
    (IntegrationHealth.down, 'Failing', Icons.close),
    (IntegrationHealth.unknown, 'Unknown', Icons.question_mark),
    (IntegrationHealth.stopped, 'Stopped', Icons.pause),
    (IntegrationHealth.notConfigured, 'Not configured', Icons.remove),
  ]) {
    testWidgets(
      'given a ${health.wire} integration, when drawn, then its pill reads $word '
      'with '
      'its own glyph',
      (tester) async {
        await pumpMap(tester, heimdallToHermes(status: health));

        final pill = line('heimdall', 'hermes');
        expect(
          find.descendant(of: pill, matching: find.text(word)),
          findsOneWidget,
        );
        expect(
          find.descendant(of: pill, matching: find.byIcon(glyph)),
          findsOneWidget,
        );
      },
    );
  }

  test('given stopped and not configured integrations, when their look is '
      'chosen, then both are grey and dashed, each its own way', () {
    IntegrationVisual visual(IntegrationHealth health) =>
        IntegrationVisual.of(health, Brightness.light);

    final stopped = visual(IntegrationHealth.stopped);
    final notConfigured = visual(IntegrationHealth.notConfigured);

    expect(stopped.dash, isNotNull);
    expect(notConfigured.dash, isNotNull);
    expect(stopped.dash, isNot(notConfigured.dash));
    expect(stopped.foreground, notConfigured.foreground);
    expect(visual(IntegrationHealth.up).dash, isNull);
    expect(visual(IntegrationHealth.down).dash, isNull);
    expect(visual(IntegrationHealth.unknown).dash, isNull);
  });

  testWidgets(
    'given a stopped system with a stopped integration, when drawn, then '
    'neither is shown or counted as a problem',
    (tester) async {
      final host = hostOf(<SystemStatus>[
        sys('heimdall', 'Heimdall', <SystemEnvironment>[
          deployed(
            development,
            'heimdall-api',
            status: Status.stopped,
            onDemand: true,
            integrations: <IntegrationStatus>[
              link('heimdall-api', 'hermes', status: IntegrationHealth.stopped),
            ],
          ),
        ]),
        sys('hermes', 'Hermes', <SystemEnvironment>[
          deployed(development, 'hermes-api', status: Status.stopped),
        ]),
      ]);

      await pumpMap(tester, host, environment: development);

      expect(find.byIcon(Icons.pause_circle_outline), findsNWidgets(2));
      expect(find.byIcon(Icons.cancel), findsNothing);
      expect(find.byIcon(Icons.warning_rounded), findsNothing);
      expect(find.text('Failing'), findsNothing);
      expect(find.text('Stopped'), findsOneWidget);
      expect(environmentSummaryLine(host, development), isEmpty);
    },
  );

  for (final status in <Status>[Status.up, Status.stopped]) {
    testWidgets(
      'given an on-demand system that is ${status.wire}, when drawn, then it '
      'shows the on demand label',
      (tester) async {
        await pumpMap(
          tester,
          hostOf(<SystemStatus>[
            sys('heimdall', 'Heimdall', <SystemEnvironment>[
              deployed(
                production,
                'heimdall-api',
                status: status,
                onDemand: true,
              ),
            ]),
          ]),
        );

        expect(find.text('on demand'), findsOneWidget);
        expect(find.byIcon(Icons.power_settings_new), findsOneWidget);
      },
    );
  }

  testWidgets(
    'given an always-on system, when drawn, then it shows no on demand label',
    (tester) async {
      await pumpMap(tester, heimdallToHermes());

      expect(find.text('on demand'), findsNothing);
      expect(find.byIcon(Icons.power_settings_new), findsNothing);
    },
  );

  testWidgets(
    'given an external target, when drawn, then it is captioned external and '
    'has no status',
    (tester) async {
      await pumpMap(tester, hermesToMailgun());

      expect(find.text('Mailgun'), findsOneWidget);
      expect(find.text('external'), findsOneWidget);
      expect(find.byIcon(Icons.cloud), findsOneWidget);
      // Only Hermes has a badge.
      expect(find.byIcon(Icons.check_circle), findsOneWidget);
      expect(line('hermes', 'Mailgun'), findsOneWidget);
    },
  );

  testWidgets(
    'given an unknown or missing icon name, when drawn, then the neutral icon '
    'shows',
    (tester) async {
      await pumpMap(
        tester,
        hostOf(<SystemStatus>[
          sys('a', 'A', <SystemEnvironment>[
            deployed(production, 'a-api'),
          ], icon: 'no_such_icon'),
          sys('b', 'B', <SystemEnvironment>[deployed(production, 'b-api')]),
          sys('c', 'C', <SystemEnvironment>[
            deployed(production, 'c-api'),
          ], icon: 'shield'),
        ]),
      );

      expect(find.byIcon(Icons.hub), findsNWidgets(2));
      expect(find.byIcon(Icons.shield), findsOneWidget);
    },
  );

  test('given each health, when its look is chosen, then each has its own '
      'word and glyph, tinted like the nearest status', () {
    final visuals = <IntegrationVisual>[
      for (final health in IntegrationHealth.values)
        IntegrationVisual.of(health, Brightness.light),
    ];

    expect(visuals.map((v) => v.label).toSet(), hasLength(visuals.length));
    expect(visuals.map((v) => v.icon).toSet(), hasLength(visuals.length));
    expect(
      IntegrationVisual.of(IntegrationHealth.down, Brightness.light).foreground,
      StatusVisual.of(Status.down, Brightness.light).foreground,
    );
  });

  test('given a dark theme, when a look is chosen, then it uses the dark '
      'tints and keeps its word, glyph and dash', () {
    for (final health in IntegrationHealth.values) {
      final light = IntegrationVisual.of(health, Brightness.light);
      final dark = IntegrationVisual.of(health, Brightness.dark);

      expect(dark.foreground, isNot(light.foreground), reason: health.wire);
      expect(dark.label, light.label);
      expect(dark.icon, light.icon);
      expect(dark.dash, light.dash);
    }
  });

  testWidgets(
    'given a response with no icon, integrations or details, when drawn, then '
    'it shows neutral icons and no lines',
    (tester) async {
      final host = HostStatus.fromJson(<String, dynamic>{
        'host': 'example.com',
        'generatedAt': null,
        'status': 'up',
        'environments': <dynamic>[
          <String, dynamic>{'id': production, 'status': 'up'},
        ],
        'systems': <dynamic>[
          <String, dynamic>{
            'id': 'heimdall',
            'name': 'Heimdall',
            'status': 'up',
            'environments': <dynamic>[
              <String, dynamic>{
                'environment': production,
                'status': 'up',
                'applications': <dynamic>[
                  <String, dynamic>{
                    'id': 'heimdall-api',
                    'name': 'Heimdall API',
                    'kind': 'api',
                    'status': 'up',
                  },
                ],
              },
            ],
          },
        ],
      });

      await pumpMap(tester, host);

      expect(tile('heimdall'), findsOneWidget);
      expect(find.byIcon(Icons.hub), findsOneWidget);
      expect(anyLine, findsNothing);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets(
    'given systems, lines and an external, when drawn, then each has a '
    'semantics label',
    (tester) async {
      final handle = tester.ensureSemantics();

      await pumpMap(
        tester,
        hostOf(<SystemStatus>[
          sys('heimdall', 'Heimdall', <SystemEnvironment>[
            deployed(
              production,
              'heimdall-api',
              status: Status.degraded,
              onDemand: true,
              integrations: <IntegrationStatus>[
                link(
                  'heimdall-api',
                  'hermes',
                  status: IntegrationHealth.down,
                  reason: 'connection refused',
                ),
              ],
            ),
          ]),
          sys('hermes', 'Hermes', <SystemEnvironment>[
            deployed(
              production,
              'hermes-api',
              integrations: <IntegrationStatus>[
                linkOut(
                  'hermes-api',
                  'Mailgun',
                  status: IntegrationHealth.notConfigured,
                ),
              ],
            ),
          ]),
        ]),
      );

      for (final label in <String>[
        'Heimdall, degraded, on demand',
        'Hermes, up',
        'Heimdall to Hermes: failing, connection refused',
        'Hermes to Mailgun: not configured',
        'Mailgun, external',
      ]) {
        expect(find.bySemanticsLabel(label), findsOneWidget, reason: label);
      }

      expect(
        tester.getSemantics(
          find.bySemanticsLabel('Heimdall, degraded, on demand'),
        ),
        matchesSemantics(
          label: 'Heimdall, degraded, on demand',
          isButton: true,
          hasTapAction: true,
        ),
      );

      handle.dispose();
    },
  );

  testWidgets(
    'given a system, when tapped on its icon or name, then it opens',
    (tester) async {
      final opened = await pumpMap(tester, heimdallToHermes());

      await tester.tap(tile('hermes'));
      await tester.tap(find.text('Heimdall'));

      expect(opened, <String>['hermes', 'heimdall']);
    },
  );

  testWidgets(
    'given a line, when tapped on or near it, then the caller opens',
    (tester) async {
      final opened = await pumpMap(tester, heimdallToHermes());
      final middle = tester.getCenter(line('heimdall', 'hermes'));

      await tester.tapAt(middle);
      await tester.tapAt(middle + const Offset(0, 11));

      expect(opened, <String>['heimdall', 'heimdall']);
    },
  );

  testWidgets(
    'given a line, when tapped further than 12 px from it, then nothing '
    'opens',
    (tester) async {
      final opened = await pumpMap(tester, heimdallToHermes());
      final middle = tester.getCenter(line('heimdall', 'hermes'));

      await tester.tapAt(middle + const Offset(0, 14));

      expect(opened, isEmpty);
    },
  );

  testWidgets('given an external, when tapped, then nothing opens', (
    tester,
  ) async {
    final opened = await pumpMap(tester, hermesToMailgun());
    final circle = find.byKey(const ValueKey<String>('map-external-Mailgun'));

    await tester.tap(circle);
    // Its edge, right where the line ends.
    await tester.tapAt(tester.getRect(circle).centerLeft + const Offset(2, 0));
    await tester.tap(find.text('external'));

    expect(opened, isEmpty);
  });

  /// The pill's own box, on the line from Heimdall to Hermes.
  Finder heimdallToHermesPill() => find.descendant(
    of: line('heimdall', 'hermes'),
    matching: find.byType(DecoratedBox),
  );

  for (final width in <double>[320, 390, 700]) {
    for (final health in IntegrationHealth.values) {
      testWidgets(
        'given a ${health.wire} pill that would cover a level line at $width px, '
        'when drawn, then it rides above the line',
        (tester) async {
          await pumpMap(
            tester,
            heimdallToHermes(status: health),
            size: Size(width, 900),
          );

          // The arrowhead is 5 px either side of the line.
          expect(
            tester.getRect(heimdallToHermesPill()).bottom,
            lessThanOrEqualTo(tester.getCenter(tile('heimdall')).dy - 5),
          );
          expect(tester.takeException(), isNull);
        },
      );
    }
  }

  testWidgets(
    'given room for it, when a pill is drawn, then it sits on the line',
    (tester) async {
      await pumpMap(tester, heimdallToHermes());

      expect(
        tester.getCenter(heimdallToHermesPill()).dy,
        tester.getCenter(tile('heimdall')).dy,
      );
    },
  );

  testWidgets(
    'given a pill riding above its line, when the line is tapped, then the '
    'caller opens',
    (tester) async {
      final opened = await pumpMap(
        tester,
        heimdallToHermes(status: IntegrationHealth.down),
        size: const Size(390, 900),
      );

      await tester.tapAt(
        (tester.getCenter(tile('heimdall')) +
                tester.getCenter(tile('hermes'))) /
            2,
      );

      expect(opened, <String>['heimdall']);
    },
  );

  testWidgets(
    'given a line behind the caption of an external, when the caption is '
    'tapped, then nothing opens',
    (tester) async {
      // 3 columns: a b Mailgun / . . c, so c's line runs up behind the caption.
      final opened = await pumpMap(
        tester,
        hostOf(<SystemStatus>[
          sys('a', 'A', <SystemEnvironment>[
            deployed(
              production,
              'a-api',
              integrations: <IntegrationStatus>[link('a-api', 'b')],
            ),
          ]),
          sys('b', 'B', <SystemEnvironment>[
            deployed(
              production,
              'b-api',
              integrations: <IntegrationStatus>[linkOut('b-api', 'Mailgun')],
            ),
          ]),
          sys('c', 'C', <SystemEnvironment>[
            deployed(
              production,
              'c-api',
              integrations: <IntegrationStatus>[linkOut('c-api', 'Mailgun')],
            ),
          ]),
        ]),
        size: const Size(320, 900),
      );

      await tester.tap(find.text('external'));
      await tester.tap(find.text('Mailgun'));

      expect(opened, isEmpty);
    },
  );

  testWidgets(
    'given two integrations between the same systems, when drawn, then each '
    'keeps its own pill',
    (tester) async {
      await pumpMap(
        tester,
        hostOf(<SystemStatus>[
          sys('heimdall', 'Heimdall', <SystemEnvironment>[
            SystemEnvironment(
              environment: production,
              status: Status.up,
              applications: const <ApplicationStatus>[
                ApplicationStatus(
                  id: 'heimdall-api',
                  name: 'API',
                  kind: ApplicationKind.api,
                  status: Status.up,
                ),
                ApplicationStatus(
                  id: 'heimdall-worker',
                  name: 'Worker',
                  kind: ApplicationKind.worker,
                  status: Status.up,
                ),
              ],
              integrations: <IntegrationStatus>[
                link('heimdall-api', 'hermes'),
                link(
                  'heimdall-worker',
                  'hermes',
                  status: IntegrationHealth.down,
                ),
              ],
            ),
          ]),
          sys('hermes', 'Hermes', <SystemEnvironment>[
            deployed(production, 'hermes-api'),
          ]),
        ]),
      );

      expect(line('heimdall', 'hermes'), findsNWidgets(2));
      expect(
        tester
            .getRect(find.text('OK'))
            .overlaps(tester.getRect(find.text('Failing'))),
        isFalse,
      );
    },
  );

  for (final width in <double>[320, 360, 800, 1400]) {
    testWidgets(
      'given $width px of width, when seven systems are drawn, then every '
      'column fits with no overflow',
      (tester) async {
        const ids = <String>['a', 'b', 'c', 'd', 'e', 'f', 'g'];
        final host = hostOf(<SystemStatus>[
          for (final id in ids)
            sys(id, 'A system with a long name $id', <SystemEnvironment>[
              deployed(
                production,
                '$id-api',
                onDemand: true,
                integrations: <IntegrationStatus>[
                  if (id == 'a')
                    link('a-api', 'b', status: IntegrationHealth.notConfigured),
                ],
              ),
            ]),
        ]);

        await pumpMap(tester, host, size: Size(width, 1200));

        expect(tester.takeException(), isNull);

        final centres = <double>{
          for (final id in ids) tester.getCenter(tile(id)).dx,
        };
        expect(centres, hasLength(mapColumnsFor(width)));
        expect(centres.every((x) => x > 0 && x < width), isTrue);
      },
    );
  }

  testWidgets(
    'given an environment with nothing deployed, when drawn, then it says so',
    (tester) async {
      await pumpMap(tester, heimdallToHermes(), environment: development);

      expect(find.text('Nothing is deployed in Development.'), findsOneWidget);
    },
  );

  testWidgets('given a host with no systems, when drawn, then it says so', (
    tester,
  ) async {
    await pumpMap(tester, hostOf(const <SystemStatus>[]));

    expect(find.text('This host has no systems.'), findsOneWidget);
  });
}
