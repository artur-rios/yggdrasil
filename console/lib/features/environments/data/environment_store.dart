import 'dart:convert';

import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../domain/environment.dart';

/// Where the list of environments and the selected one live between launches.
///
/// Not sensitive, so `shared_preferences`; tokens go to [TokenStore].
abstract interface class EnvironmentStore {
  /// The saved list, or `null` when the user never saved one (which is what
  /// makes the compiled-in and same-origin defaults apply).
  Future<List<Environment>?> readEnvironments();

  Future<void> writeEnvironments(List<Environment> environments);

  Future<String?> readSelectedId();

  Future<void> writeSelectedId(String id);
}

class PreferencesEnvironmentStore implements EnvironmentStore {
  const PreferencesEnvironmentStore();

  static const String _environmentsKey = 'yggdrasil.environments';
  static const String _selectedKey = 'yggdrasil.environments.selected';

  @override
  Future<List<Environment>?> readEnvironments() async {
    final preferences = await SharedPreferences.getInstance();
    final raw = preferences.getString(_environmentsKey);

    if (raw == null) {
      return null;
    }

    try {
      return <Environment>[
        for (final entry in jsonDecode(raw) as List<dynamic>)
          Environment.fromJson(entry as Map<String, dynamic>),
      ];
    } on Object {
      // Unreadable (an older or damaged value): fall back to the defaults
      // rather than failing every launch.
      return null;
    }
  }

  @override
  Future<void> writeEnvironments(List<Environment> environments) async {
    final preferences = await SharedPreferences.getInstance();
    await preferences.setString(
      _environmentsKey,
      jsonEncode(<Map<String, dynamic>>[
        for (final environment in environments) environment.toJson(),
      ]),
    );
  }

  @override
  Future<String?> readSelectedId() async =>
      (await SharedPreferences.getInstance()).getString(_selectedKey);

  @override
  Future<void> writeSelectedId(String id) async {
    await (await SharedPreferences.getInstance()).setString(_selectedKey, id);
  }
}

class InMemoryEnvironmentStore implements EnvironmentStore {
  InMemoryEnvironmentStore({this.environments, this.selectedId});

  List<Environment>? environments;
  String? selectedId;

  @override
  Future<List<Environment>?> readEnvironments() async => environments;

  @override
  Future<void> writeEnvironments(List<Environment> environments) async =>
      this.environments = List<Environment>.of(environments);

  @override
  Future<String?> readSelectedId() async => selectedId;

  @override
  Future<void> writeSelectedId(String id) async => selectedId = id;
}

/// Each environment's bearer token, keyed by environment id.
abstract interface class TokenStore {
  Future<String?> read(String environmentId);

  Future<void> write(String environmentId, String token);

  Future<void> delete(String environmentId);
}

/// Keystore-backed on Android, DPAPI-encrypted per Windows user on Windows,
/// and WebCrypto-encrypted local storage on the web.
class SecureTokenStore implements TokenStore {
  const SecureTokenStore(this._storage);

  final FlutterSecureStorage _storage;

  static String _key(String environmentId) => 'yggdrasil.token.$environmentId';

  @override
  Future<String?> read(String environmentId) async {
    try {
      return await _storage.read(key: _key(environmentId));
    } on Object {
      // A value the platform can no longer decrypt (e.g. keys reset) is as
      // good as none: the next 401 asks for the token again.
      return null;
    }
  }

  @override
  Future<void> write(String environmentId, String token) =>
      _storage.write(key: _key(environmentId), value: token);

  @override
  Future<void> delete(String environmentId) =>
      _storage.delete(key: _key(environmentId));
}

class InMemoryTokenStore implements TokenStore {
  InMemoryTokenStore([Map<String, String>? tokens])
    : tokens = tokens ?? <String, String>{};

  final Map<String, String> tokens;

  @override
  Future<String?> read(String environmentId) async => tokens[environmentId];

  @override
  Future<void> write(String environmentId, String token) async =>
      tokens[environmentId] = token;

  @override
  Future<void> delete(String environmentId) async =>
      tokens.remove(environmentId);
}
