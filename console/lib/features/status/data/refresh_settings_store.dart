import 'package:shared_preferences/shared_preferences.dart';

import '../domain/refresh_settings.dart';

/// Where the auto-refresh choice lives between launches.
abstract interface class RefreshSettingsStore {
  Future<RefreshSettings> read();

  Future<void> write(RefreshSettings settings);
}

class PreferencesRefreshSettingsStore implements RefreshSettingsStore {
  const PreferencesRefreshSettingsStore();

  static const String _enabledKey = 'yggdrasil.refresh.enabled';
  static const String _minutesKey = 'yggdrasil.refresh.minutes';

  @override
  Future<RefreshSettings> read() async {
    final preferences = await SharedPreferences.getInstance();
    final interval = Duration(minutes: preferences.getInt(_minutesKey) ?? 0);

    return RefreshSettings(
      enabled: preferences.getBool(_enabledKey) ?? false,
      // An interval the menu does not offer (an older or damaged value) is
      // the default.
      interval: autoRefreshIntervals.contains(interval)
          ? interval
          : const RefreshSettings().interval,
    );
  }

  @override
  Future<void> write(RefreshSettings settings) async {
    final preferences = await SharedPreferences.getInstance();
    await preferences.setBool(_enabledKey, settings.enabled);
    await preferences.setInt(_minutesKey, settings.interval.inMinutes);
  }
}

class InMemoryRefreshSettingsStore implements RefreshSettingsStore {
  InMemoryRefreshSettingsStore([this.settings = const RefreshSettings()]);

  RefreshSettings settings;

  @override
  Future<RefreshSettings> read() async => settings;

  @override
  Future<void> write(RefreshSettings settings) async =>
      this.settings = settings;
}
