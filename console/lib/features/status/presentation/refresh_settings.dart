import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../data/refresh_settings_store.dart';

/// The intervals auto-refresh offers; the first is the default.
const List<Duration> autoRefreshIntervals = <Duration>[
  Duration(minutes: 1),
  Duration(minutes: 5),
  Duration(minutes: 10),
  Duration(minutes: 15),
  Duration(minutes: 30),
];

/// Whether the overview repeats its load by itself, and how often.
class RefreshSettings {
  const RefreshSettings({
    this.enabled = false,
    this.interval = const Duration(minutes: 1),
  });

  final bool enabled;
  final Duration interval;

  RefreshSettings copyWith({bool? enabled, Duration? interval}) =>
      RefreshSettings(
        enabled: enabled ?? this.enabled,
        interval: interval ?? this.interval,
      );

  @override
  bool operator ==(Object other) =>
      other is RefreshSettings &&
      other.enabled == enabled &&
      other.interval == interval;

  @override
  int get hashCode => Object.hash(enabled, interval);
}

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
