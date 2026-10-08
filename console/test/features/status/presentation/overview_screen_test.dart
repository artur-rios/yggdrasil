import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:yggdrasil_console/features/environments/domain/environment.dart';
import 'package:yggdrasil_console/features/status/data/status_source.dart';
import 'package:yggdrasil_console/features/status/domain/status.dart';
import 'package:yggdrasil_console/features/status/presentation/widgets/application_detail.dart';
import 'package:yggdrasil_console/features/status/presentation/widgets/status_visuals.dart';
import 'package:yggdrasil_console/features/status/presentation/widgets/system_card.dart';

import '../../../helpers.dart';

Finder systemHeader(String id) => find.byKey(ValueKey<String>('system-$id'));

Finder card(String id) => find.byKey(ValueKey<String>('card-$id'));

/// System ids in reading order: top to bottom, then left to right.
List<String> shownSystems(WidgetTester tester) {
  final cards = tester.widgetList<SystemCard>(find.byType(SystemCard)).toList()
    ..sort((a, b) {
      final pa = tester.getTopLeft(find.byWidget(a));
      final pb = tester.getTopLeft(find.byWidget(b));

      return pa.dy != pb.dy ? pa.dy.compareTo(pb.dy) : pa.dx.compareTo(pb.dx);
    });

  return cards.map((card) => card.system.id).toList();
}

/// The environment chips under [of], as `name · status`, in reading order.
List<String> chipsIn(WidgetTester tester, Finder of) {
  final chips = find.descendant(
    of: of,
    matching: find.byType(EnvironmentStatusChip),
  );
  final positioned =
      <(Offset, EnvironmentStatusChip)>[
        for (final element in chips.evaluate())
          (
            tester.getTopLeft(find.byWidget(element.widget)),
            element.widget as EnvironmentStatusChip,
          ),
      ]..sort(
        (a, b) => a.$1.dy != b.$1.dy
            ? a.$1.dy.compareTo(b.$1.dy)
            : a.$1.dx.compareTo(b.$1.dx),
      );

  return <String>[
    for (final (_, chip) in positioned)
      '${chip.name} · ${StatusVisual.of(chip.status, Brightness.light).label}',
  ];
}

Future<void> expandAll(WidgetTester tester, List<String> ids) async {
  for (final id in ids) {
    await tester.ensureVisible(systemHeader(id));
    await tester.pump();
    await tester.tap(systemHeader(id));
    await settle(tester);
  }
}

const List<String> demoSystems = <String>['heimdall', 'fortuna', 'yggdrasil'];

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

