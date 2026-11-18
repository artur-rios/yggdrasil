import 'package:flutter_test/flutter_test.dart';
import 'package:material_ui/material_ui.dart';
import 'package:yggdrasil_console/features/status/domain/status.dart';
import 'package:yggdrasil_console/features/status/presentation/widgets/environment_selector.dart';

import '../../../../helpers.dart';

final Finder selector = find.byKey(
  const ValueKey<String>('environment-selector'),
);

HostEnvironment environment(String id, [Status status = Status.up]) =>
    HostEnvironment(
      id: id,
      name: '${id[0].toUpperCase()}${id.substring(1)}',
      onDemand: false,
      status: status,
    );

const List<String> three = <String>[
  'development',
  'homologation',
  'production',
];

/// Pumps the selector [width] wide; the list receives each choice.
Future<List<String>> pumpSelector(
  WidgetTester tester, {
  List<HostEnvironment>? environments,
  String? selectedId = 'production',
  double width = 800,
}) async {
  final chosen = <String>[];

  await tester.pumpWidget(
    MaterialApp(
      home: Scaffold(
        body: Center(
          child: SizedBox(
            width: width,
            child: EnvironmentSelector(
              environments:
                  environments ??
                  <HostEnvironment>[for (final id in three) environment(id)],
              selectedId: selectedId,
              onSelected: chosen.add,
            ),
          ),
        ),
      ),
    ),
  );

  return chosen;
}

Finder inSelector(Finder finder) =>
    find.descendant(of: selector, matching: finder);

void main() {
  group('EnvironmentSelector', () {
    testWidgets('given room for every choice, when drawn, then it is a '
        'segmented control with the selection marked', (tester) async {
      await pumpSelector(tester);

      expect(tester.widget(selector), isA<SegmentedButton<String>>());
      expect(
        tester.widget<SegmentedButton<String>>(selector).selected,
        <String>{'production'},
      );
    });

    testWidgets('given room for every choice and no selection, when drawn, '
        'then it is a segmented control with nothing marked', (tester) async {
      await pumpSelector(tester, selectedId: null);

      expect(tester.takeException(), isNull);
      expect(tester.widget(selector), isA<SegmentedButton<String>>());
      expect(
        tester.widget<SegmentedButton<String>>(selector).selected,
        isEmpty,
      );
    });

    testWidgets('given no selection, when a segment is tapped, then its '
        'environment is chosen', (tester) async {
      final chosen = await pumpSelector(tester, selectedId: null);

      await tester.tap(inSelector(find.text('Development')));

      expect(chosen, <String>['development']);
    });

    testWidgets('given a selection, when the selected segment is tapped, then '
        'nothing is chosen', (tester) async {
      final chosen = await pumpSelector(tester);

      await tester.tap(inSelector(find.text('Production')));

      expect(chosen, isEmpty);
    });

    testWidgets('given a segment, when tapped, then its environment is '
        'chosen', (tester) async {
      final chosen = await pumpSelector(tester);

      await tester.tap(inSelector(find.text('Homologation')));

      expect(chosen, <String>['homologation']);
    });

    testWidgets('given more than four environments, when drawn even on a wide '
        'screen, then it is a dropdown', (tester) async {
      await pumpSelector(
        tester,
        environments: <HostEnvironment>[
          for (final id in <String>['a', 'b', 'c', 'd', 'e']) environment(id),
        ],
        selectedId: 'e',
        width: 1600,
      );

      expect(find.byType(SegmentedButton<String>), findsNothing);
      expect(inSelector(find.byType(DropdownButton<String>)), findsOneWidget);
    });

    testWidgets('given too little room, when drawn, then it is a dropdown '
        'showing the selection', (tester) async {
      await pumpSelector(tester, width: 300);

      expect(inSelector(find.byType(DropdownButton<String>)), findsOneWidget);
      expect(inSelector(find.text('Production')), findsOneWidget);
      expect(find.text('Environment'), findsOneWidget);
    });

    testWidgets('given the dropdown, when another environment is picked, then '
        'it is chosen', (tester) async {
      final chosen = await pumpSelector(tester, width: 300);

      await tester.tap(selector);
      await settle(tester);
      await tester.tap(find.text('Development').last);
      await settle(tester);

      expect(chosen, <String>['development']);
    });

    testWidgets('given environments of different statuses, when drawn, then '
        'each mark has its status shape and name', (tester) async {
      await pumpSelector(
        tester,
        environments: <HostEnvironment>[
          environment('development', Status.stopped),
          environment('production', Status.down),
        ],
      );

      final marks = tester
          .widgetList<Icon>(inSelector(find.byType(Icon)))
          .map((icon) => (icon.icon, icon.semanticLabel))
          .toList();
      expect(marks, <(IconData?, String?)>[
        (Icons.pause_circle_outline, 'Stopped'),
        (Icons.cancel, 'Down'),
      ]);
    });
  });
}
