import 'package:flutter_test/flutter_test.dart';
import 'package:material_ui/material_ui.dart';
import 'package:yggdrasil_console/features/status/domain/status.dart';
import 'package:yggdrasil_console/features/status/domain/status_logic.dart';
import 'package:yggdrasil_console/features/status/presentation/widgets/status_visuals.dart';

Future<void> pumpIn(
  WidgetTester tester,
  Widget child, {
  Brightness brightness = Brightness.light,
}) => tester.pumpWidget(
  MaterialApp(
    theme: ThemeData(brightness: brightness),
    home: Scaffold(body: Center(child: child)),
  ),
);

void main() {
  group('StatusVisual', () {
    test('given every status, when its look is chosen, then each has its own '
        'icon and its label', () {
      final visuals = <Status, StatusVisual>{
        for (final status in Status.values)
          status: StatusVisual.of(status, Brightness.light),
      };

      expect(
        visuals.values.map((visual) => visual.icon).toSet(),
        hasLength(Status.values.length),
      );
      for (final MapEntry(key: status, value: visual) in visuals.entries) {
        expect(visual.label, statusLabel(status));
      }
    });

    test('given a dark theme, when a look is chosen, then the colours change '
        'and the icon and label do not', () {
      for (final status in Status.values) {
        final light = StatusVisual.of(status, Brightness.light);
        final dark = StatusVisual.of(status, Brightness.dark);

        expect(dark.icon, light.icon, reason: status.wire);
        expect(dark.label, light.label);
        expect(dark.foreground, isNot(light.foreground));
        expect(dark.container, isNot(light.container));
      }
    });
  });

  group('kindIcon and kindLabel', () {
    test(
      'given every kind, when its icon is chosen, then each has its own',
      () {
        expect(
          ApplicationKind.values.map(kindIcon).toSet(),
          hasLength(ApplicationKind.values.length),
        );
      },
    );

    test('given every known kind, when labelled, then the wire value is not '
        'used', () {
      expect(
        <String>[
          for (final kind in ApplicationKind.values) kindLabel(kind, 'cron'),
        ],
        <String>['API', 'Web', 'Worker', 'Platform', 'cron'],
      );
    });

    test('given another kind without a wire value, when labelled, then it is '
        'Other', () {
      expect(kindLabel(ApplicationKind.other), 'Other');
    });
  });

  group('StatusIcon', () {
    testWidgets('given a status, when drawn, then its icon names the status '
        'for screen readers', (tester) async {
      await pumpIn(tester, const StatusIcon(Status.degraded, size: 30));

      final icon = tester.widget<Icon>(find.byType(Icon));
      expect(icon.icon, Icons.warning_rounded);
      expect(icon.semanticLabel, 'Degraded');
      expect(icon.size, 30);
      expect(
        icon.color,
        StatusVisual.of(Status.degraded, Brightness.light).foreground,
      );
    });

    testWidgets('given a dark theme, when drawn, then it takes the dark tint', (
      tester,
    ) async {
      await pumpIn(
        tester,
        const StatusIcon(Status.down),
        brightness: Brightness.dark,
      );

      expect(
        tester.widget<Icon>(find.byType(Icon)).color,
        StatusVisual.of(Status.down, Brightness.dark).foreground,
      );
    });
  });

  group('StatusChip', () {
    testWidgets('given a status, when drawn, then it shows the icon and label '
        'and reads as one label', (tester) async {
      final handle = tester.ensureSemantics();

      await pumpIn(tester, const StatusChip(Status.notDeployed));

      expect(find.text('Not deployed'), findsOneWidget);
      expect(find.byIcon(Icons.remove_circle_outline), findsOneWidget);
      expect(find.bySemanticsLabel('Status: Not deployed'), findsOneWidget);

      handle.dispose();
    });
  });
}
