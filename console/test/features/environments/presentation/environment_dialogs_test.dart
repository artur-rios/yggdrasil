import 'package:flutter_test/flutter_test.dart';
import 'package:material_ui/material_ui.dart';
import 'package:yggdrasil_console/features/environments/presentation/environment_dialogs.dart';

import '../../../helpers.dart';

final Finder nameField = find.byKey(const ValueKey<String>('environment-name'));
final Finder urlField = find.byKey(const ValueKey<String>('environment-url'));
final Finder tokenField = find.byKey(
  const ValueKey<String>('environment-token'),
);

/// Opens a dialog with [open] from a button; the returned list receives what
/// the dialog returned once it closes.
Future<List<T?>> pumpOpener<T>(
  WidgetTester tester,
  Future<T?> Function(BuildContext context) open,
) async {
  final results = <T?>[];

  await tester.pumpWidget(
    MaterialApp(
      home: Builder(
        builder: (context) => Scaffold(
          body: TextButton(
            onPressed: () async => results.add(await open(context)),
            child: const Text('open'),
          ),
        ),
      ),
    ),
  );
  await tester.tap(find.text('open'));
  await settle(tester);

  return results;
}

bool obscured(WidgetTester tester, Finder field) => tester
    .widget<EditableText>(
      find.descendant(of: field, matching: find.byType(EditableText)),
    )
    .obscureText;

