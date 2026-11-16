import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:yggdrasil_console/features/status/data/refresh_settings_store.dart';
import 'package:yggdrasil_console/features/status/domain/refresh_settings.dart';

void main() {
  group('PreferencesRefreshSettingsStore', () {
    const store = PreferencesRefreshSettingsStore();

    setUp(() => SharedPreferences.setMockInitialValues(<String, Object>{}));

    test('given nothing saved, when read, then auto-refresh is off every '
        'minute', () async {
      expect(await store.read(), const RefreshSettings());
    });

    test(
      'given settings written, when read back, then they are the same',
      () async {
        const settings = RefreshSettings(
          enabled: true,
          interval: Duration(minutes: 15),
        );

        await store.write(settings);

        expect(await store.read(), settings);
      },
    );

    test('given an interval the menu does not offer, when read, then it is '
        'the default, and the switch is kept', () async {
      SharedPreferences.setMockInitialValues(<String, Object>{
        'yggdrasil.refresh.enabled': true,
        'yggdrasil.refresh.minutes': 7,
      });

      expect(
        await store.read(),
        const RefreshSettings(enabled: true, interval: Duration(minutes: 1)),
      );
    });

    test(
      'given each offered interval saved, when read, then it is kept',
      () async {
        for (final interval in autoRefreshIntervals) {
          SharedPreferences.setMockInitialValues(<String, Object>{
            'yggdrasil.refresh.minutes': interval.inMinutes,
          });

          expect((await store.read()).interval, interval);
        }
      },
    );

    test('given environments chosen on two hosts, when read, then each host '
        'has its own, and an unknown host none', () async {
      await store.writeEnvironmentFor('a', 'development');
      await store.writeEnvironmentFor('b', 'production');
      await store.writeEnvironmentFor('a', 'homologation');

      expect(await store.readEnvironmentFor('a'), 'homologation');
      expect(await store.readEnvironmentFor('b'), 'production');
      expect(await store.readEnvironmentFor('c'), isNull);
    });
  });

  group('InMemoryRefreshSettingsStore', () {
    test('given initial settings, when read and written, then it answers '
        'what it holds', () async {
      final store = InMemoryRefreshSettingsStore(
        const RefreshSettings(enabled: true),
      );

      expect(await store.read(), const RefreshSettings(enabled: true));

      await store.write(const RefreshSettings());

      expect(store.settings, const RefreshSettings());
    });

    test('given environments chosen on two hosts, when read, then each host '
        'has its own', () async {
      final store = InMemoryRefreshSettingsStore();

      await store.writeEnvironmentFor('a', 'development');

      expect(await store.readEnvironmentFor('a'), 'development');
      expect(await store.readEnvironmentFor('b'), isNull);
    });
  });
}