void main() {
  testWidgets('the overview shows the host, its environments and its systems', (
    tester,
  ) async {
    final source = FakeStatusSource();
    await pumpConsole(tester, source: source);

    expect(source.calls.single.$1, production);
    expect(find.byKey(const ValueKey<String>('host-name')), findsOneWidget);
    expect(find.text('example.com'), findsOneWidget);
    expect(find.text('3 systems · 3 environments'), findsOneWidget);
    expect(find.textContaining('updated '), findsOneWidget);
    expect(
      chipsIn(tester, find.byKey(const ValueKey<String>('host-environments'))),
      <String>[
        'Development · Stopped',
        'Homologation · Stopped',
        'Production · Up',
      ],
    );

    // All up overall, so by name.
    expect(shownSystems(tester), <String>['fortuna', 'heimdall', 'yggdrasil']);

    // One chip per environment, in the host's order.
    expect(chipsIn(tester, card('heimdall')), <String>[
      'Development · Stopped',
      'Homologation · Stopped',
      'Production · Up',
    ]);
    expect(chipsIn(tester, card('yggdrasil')), <String>[
      'Development · Up',
      'Homologation · Up',
      'Production · Up',
    ]);
    expect(find.text('Development · Stopped'), findsNWidgets(3));

    // Collapsed: no application rows yet.
    expect(find.byType(ApplicationRow), findsNothing);
    expect(find.byType(EnvironmentSection), findsNothing);
  });

  testWidgets('expanding a system shows one section per environment', (
    tester,
  ) async {
    await pumpConsole(tester, source: FakeStatusSource());

    await tester.tap(systemHeader('heimdall'));
    await settle(tester);

    final sections = tester
        .widgetList<EnvironmentSection>(find.byType(EnvironmentSection))
        .toList();
    expect(sections.map((section) => section.name), <String>[
      'Development',
      'Homologation',
      'Production',
    ]);
    final tops = <double>[
      for (final section in sections)
        tester.getTopLeft(find.byWidget(section)).dy,
    ];
    expect(tops, orderedEquals(List<double>.of(tops)..sort()));

    expect(find.byType(ApplicationRow), findsNWidgets(6));
    expect(find.text('on demand'), findsNWidgets(2));
    expect(find.text('2 applications · all stopped'), findsNWidgets(2));
    expect(find.text('2 applications · all up'), findsOneWidget);
    expect(find.text('Heimdall API'), findsNWidgets(3));
    expect(find.text('v1.4.0 · 3f2a9c1'), findsOneWidget);
    expect(find.text('v1.5.0 · 9c41e7b'), findsOneWidget);
    expect(find.text('12 ms'), findsOneWidget);
    expect(find.text('running · healthy'), findsNWidgets(2));
    expect(find.text('exited'), findsNWidgets(4));
    expect(
      find.byKey(const ValueKey<String>('application-production-heimdall-api')),
      findsOneWidget,
    );

    await tester.tap(systemHeader('heimdall'));
    await settle(tester);

    expect(find.byType(ApplicationRow), findsNothing);
  });

  testWidgets('320 dp: one column, nothing overflows when all is expanded', (
    tester,
  ) async {
    await pumpConsole(
      tester,
      source: FakeStatusSource(),
      size: const Size(320, 12000),
    );

    await expandAll(tester, demoSystems);

    // 2 + 2 applications in 3 environments, and 8 platform components in 3.
    expect(find.byType(ApplicationRow), findsNWidgets(36));
    expect(find.byType(EnvironmentSection), findsNWidgets(9));
    final lefts = tester
        .widgetList<SystemCard>(find.byType(SystemCard))
        .map((card) => tester.getTopLeft(find.byWidget(card)).dx)
        .toSet();
    expect(lefts, hasLength(1));
    expect(tester.takeException(), isNull);
  });

  testWidgets('1400 dp: three columns, nothing overflows when expanded', (
    tester,
  ) async {
    await pumpConsole(
      tester,
      source: FakeStatusSource(),
      size: const Size(1400, 5000),
    );

    await expandAll(tester, demoSystems);

    final lefts = tester
        .widgetList<SystemCard>(find.byType(SystemCard))
        .map((card) => tester.getTopLeft(find.byWidget(card)).dx)
        .toSet();
    expect(lefts, hasLength(3));
    expect(tester.takeException(), isNull);
  });

  testWidgets('800 dp: two columns', (tester) async {
    await pumpConsole(
      tester,
      source: FakeStatusSource(),
      size: const Size(800, 3000),
    );

    final lefts = tester
        .widgetList<SystemCard>(find.byType(SystemCard))
        .map((card) => tester.getTopLeft(find.byWidget(card)).dx)
        .toSet();
    expect(lefts, hasLength(2));
  });

  testWidgets('an application opens in a bottom sheet on a phone', (
    tester,
  ) async {
    await pumpConsole(
      tester,
      source: FakeStatusSource(),
      size: const Size(360, 800),
    );

    await expandAll(tester, <String>['yggdrasil']);
    // The name, not the row's centre, which may be one of its link buttons.
    final jenkins = find.text('Jenkins').first;
    await tester.ensureVisible(jenkins);
    await settle(tester);
    await tester.tap(jenkins);
    await settle(tester);

    expect(find.byType(BottomSheet), findsOneWidget);
    expect(find.byType(ApplicationDetail), findsOneWidget);
    expect(find.text('Yggdrasil · Development · jenkins'), findsOneWidget);
    await tester.scrollUntilVisible(
      find.text('Not probed: not deployed on this host.'),
      200,
      scrollable: find
          .descendant(
            of: find.byType(ApplicationDetail),
            matching: find.byType(Scrollable),
          )
          .first,
    );
    expect(find.text('No container on this host.'), findsNWidgets(2));
    expect(find.text('Not probed: not deployed on this host.'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('an application opens in a dialog on a wide screen', (
    tester,
  ) async {
    await pumpConsole(
      tester,
      source: FakeStatusSource(),
      size: const Size(1400, 1400),
    );

    await tester.tap(systemHeader('heimdall'));
    await settle(tester);
    await tester.tap(
      find.descendant(
        of: find.byKey(
          const ValueKey<String>('application-production-heimdall-api'),
        ),
        matching: find.text('Heimdall API'),
      ),
    );
    await settle(tester);

    expect(find.byType(Dialog), findsOneWidget);
    final detail = find.byType(ApplicationDetail);
    expect(
      find.descendant(
        of: detail,
        matching: find.text('heimdall-api:production-1.4.0-3f2a9c1'),
      ),
      findsOneWidget,
    );
    expect(
      find.descendant(
        of: detail,
        matching: find.text('Heimdall · Production · heimdall-api'),
      ),
      findsOneWidget,
    );
    expect(
      find.descendant(of: detail, matching: find.text('HTTP status')),
      findsOneWidget,
    );

    await tester.tap(find.byTooltip('Close'));
    await settle(tester);
    expect(find.byType(Dialog), findsNothing);
  });

  testWidgets('a stopped application says why it was not probed', (
    tester,
  ) async {
    await pumpConsole(
      tester,
      source: FakeStatusSource(),
      size: const Size(1400, 1400),
    );

    await tester.tap(systemHeader('heimdall'));
    await settle(tester);
    await tester.tap(
      find.descendant(
        of: find.byKey(
          const ValueKey<String>('application-development-heimdall-api'),
        ),
        matching: find.text('Heimdall API'),
      ),
    );
    await settle(tester);

    final detail = find.byType(ApplicationDetail);
    expect(
      find.descendant(
        of: detail,
        matching: find.text('Not probed: stopped (an on-demand environment).'),
      ),
      findsOneWidget,
    );
    expect(
      find.descendant(of: detail, matching: find.text('Stopped')),
      findsWidgets,
    );
  });

  testWidgets('stopped environments are not problems', (tester) async {
    await pumpConsole(tester, source: FakeStatusSource());

    expect(find.text('Problems only (0)'), findsOneWidget);

    await tester.tap(find.byKey(const ValueKey<String>('problems-only')));
    await settle(tester);

    expect(find.byType(SystemCard), findsNothing);
    expect(find.text('No problems: every system is up.'), findsOneWidget);
  });

  testWidgets(
    'a problem in one environment shows on its chip and sorts first',
    (tester) async {
      await pumpConsole(
        tester,
        source: FakeStatusSource((_, _) async => demoWithAProblem()),
      );

      expect(shownSystems(tester), <String>[
        'fortuna',
        'heimdall',
        'yggdrasil',
      ]);
      expect(chipsIn(tester, card('fortuna')), <String>[
        'Development · Stopped',
        'Homologation · Stopped',
        'Production · Degraded',
      ]);
      expect(
        find.text('3 systems · 3 environments · 1 degraded'),
        findsOneWidget,
      );
      expect(find.text('Problems only (1)'), findsOneWidget);

      await tester.tap(find.byKey(const ValueKey<String>('problems-only')));
      await settle(tester);

      expect(shownSystems(tester), <String>['fortuna']);
    },
  );

  testWidgets('a v1 host shows as one environment', (tester) async {
    await pumpConsole(
      tester,
      source: FakeStatusSource((_, _) async => v1Status()),
    );

    expect(find.byKey(const ValueKey<String>('host-name')), findsOneWidget);
    expect(
      find.text('4 systems · 1 environment · 1 down · 2 degraded'),
      findsOneWidget,
    );
    expect(shownSystems(tester), <String>[
      'huginn',
      'fortuna',
      'yggdrasil',
      'heimdall',
    ]);
    expect(chipsIn(tester, card('huginn')), <String>['Production · Down']);

    await tester.tap(systemHeader('heimdall'));
    await settle(tester);

    expect(find.byType(EnvironmentSection), findsOneWidget);
    expect(find.byType(ApplicationRow), findsNWidgets(2));
    expect(find.text('on demand'), findsNothing);

    await tester.tap(find.byKey(const ValueKey<String>('problems-only')));
    await settle(tester);

    expect(shownSystems(tester), <String>['huginn', 'fortuna', 'yggdrasil']);
  });

  testWidgets('360 dp: the header and the chips fit', (tester) async {
    await pumpConsole(
      tester,
      source: FakeStatusSource((_, _) async => demoWithAProblem()),
      size: const Size(360, 3000),
    );

    await expandAll(tester, <String>['fortuna']);

    expect(find.byType(EnvironmentStatusChip), findsNWidgets(12));
    expect(tester.takeException(), isNull);
  });

  testWidgets('switching host loads that host', (tester) async {
    final source = FakeStatusSource();
    await pumpConsole(tester, source: source);

    await tester.tap(find.text('homologation'));
    await settle(tester);

    expect(source.calls.last.$1, homologation);
  });

  testWidgets('a narrow screen switches hosts with a dropdown', (tester) async {
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
      size: const Size(360, 800),
    );

    expect(
      find.widgetWithText(DropdownButtonFormField<String>, 'Host'),
      findsOneWidget,
    );
    expect(tester.takeException(), isNull);
  });

  testWidgets('a 401 asks for the token, then retries with it', (tester) async {
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
    expect(find.byType(SystemCard), findsNothing);

    await tester.enterText(
      find.byKey(const ValueKey<String>('token-field')),
      'right',
    );
    await tester.tap(find.text('Save'));
    await settle(tester);

    expect(source.calls.last.$2, 'right');
    expect(find.byType(SystemCard), findsNWidgets(3));
    expect(find.byKey(const ValueKey<String>('failure-banner')), findsNothing);
  });

  testWidgets('a network error keeps showing the last data with a warning', (
    tester,
  ) async {
    final source = FakeStatusSource();
    await pumpConsole(tester, source: source);
    expect(find.byKey(const ValueKey<String>('failure-banner')), findsNothing);

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
    expect(find.byType(SystemCard), findsNWidgets(3));
    expect(find.byType(ColorFiltered), findsWidgets);
  });

  testWidgets('a 401 says the host needs a valid token', (tester) async {
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
  });

  testWidgets('503 + Retry-After shows "starting" and retries after it', (
    tester,
  ) async {
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
    expect(find.byKey(const ValueKey<String>('failure-banner')), findsNothing);
    expect(find.text('Token for production'), findsNothing);
    expect(source.calls, hasLength(1));

    source.respond = null;
    await tester.pump(const Duration(seconds: 5));
    await settle(tester);

    expect(source.calls, hasLength(2), reason: 'retried after Retry-After');
    expect(find.byKey(const ValueKey<String>('starting-banner')), findsNothing);
    expect(find.byType(SystemCard), findsNWidgets(3));
    expect(find.byType(ColorFiltered), findsNothing, reason: 'not stale');
  });

  testWidgets('an error with no data yet shows the error and a retry', (
    tester,
  ) async {
    final source = FakeStatusSource(
      (_, _) async => throw const StatusException(
        StatusFailureKind.server,
        statusCode: 503,
      ),
    );
    await pumpConsole(tester, source: source);

    expect(find.text('The status API answered with HTTP 503.'), findsOneWidget);

    source.respond = null;
    await tester.tap(find.text('Retry'));
    await settle(tester);

    expect(find.byType(SystemCard), findsNWidgets(3));
  });

  testWidgets('polls every 30 s while visible, not while hidden', (
    tester,
  ) async {
    final source = FakeStatusSource();
    await pumpConsole(tester, source: source);
    expect(source.calls, hasLength(1));

    await tester.pump(const Duration(seconds: 30));
    await settle(tester);
    expect(source.calls, hasLength(2));

    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.inactive);
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.hidden);
    await tester.pump(const Duration(seconds: 90));
    expect(source.calls, hasLength(2));

    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.inactive);
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
    await settle(tester);
    expect(source.calls, hasLength(3), reason: 'refreshes on return');

    await tester.pump(const Duration(seconds: 30));
    await settle(tester);
    expect(source.calls, hasLength(4));
  });

  testWidgets('the settings screen manages hosts', (tester) async {
    await pumpConsole(tester, source: FakeStatusSource());

    expect(find.byTooltip('Hosts'), findsOneWidget);

    await tester.tap(find.byKey(const ValueKey<String>('settings')));
    await settle(tester);

    expect(find.text('Hosts'), findsOneWidget);
    expect(find.textContaining('nvironment'), findsNothing);

    await tester.tap(find.text('Add host'));
    await settle(tester);

    expect(find.widgetWithText(AlertDialog, 'Add host'), findsOneWidget);

    await tester.tap(find.text('Cancel'));
    await settle(tester);
    await tester.tap(find.byTooltip('Actions for production'));
    await settle(tester);
    await tester.tap(find.text('Remove'));
    await settle(tester);

    expect(
      find.text('The host and its saved token are removed from this device.'),
      findsOneWidget,
    );
  });

  testWidgets('a host chosen in the settings loads on return', (tester) async {
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
    expect(find.byType(SystemCard), findsNWidgets(3));
  });

  testWidgets('with no host, the demo can be opened', (tester) async {
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

    expect(find.byType(SystemCard), findsNWidgets(3));
    expect(tester.takeException(), isNull);
  });

  testWidgets('on the web, the page origin is used and named by the host', (
    tester,
  ) async {
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
  });

  testWidgets('a v1 host is named after its environment', (tester) async {
    await pumpConsole(
      tester,
      source: FakeStatusSource((_, _) async => v1Status()),
      environments: null,
      webOrigin: 'https://yggdrasil.example.com',
    );

    await tester.tap(find.byKey(const ValueKey<String>('settings')));
    await settle(tester);

    expect(find.text('Production'), findsOneWidget);
  });
}
