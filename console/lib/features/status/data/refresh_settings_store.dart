import 'package:shared_preferences/shared_preferences.dart';

import '../domain/refresh_settings.dart';

/// Where the auto-refresh choice, and the environment last looked at on each
/// host, live between launches.
abstract interface class RefreshSettingsStore {
  Future<RefreshSettings> read();

  Future<void> write(RefreshSettings settings);

  /// The id of the host environment (development, production...) last chosen
  /// on the saved host [hostId], or `null` if none was.
  Future<String?> readEnvironmentFor(String hostId);

  Future<void> writeEnvironmentFor(String hostId, String environmentId);
}

class PreferencesRefreshSettingsStore implements RefreshSettingsStore {
  const PreferencesRefreshSettingsStore();

  static const String _enabledKey = 'yggdrasil.refresh.enabled';
  static const String _minutesKey = 'yggdrasil.refresh.minutes';
  static const String _environmentKey = 'yggdrasil.selected-environment.';

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

  @override
  Future<String?> readEnvironmentFor(String hostId) async {
    final preferences = await SharedPreferences.getInstance();

    return preferences.getString('$_environmentKey$hostId');
  }

  @override
  Future<void> writeEnvironmentFor(String hostId, String environmentId) async {
    final preferences = await SharedPreferences.getInstance();
    await preferences.setString('$_environmentKey$hostId', environmentId);
  }
}

class InMemoryRefreshSettingsStore implements RefreshSettingsStore {
  InMemoryRefreshSettingsStore([this.settings = const RefreshSettings()]);

  RefreshSettings settings;

  final Map<String, String> _environments = <String, String>{};

  @override
  Future<RefreshSettings> read() async => settings;

  @override
  Future<void> write(RefreshSettings settings) async =>
      this.settings = settings;

  @override
  Future<String?> readEnvironmentFor(String hostId) async =>
      _environments[hostId];

  @override
  Future<void> writeEnvironmentFor(String hostId, String environmentId) async =>
      _environments[hostId] = environmentId;
}
