import 'package:flutter/services.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mocktail/mocktail.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:yggdrasil_console/features/environments/data/environment_store.dart';
import 'package:yggdrasil_console/features/environments/domain/environment.dart';

import '../../../helpers.dart';

class _MockSecureStorage extends Mock implements FlutterSecureStorage {}

void main() {
  group('PreferencesEnvironmentStore', () {
    const store = PreferencesEnvironmentStore();

    setUp(() => SharedPreferences.setMockInitialValues(<String, Object>{}));

    test('given nothing saved, when the list is read, then it is null so the '
        'defaults apply', () async {
      expect(await store.readEnvironments(), isNull);
      expect(await store.readSelectedId(), isNull);
    });

    test('given a saved list, when read back, then it is the same list in '
        'order', () async {
      final environments = <Environment>[
        production,
        homologation.copyWith(nameFromResponse: true),
      ];

      await store.writeEnvironments(environments);

      expect(await store.readEnvironments(), environments);
    });

    test('given an empty list saved, when read back, then it is empty, not '
        'null', () async {
      await store.writeEnvironments(const <Environment>[]);

      expect(await store.readEnvironments(), isEmpty);
    });

    test('given a damaged saved value, when the list is read, then it is null '
        'so the defaults apply', () async {
      for (final raw in <String>[
        'not json',
        '{"id":"a"}',
        '[42]',
        '[{"id":"a","name":"a"}]',
      ]) {
        SharedPreferences.setMockInitialValues(<String, Object>{
          'yggdrasil.environments': raw,
        });

        expect(await store.readEnvironments(), isNull, reason: raw);
      }
    });

    test(
      'given a selected id saved, when read back, then it is that id',
      () async {
        await store.writeSelectedId('homologation');

        expect(await store.readSelectedId(), 'homologation');
      },
    );
  });

  group('InMemoryEnvironmentStore', () {
    test('given a list written, when the caller changes its own list, then '
        'the stored copy does not change', () async {
      final store = InMemoryEnvironmentStore();
      final environments = <Environment>[production];

      await store.writeEnvironments(environments);
      environments.add(homologation);

      expect(await store.readEnvironments(), <Environment>[production]);
    });
  });

  group('SecureTokenStore', () {
    test('given a token written, when read, then it is under the '
        "environment's own key", () async {
      FlutterSecureStorage.setMockInitialValues(<String, String>{});
      const storage = FlutterSecureStorage();
      const tokens = SecureTokenStore(storage);

      await tokens.write('production', 'secret');

      expect(await tokens.read('production'), 'secret');
      expect(await tokens.read('homologation'), isNull);
      expect(await storage.readAll(), <String, String>{
        'yggdrasil.token.production': 'secret',
      });
    });

    test('given a saved token, when deleted, then it reads as none', () async {
      FlutterSecureStorage.setMockInitialValues(<String, String>{
        'yggdrasil.token.production': 'secret',
      });
      const tokens = SecureTokenStore(FlutterSecureStorage());

      await tokens.delete('production');

      expect(await tokens.read('production'), isNull);
    });

    test('given storage that can no longer decrypt, when read, then the token '
        'reads as none', () async {
      final storage = _MockSecureStorage();
      when(() => storage.read(key: any(named: 'key')))
          .thenThrow(PlatformException(code: 'decrypt'));

      expect(await SecureTokenStore(storage).read('production'), isNull);
    });
  });

  group('InMemoryTokenStore', () {
    test('given tokens written and deleted, when read, then each reads as '
        'last written', () async {
      final tokens = InMemoryTokenStore(<String, String>{'a': '1'});

      await tokens.write('b', '2');
      await tokens.delete('a');

      expect(await tokens.read('a'), isNull);
      expect(await tokens.read('b'), '2');
    });
  });
}
