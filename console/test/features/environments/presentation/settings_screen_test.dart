import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:material_ui/material_ui.dart';
import 'package:yggdrasil_console/features/environments/domain/environment.dart';
import 'package:yggdrasil_console/features/environments/presentation/environments_controller.dart';
import 'package:yggdrasil_console/features/environments/presentation/settings_screen.dart';
import 'package:yggdrasil_console/features/status/presentation/status_controller.dart';

import '../../../helpers.dart';

/// Pumps the console and opens the hosts screen.
Future<ProviderContainer> openHosts(
  WidgetTester tester, {
  List<Environment>? environments = const <Environment>[
    production,
    homologation,
  ],
  Map<String, String>? tokens,
}) async {
  await pumpConsole(
    tester,
    source: FakeStatusSource(),
    environments: environments,
    tokens: tokens,
  );
  await tester.tap(find.byKey(const ValueKey<String>('settings')));
  await settle(tester);

  return ProviderScope.containerOf(tester.element(find.byType(SettingsScreen)));
}

List<String> savedNames(ProviderContainer container) => container
    .read(environmentsControllerProvider)
    .requireValue
    .environments
    .map((environment) => environment.name)
    .toList();

Future<void> openActions(WidgetTester tester, String name) async {
  await tester.tap(find.byTooltip('Actions for $name'));
  await settle(tester);
}

void main() {
  testWidgets('given the hosts screen, when Add host and Remove are used, '
      'then the editor and the removal confirmation open', (tester) async {
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
    await openActions(tester, 'production');
    await tester.tap(find.text('Remove'));
    await settle(tester);

    expect(
      find.text('The host and its saved token are removed from this device.'),
      findsOneWidget,
    );
  });

  testWidgets('given saved hosts, when the screen opens, then each shows its '
      'URL and whether a token is saved', (tester) async {
    await openHosts(tester, tokens: <String, String>{'production': 'secret'});

    expect(
      find.text('https://yggdrasil.example.com\nToken saved'),
      findsOneWidget,
    );
    expect(
      find.text('https://yggdrasil.hml.example.com\nNo token'),
      findsOneWidget,
    );
  });

  testWidgets('given no hosts, when the screen opens, then it says there are '
      'none', (tester) async {
    await pumpConsole(
      tester,
      source: FakeStatusSource(),
      environments: const <Environment>[],
    );
    await tester.tap(find.text('Manage hosts'));
    await settle(tester);

    expect(find.text('No hosts'), findsOneWidget);
    expect(find.text('Offline demo'), findsOneWidget);
  });

  testWidgets('given a new host entered, when saved, then it is listed and '
      'selected', (tester) async {
    final container = await openHosts(tester);

    await tester.tap(find.text('Add host'));
    await settle(tester);
    await tester.enterText(
      find.byKey(const ValueKey<String>('environment-name')),
      'vps',
    );
    await tester.enterText(
      find.byKey(const ValueKey<String>('environment-url')),
      'https://vps.example.com',
    );
    await tester.tap(find.text('Save'));
    await settle(tester);

    expect(savedNames(container), <String>[
      'production',
      'homologation',
      'vps',
    ]);
    expect(
      container
          .read(environmentsControllerProvider)
          .requireValue
          .selected!
          .name,
      'vps',
    );
    expect(find.text('https://vps.example.com\nNo token'), findsOneWidget);
  });

  testWidgets('given a host, when its URL is edited, then what was remembered '
      'of the old URL is forgotten', (tester) async {
    final container = await openHosts(tester);
    final repository = container.read(statusRepositoryProvider);
    expect(repository.lastFor(production), isNotNull);

    await openActions(tester, 'production');
    await tester.tap(find.text('Edit'));
    await settle(tester);
    await tester.enterText(
      find.byKey(const ValueKey<String>('environment-url')),
      'https://yggdrasil.new.example.com',
    );
    await tester.tap(find.text('Save'));
    await settle(tester);

    expect(repository.lastFor(production), isNull);
    expect(
      find.text('https://yggdrasil.new.example.com\nNo token'),
      findsOneWidget,
    );
  });

  testWidgets('given a host, when only its name is edited, then what was '
      'remembered of it is kept', (tester) async {
    final container = await openHosts(tester);
    final repository = container.read(statusRepositoryProvider);

    // A tap on the tile edits too.
    await tester.tap(find.text('production'));
    await settle(tester);
    await tester.enterText(
      find.byKey(const ValueKey<String>('environment-name')),
      'prod',
    );
    await tester.tap(find.text('Save'));
    await settle(tester);

    expect(savedNames(container), <String>['prod', 'homologation']);
    expect(repository.lastFor(production.copyWith(name: 'prod')), isNotNull);
  });

  testWidgets('given the editor, when cancelled, then nothing changes', (
    tester,
  ) async {
    final container = await openHosts(tester);

    await openActions(tester, 'homologation');
    await tester.tap(find.text('Edit'));
    await settle(tester);
    await tester.tap(find.text('Cancel'));
    await settle(tester);

    expect(savedNames(container), <String>['production', 'homologation']);
  });

  testWidgets('given a removal confirmed, when done, then the host is gone '
      'and so is what was remembered of it', (tester) async {
    final container = await openHosts(tester);
    final repository = container.read(statusRepositoryProvider);

    await openActions(tester, 'production');
    await tester.tap(find.text('Remove'));
    await settle(tester);
    expect(find.text('Remove production?'), findsOneWidget);
    await tester.tap(find.widgetWithText(FilledButton, 'Remove'));
    await settle(tester);

    expect(savedNames(container), <String>['homologation']);
    expect(repository.lastFor(production), isNull);
    expect(find.text('production'), findsNothing);
  });

  testWidgets('given a removal cancelled, when done, then the host stays', (
    tester,
  ) async {
    final container = await openHosts(tester);

    await openActions(tester, 'production');
    await tester.tap(find.text('Remove'));
    await settle(tester);
    await tester.tap(find.text('Cancel'));
    await settle(tester);

    expect(savedNames(container), <String>['production', 'homologation']);
  });

  testWidgets('given the demo in the list, when its actions open, then it '
      'can only be removed', (tester) async {
    await openHosts(
      tester,
      environments: <Environment>[production, Environment.demo()],
    );

    // Its subtitle, beside the "Offline demo" entry that adds it.
    expect(find.text('Offline demo'), findsNWidgets(2));

    await openActions(tester, 'demo');

    expect(find.text('Remove'), findsOneWidget);
    expect(find.text('Edit'), findsNothing);
  });

  testWidgets('given the offline demo entry, when tapped, then the demo is '
      'added and the screen closes', (tester) async {
    final container = await openHosts(tester);

    await tester.tap(find.text('Offline demo'));
    await settle(tester);

    expect(find.byType(SettingsScreen), findsNothing);
    expect(
      container.read(environmentsControllerProvider).requireValue.selected,
      Environment.demo(),
    );
  });
}
