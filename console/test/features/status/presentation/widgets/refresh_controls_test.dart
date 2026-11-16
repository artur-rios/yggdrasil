import 'package:flutter_test/flutter_test.dart';
import 'package:material_ui/material_ui.dart';
import 'package:yggdrasil_console/features/status/domain/refresh_settings.dart';
import 'package:yggdrasil_console/features/status/presentation/widgets/refresh_controls.dart';

import '../../../../helpers.dart';

final Finder refreshButton = find.byKey(const ValueKey<String>('refresh'));

/// Pumps the controls; the lists receive the refreshes and the settings
/// changes they report.
Future<(List<void>, List<RefreshSettings>)> pumpControls(
  WidgetTester tester, {
  DateTime? updatedAt,
  bool loading = false,
  RefreshSettings settings = const RefreshSettings(),
}) async {
  final refreshes = <void>[];
  final changes = <RefreshSettings>[];

  await tester.pumpWidget(
    MaterialApp(
      home: Scaffold(
        body: RefreshControls(
          updatedAt: updatedAt,
          loading: loading,
          settings: settings,
          onRefresh: () => refreshes.add(null),
          onSettingsChanged: changes.add,
        ),
      ),
    ),
  );

  return (refreshes, changes);
}

void main() {
  group('RefreshControls.intervalLabel', () {
    test('given one minute, when labelled, then it is singular', () {
      expect(
        RefreshControls.intervalLabel(const Duration(minutes: 1)),
        'Every 1 minute',
      );
    });

    test('given several minutes, when labelled, then it is plural', () {
      expect(
        <String>[
          for (final interval in autoRefreshIntervals.skip(1))
            RefreshControls.intervalLabel(interval),
        ],
        <String>[
          'Every 5 minutes',
          'Every 10 minutes',
          'Every 15 minutes',
          'Every 30 minutes',
        ],
      );
    });
  });

  group('RefreshControls', () {
    testWidgets('given no update time, when drawn, then only the buttons '
        'show', (tester) async {
      await pumpControls(tester);

      expect(find.textContaining('Updated'), findsNothing);
      expect(find.text('Refresh now'), findsOneWidget);
      expect(find.text('Every 1 minute'), findsOneWidget);
    });

    testWidgets('given an update time, when time passes, then the relative '
        'time keeps counting', (tester) async {
      // Real time: the widget reads DateTime.now(), so the moment is an hour
      // back and only the unit is asserted.
      await pumpControls(
        tester,
        updatedAt: DateTime.now().subtract(const Duration(minutes: 90)),
      );

      expect(find.text('Updated 1 h ago'), findsOneWidget);

      await tester.pump(const Duration(seconds: 1));
      expect(find.text('Updated 1 h ago'), findsOneWidget);
    });

    testWidgets('given no load in flight, when Refresh now is tapped, then it '
        'refreshes', (tester) async {
      final (refreshes, _) = await pumpControls(tester);

      await tester.tap(refreshButton);

      expect(refreshes, hasLength(1));
    });

    testWidgets('given a load in flight, when Refresh now is tapped, then '
        'nothing happens', (tester) async {
      final (refreshes, _) = await pumpControls(tester, loading: true);

      await tester.tap(refreshButton);

      expect(refreshes, isEmpty);
      expect(tester.widget<ButtonStyleButton>(refreshButton).onPressed, isNull);
    });

    testWidgets('given auto-refresh off, when the switch is flipped, then it '
        'reports it on, keeping the interval', (tester) async {
      final (_, changes) = await pumpControls(
        tester,
        settings: const RefreshSettings(interval: Duration(minutes: 10)),
      );

      await tester.tap(find.byKey(const ValueKey<String>('auto-refresh')));

      expect(changes, <RefreshSettings>[
        const RefreshSettings(enabled: true, interval: Duration(minutes: 10)),
      ]);
    });

    testWidgets('given auto-refresh on, when an interval is picked, then it '
        'reports it, keeping the switch', (tester) async {
      final (_, changes) = await pumpControls(
        tester,
        settings: const RefreshSettings(enabled: true),
      );

      await tester.tap(
        find.byKey(const ValueKey<String>('auto-refresh-interval')),
      );
      await settle(tester);
      await tester.tap(find.text('Every 30 minutes'));
      await settle(tester);

      expect(changes, <RefreshSettings>[
        const RefreshSettings(enabled: true, interval: Duration(minutes: 30)),
      ]);
    });
  });
}
