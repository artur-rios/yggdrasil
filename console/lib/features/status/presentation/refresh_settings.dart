import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../data/refresh_settings_store.dart';
import '../domain/refresh_settings.dart';

// Re-exported so the screens and tests need one import.
export '../domain/refresh_settings.dart';

/// Overridden in `main.dart` with the preferences-backed store.
final Provider<RefreshSettingsStore> refreshSettingsStoreProvider =
    Provider<RefreshSettingsStore>((ref) => InMemoryRefreshSettingsStore());

final NotifierProvider<RefreshSettingsController, RefreshSettings>
refreshSettingsProvider =
    NotifierProvider<RefreshSettingsController, RefreshSettings>(
      RefreshSettingsController.new,
    );

/// The saved choice, off until it has been read.
class RefreshSettingsController extends Notifier<RefreshSettings> {
  /// The user (or a test) chose something before the saved value was read.
  bool _changed = false;

  @override
  RefreshSettings build() {
    unawaited(_load());

    return const RefreshSettings();
  }

  Future<void> _load() async {
    final saved = await ref.read(refreshSettingsStoreProvider).read();

    if (ref.mounted && !_changed) {
      state = saved;
    }
  }

  Future<void> update(RefreshSettings settings) async {
    _changed = true;
    state = settings;
    await ref.read(refreshSettingsStoreProvider).write(settings);
  }
}
