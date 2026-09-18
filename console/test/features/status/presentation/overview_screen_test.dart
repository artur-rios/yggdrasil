import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:yggdrasil_console/features/environments/domain/environment.dart';
import 'package:yggdrasil_console/features/status/data/status_source.dart';
import 'package:yggdrasil_console/features/status/presentation/widgets/application_detail.dart';
import 'package:yggdrasil_console/features/status/presentation/widgets/system_card.dart';

import '../../../helpers.dart';

Finder systemHeader(String id) => find.byKey(ValueKey<String>('system-$id'));

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

Future<void> expandAll(WidgetTester tester) async {
  for (final id in <String>['heimdall', 'fortuna', 'huginn', 'yggdrasil']) {
    await tester.ensureVisible(systemHeader(id));
    await tester.tap(systemHeader(id));
    await settle(tester);
  }
}

void main() {
  testWidgets('the overview renders every system, worst first', (tester) async {
    final source = FakeStatusSource();
    await pumpConsole(tester, source: source);

    expect(source.calls.single.$1, production);
    expect(find.text('production'), findsWidgets);
    expect(find.text('Degraded'), findsWidgets);
    expect(
      find.text(
        '4 systems · 13 applications · 1 down · 2 degraded · 1 not deployed',
      ),
      findsOneWidget,
    );
    expect(find.textContaining('updated '), findsOneWidget);

    expect(shownSystems(tester), <String>[
      'huginn',
      'fortuna',
      'yggdrasil',
      'heimdall',
    ]);
    expect(find.text('2 applications · 1 degraded'), findsOneWidget);
    expect(find.text('2 applications · all up'), findsOneWidget);

    // Collapsed: no application rows yet.
    expect(find.byType(ApplicationRow), findsNothing);
  });

  testWidgets('expanding a system shows its applications', (tester) async {
    await pumpConsole(tester, source: FakeStatusSource());

    await tester.tap(systemHeader('heimdall'));
    await settle(tester);

    expect(find.byType(ApplicationRow), findsNWidgets(2));
    expect(find.text('Heimdall API'), findsOneWidget);
    expect(find.text('Heimdall web'), findsOneWidget);
    expect(find.text('v1.4.0 · 3f2a9c1'), findsOneWidget);
    expect(find.text('12 ms'), findsOneWidget);
    expect(find.text('running · healthy · 0 restarts'), findsNWidgets(2));
    expect(find.text('Repository'), findsNWidgets(2));

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
      size: const Size(320, 6000),
    );

    await expandAll(tester);

    expect(find.byType(ApplicationRow), findsNWidgets(13));
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
      size: const Size(1400, 3000),
    );

    await expandAll(tester);

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

    await tester.tap(systemHeader('yggdrasil'));
    await settle(tester);
    // The name, not the row's centre, which may be one of its link buttons.
    await tester.ensureVisible(find.text('Jenkins'));
    await settle(tester);
    await tester.tap(find.text('Jenkins'));
    await settle(tester);

    expect(find.byType(BottomSheet), findsOneWidget);
    expect(find.byType(ApplicationDetail), findsOneWidget);
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
      size: const Size(1400, 1000),
    );

    await tester.tap(systemHeader('heimdall'));
    await settle(tester);
    await tester.tap(find.text('Heimdall API'));
    await settle(tester);

    expect(find.byType(Dialog), findsOneWidget);
    final detail = find.byType(ApplicationDetail);
    expect(
      find.descendant(
        of: detail,
        matching: find.text('heimdall-api:1.4.0-3f2a9c1'),
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

  testWidgets('"problems only" hides systems that are all up', (tester) async {
    await pumpConsole(tester, source: FakeStatusSource());

    expect(find.byType(SystemCard), findsNWidgets(4));

    await tester.tap(find.byKey(const ValueKey<String>('problems-only')));
    await settle(tester);

    expect(shownSystems(tester), <String>['huginn', 'fortuna', 'yggdrasil']);
  });

  testWidgets('switching environment loads that environment', (tester) async {
    final source = FakeStatusSource();
    await pumpConsole(tester, source: source);

    await tester.tap(find.text('homologation'));
    await settle(tester);

    expect(source.calls.last.$1, homologation);
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
    expect(find.byType(SystemCard), findsNWidgets(4));
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
    expect(find.byType(SystemCard), findsNWidgets(4));
    expect(find.byType(ColorFiltered), findsWidgets);
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
    expect(find.byType(SystemCard), findsNWidgets(4));
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

    expect(find.byType(SystemCard), findsNWidgets(4));
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

  testWidgets('with no environment, the demo can be opened', (tester) async {
    await pumpConsole(
      tester,
      source: FakeStatusSource(),
      environments: null,
      size: const Size(320, 700),
    );

    expect(find.text('No environments yet'), findsOneWidget);

    await tester.tap(find.byKey(const ValueKey<String>('open-demo')));
    await settle(tester);

    expect(find.byType(SystemCard), findsNWidgets(4));
    expect(tester.takeException(), isNull);
  });

  testWidgets('on the web, the page origin is used and named by the response', (
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

    // The settings list shows the adopted name.
    expect(find.text('production'), findsOneWidget);
    expect(find.text(Environment.demoUrl), findsNothing);
  });
}
