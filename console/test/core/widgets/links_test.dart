import 'package:flutter_test/flutter_test.dart';
import 'package:material_ui/material_ui.dart';
import 'package:yggdrasil_console/core/widgets/links.dart';

import '../../helpers.dart';

Future<void> pumpLink(WidgetTester tester, String url) => tester.pumpWidget(
  MaterialApp(
    home: Scaffold(
      body: Center(
        child: LinkButton(icon: Icons.open_in_new, label: 'Open', url: url),
      ),
    ),
  ),
);

void main() {
  group('LinkButton', () {
    testWidgets('given a link, when drawn, then it shows its label and the '
        'URL on hover', (tester) async {
      await pumpLink(tester, 'https://example.com');

      expect(find.text('Open'), findsOneWidget);
      expect(find.byIcon(Icons.open_in_new), findsOneWidget);
      expect(find.byTooltip('https://example.com'), findsOneWidget);
    });

    testWidgets('given a URL that is not http(s), when tapped, then a snack '
        'bar says it could not be opened', (tester) async {
      await pumpLink(tester, 'mailto:ops@example.com');

      await tester.tap(find.text('Open'));
      await settle(tester);

      expect(
        find.text('Could not open mailto:ops@example.com'),
        findsOneWidget,
      );
    });
  });

  group('openLink', () {
    testWidgets('given no scaffold messenger, when a link cannot be opened, '
        'then nothing breaks', (tester) async {
      late BuildContext context;

      await tester.pumpWidget(
        Directionality(
          textDirection: TextDirection.ltr,
          child: Builder(
            builder: (c) {
              context = c;

              return const SizedBox();
            },
          ),
        ),
      );

      await openLink(context, 'not a url at all');

      expect(tester.takeException(), isNull);
    });
  });
}