void main() {
  group('showTokenDialog', () {
    testWidgets('given a rejected request, when the dialog opens, then it '
        'says the token was rejected', (tester) async {
      await pumpOpener<String>(
        tester,
        (context) => showTokenDialog(context, production, rejected: true),
      );

      expect(find.text('Token for production'), findsOneWidget);
      expect(find.textContaining('rejected the request (401)'), findsOneWidget);
      expect(
        find.textContaining('https://yggdrasil.example.com'),
        findsOneWidget,
      );
    });

    testWidgets('given no rejection, when the dialog opens, then it only '
        "names the host's token", (tester) async {
      await pumpOpener<String>(
        tester,
        (context) => showTokenDialog(context, production),
      );

      expect(
        find.text(
          'The YGGDRASIL_STATUS_TOKEN of https://yggdrasil.example.com.',
        ),
        findsOneWidget,
      );
      expect(find.textContaining('rejected'), findsNothing);
    });

    testWidgets('given a token with blanks, when saved, then the dialog '
        'returns it trimmed', (tester) async {
      final results = await pumpOpener<String>(
        tester,
        (context) => showTokenDialog(context, production),
      );

      await tester.enterText(
        find.byKey(const ValueKey<String>('token-field')),
        '  secret  ',
      );
      await tester.tap(find.text('Save'));
      await settle(tester);

      expect(results, <String?>['secret']);
    });

    testWidgets('given a token, when submitted from the keyboard, then the '
        'dialog returns it', (tester) async {
      final results = await pumpOpener<String>(
        tester,
        (context) => showTokenDialog(context, production),
      );

      await tester.enterText(
        find.byKey(const ValueKey<String>('token-field')),
        'secret',
      );
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await settle(tester);

      expect(results, <String?>['secret']);
    });

    testWidgets('given a blank token, when saved, then the dialog stays open', (
      tester,
    ) async {
      final results = await pumpOpener<String>(
        tester,
        (context) => showTokenDialog(context, production),
      );

      await tester.enterText(
        find.byKey(const ValueKey<String>('token-field')),
        '   ',
      );
      await tester.tap(find.text('Save'));
      await settle(tester);

      expect(results, isEmpty);
      expect(find.byType(AlertDialog), findsOneWidget);
    });

    testWidgets('given the dialog, when cancelled, then it returns null', (
      tester,
    ) async {
      final results = await pumpOpener<String>(
        tester,
        (context) => showTokenDialog(context, production),
      );

      await tester.tap(find.text('Cancel'));
      await settle(tester);

      expect(results, <String?>[null]);
    });

    testWidgets('given a hidden token, when Show token is pressed, then it is '
        'shown, and hidden again on Hide token', (tester) async {
      await pumpOpener<String>(
        tester,
        (context) => showTokenDialog(context, production),
      );
      final field = find.byKey(const ValueKey<String>('token-field'));

      expect(obscured(tester, field), isTrue);

      await tester.tap(find.byTooltip('Show token'));
      await tester.pump();
      expect(obscured(tester, field), isFalse);

      await tester.tap(find.byTooltip('Hide token'));
      await tester.pump();
      expect(obscured(tester, field), isTrue);
    });
  });

  group('showEnvironmentEditor', () {
    testWidgets('given a new host, when the editor opens, then it is Add host '
        'with empty fields and no forget option', (tester) async {
      await pumpOpener<EnvironmentEdit>(tester, showEnvironmentEditor);

      expect(find.widgetWithText(AlertDialog, 'Add host'), findsOneWidget);
      expect(
        find.text('Stored in secure storage on this device.'),
        findsOneWidget,
      );
      expect(find.text('Forget the saved token'), findsNothing);
    });

    testWidgets('given an empty name and an invalid URL, when saved, then '
        'both are flagged and the dialog stays open', (tester) async {
      final results = await pumpOpener<EnvironmentEdit>(
        tester,
        showEnvironmentEditor,
      );

      await tester.enterText(urlField, 'yggdrasil.example.com');
      await tester.tap(find.text('Save'));
      await settle(tester);

      expect(find.text('Enter a name'), findsOneWidget);
      expect(find.textContaining('Enter an http(s) URL'), findsOneWidget);
      expect(results, isEmpty);
    });

    testWidgets('given a valid new host, when saved, then it returns a new '
        'environment with the trimmed name, normalised URL and token', (
      tester,
    ) async {
      final results = await pumpOpener<EnvironmentEdit>(
        tester,
        showEnvironmentEditor,
      );

      await tester.enterText(nameField, '  vps  ');
      await tester.enterText(urlField, ' https://yggdrasil.example.com/ ');
      await tester.enterText(tokenField, ' secret ');
      await tester.tap(find.text('Save'));
      await settle(tester);

      final edit = results.single!;
      expect(edit.environment.id, startsWith('env-'));
      expect(edit.environment.name, 'vps');
      expect(edit.environment.url, 'https://yggdrasil.example.com');
      expect(edit.environment.nameFromResponse, isFalse);
      expect(edit.token, 'secret');
    });

    testWidgets('given an existing host with a token, when saved untouched, '
        'then it keeps its id and the token is kept (null)', (tester) async {
      final results = await pumpOpener<EnvironmentEdit>(
        tester,
        (context) => showEnvironmentEditor(
          context,
          existing: production,
          hasToken: true,
        ),
      );

      expect(find.widgetWithText(AlertDialog, 'Edit host'), findsOneWidget);
      expect(
        find.text('A token is saved. Leave empty to keep it.'),
        findsOneWidget,
      );

      await tester.tap(find.text('Save'));
      await settle(tester);

      expect(results.single!.environment, production);
      expect(results.single!.token, isNull);
    });

    testWidgets('given an existing host with a token, when Forget the saved '
        'token is ticked, then the token returned is empty', (tester) async {
      final results = await pumpOpener<EnvironmentEdit>(
        tester,
        (context) => showEnvironmentEditor(
          context,
          existing: production,
          hasToken: true,
        ),
      );

      await tester.tap(find.text('Forget the saved token'));
      await tester.pump();
      await tester.tap(find.text('Save'));
      await settle(tester);

      expect(results.single!.token, '');
    });

    testWidgets('given forget ticked and a new token typed, when saved, then '
        'the new token wins', (tester) async {
      final results = await pumpOpener<EnvironmentEdit>(
        tester,
        (context) => showEnvironmentEditor(
          context,
          existing: production,
          hasToken: true,
        ),
      );

      await tester.tap(find.text('Forget the saved token'));
      await tester.enterText(tokenField, 'new');
      await tester.tap(find.text('Save'));
      await settle(tester);

      expect(results.single!.token, 'new');
    });

    testWidgets('given a placeholder host, when saved with its name '
        'unchanged, then it stays a placeholder', (tester) async {
      final placeholder = production.copyWith(nameFromResponse: true);
      final results = await pumpOpener<EnvironmentEdit>(
        tester,
        (context) => showEnvironmentEditor(context, existing: placeholder),
      );

      await tester.tap(find.text('Save'));
      await settle(tester);

      expect(results.single!.environment.nameFromResponse, isTrue);
    });

    testWidgets('given a placeholder host, when the user types a name, then '
        'the name is theirs to keep', (tester) async {
      final results = await pumpOpener<EnvironmentEdit>(
        tester,
        (context) => showEnvironmentEditor(
          context,
          existing: production.copyWith(nameFromResponse: true),
        ),
      );

      await tester.enterText(nameField, 'vps');
      await tester.tap(find.text('Save'));
      await settle(tester);

      final environment = results.single!.environment;
      expect(environment.id, production.id);
      expect(environment.name, 'vps');
      expect(environment.nameFromResponse, isFalse);
    });

    testWidgets('given the editor, when cancelled, then it returns null', (
      tester,
    ) async {
      final results = await pumpOpener<EnvironmentEdit>(
        tester,
        showEnvironmentEditor,
      );

      await tester.tap(find.text('Cancel'));
      await settle(tester);

      expect(results, <EnvironmentEdit?>[null]);
    });

    testWidgets('given a hidden token, when Show token is pressed, then the '
        'token is shown', (tester) async {
      await pumpOpener<EnvironmentEdit>(tester, showEnvironmentEditor);

      expect(obscured(tester, tokenField), isTrue);

      await tester.tap(find.byTooltip('Show token'));
      await tester.pump();

      expect(obscured(tester, tokenField), isFalse);
    });
  });
}
