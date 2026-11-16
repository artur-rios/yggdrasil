import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_riverpod/misc.dart' show Override;
import 'package:flutter_test/flutter_test.dart';
import 'package:yggdrasil_console/features/status/data/refresh_settings_store.dart';
import 'package:yggdrasil_console/features/status/presentation/refresh_settings.dart';

/// A store whose saved settings are held until [answer] completes.
class SlowRefreshSettingsStore extends InMemoryRefreshSettingsStore {
  SlowRefreshSettingsStore(super.settings);

  final Completer<void> answer = Completer<void>();

  @override
  Future<RefreshSettings> read() async {
    await answer.future;

    return super.read();
  }
}

ProviderContainer containerOver(RefreshSettingsStore store) {
  final container = ProviderContainer(
    overrides: <Override>[
      refreshSettingsStoreProvider.overrideWithValue(store),
    ],
  );
  addTearDown(container.dispose);
  container.listen(refreshSettingsProvider, (_, _) {});

  return container;
}

const RefreshSettings everyQuarterHour = RefreshSettings(
  enabled: true,
  interval: Duration(minutes: 15),
);

void main() {
  group('refreshSettingsProvider', () {
    test('given nothing saved, when read, then auto-refresh is off every '
        'minute', () async {
      final container = containerOver(InMemoryRefreshSettingsStore());
      await pumpEventQueue();

      expect(container.read(refreshSettingsProvider), const RefreshSettings());
    });

    test('given saved settings, when read before and after they load, then '
        'they are off first and the saved ones after', () async {
      final store = SlowRefreshSettingsStore(everyQuarterHour);
      final container = containerOver(store);

      expect(container.read(refreshSettingsProvider), const RefreshSettings());

      store.answer.complete();
      await pumpEventQueue();

      expect(container.read(refreshSettingsProvider), everyQuarterHour);
    });

    test('given a choice made before the saved settings load, when they '
        'load, then the choice wins', () async {
      final store = SlowRefreshSettingsStore(everyQuarterHour);
      final container = containerOver(store);

      await container
          .read(refreshSettingsProvider.notifier)
          .update(const RefreshSettings(interval: Duration(minutes: 5)));
      store.answer.complete();
      await pumpEventQueue();

      expect(
        container.read(refreshSettingsProvider),
        const RefreshSettings(interval: Duration(minutes: 5)),
      );
    });

    test('given a choice saved, when the app starts again, then it is '
        'restored', () async {
      final store = InMemoryRefreshSettingsStore();
      final first = containerOver(store);

      await first
          .read(refreshSettingsProvider.notifier)
          .update(everyQuarterHour);

      expect(store.settings, everyQuarterHour);

      final second = containerOver(store);
      await pumpEventQueue();

      expect(second.read(refreshSettingsProvider), everyQuarterHour);
    });

    test('given no override, when the store is read, then it is an in-memory '
        'one', () {
      final container = ProviderContainer();
      addTearDown(container.dispose);

      expect(
        container.read(refreshSettingsStoreProvider),
        isA<InMemoryRefreshSettingsStore>(),
      );
    });
  });
}
