import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/config/app_config.dart';
import '../data/environment_store.dart';
import '../domain/environment.dart';

/// Overridden in `main.dart` with the compiled-in configuration.
final Provider<AppConfig> appConfigProvider = Provider<AppConfig>(
  (ref) => const AppConfig(),
);

/// Overridden in `main.dart` with the `shared_preferences` store.
final Provider<EnvironmentStore> environmentStoreProvider =
    Provider<EnvironmentStore>((ref) => InMemoryEnvironmentStore());

/// Overridden in `main.dart` with the secure-storage store.
final Provider<TokenStore> tokenStoreProvider = Provider<TokenStore>(
  (ref) => InMemoryTokenStore(),
);

/// The page's own origin on the web (`Uri.base.origin`), `null` elsewhere.
///
/// When nothing is saved, the console served by an environment shows that
/// environment: Traefik routes `/api/` on the same host to the status API.
final Provider<String?> webOriginProvider = Provider<String?>((ref) => null);

class EnvironmentsState {
  const EnvironmentsState({
    required this.environments,
    required this.selectedId,
    required this.withToken,
  });

  final List<Environment> environments;
  final String? selectedId;

  /// Ids of the environments that have a token saved.
  final Set<String> withToken;

  Environment? get selected =>
      (selectedId == null ? null : byId(selectedId!)) ??
      (environments.isEmpty ? null : environments.first);

  Environment? byId(String id) {
    for (final environment in environments) {
      if (environment.id == id) {
        return environment;
      }
    }

    return null;
  }
}

final AsyncNotifierProvider<EnvironmentsController, EnvironmentsState>
environmentsControllerProvider =
    AsyncNotifierProvider<EnvironmentsController, EnvironmentsState>(
      EnvironmentsController.new,
    );

/// The environments, which one is shown, and which have a token.
class EnvironmentsController extends AsyncNotifier<EnvironmentsState> {
  EnvironmentStore get _store => ref.read(environmentStoreProvider);

  TokenStore get _tokens => ref.read(tokenStoreProvider);

  @override
  Future<EnvironmentsState> build() async {
    final store = ref.watch(environmentStoreProvider);
    final tokens = ref.watch(tokenStoreProvider);
    final environments =
        await store.readEnvironments() ??
        defaultEnvironments(
          ref.watch(appConfigProvider),
          ref.watch(webOriginProvider),
        );
    final selectedId = await store.readSelectedId();
    final withToken = <String>{};

    for (final environment in environments) {
      final token = await tokens.read(environment.id);

      if (token != null && token.isNotEmpty) {
        withToken.add(environment.id);
      }
    }

    return EnvironmentsState(
      environments: environments,
      selectedId: environments.any((e) => e.id == selectedId)
          ? selectedId
          : (environments.isEmpty ? null : environments.first.id),
      withToken: withToken,
    );
  }

  /// What applies when the user never saved a list: the page's own origin on
  /// the web, then the compiled-in environments.
  static List<Environment> defaultEnvironments(
    AppConfig config,
    String? webOrigin,
  ) {
    final environments = List<Environment>.of(config.environments);

    if (webOrigin != null && validateBaseUrl(webOrigin) == null) {
      final baseUrl = normalizeBaseUrl(webOrigin);

      if (!environments.any((environment) => environment.baseUrl == baseUrl)) {
        environments.insert(
          0,
          Environment(
            id: 'url:$baseUrl',
            name: Uri.parse(baseUrl).host,
            url: baseUrl,
            nameFromResponse: true,
          ),
        );
      }
    }

    return environments;
  }

  Future<String?> tokenFor(String environmentId) => _tokens.read(environmentId);

  Future<void> select(String id) async {
    final current = await future;

    if (current.byId(id) == null) {
      return;
    }

    state = AsyncData<EnvironmentsState>(
      EnvironmentsState(
        environments: current.environments,
        selectedId: id,
        withToken: current.withToken,
      ),
    );
    await _store.writeSelectedId(id);
  }

  /// Adds [environment], or replaces the one with its id. A non-null [token]
  /// replaces the saved one; an empty token removes it. A new environment
  /// becomes the selected one.
  Future<void> save(Environment environment, {String? token}) async {
    final current = await future;
    final exists = current.byId(environment.id) != null;
    final environments = <Environment>[
      for (final existing in current.environments)
        existing.id == environment.id ? environment : existing,
      if (!exists) environment,
    ];

    await _store.writeEnvironments(environments);

    final withToken = token == null
        ? current.withToken
        : await _writeToken(environment.id, token, current.withToken);

    state = AsyncData<EnvironmentsState>(
      EnvironmentsState(
        environments: environments,
        selectedId: exists ? current.selectedId : environment.id,
        withToken: withToken,
      ),
    );

    if (!exists) {
      await _store.writeSelectedId(environment.id);
    }
  }

  Future<void> remove(String id) async {
    final current = await future;
    final environments = current.environments
        .where((environment) => environment.id != id)
        .toList();

    await _store.writeEnvironments(environments);
    await _tokens.delete(id);

    final selectedId = current.selectedId == id
        ? (environments.isEmpty ? null : environments.first.id)
        : current.selectedId;

    state = AsyncData<EnvironmentsState>(
      EnvironmentsState(
        environments: environments,
        selectedId: selectedId,
        withToken: current.withToken.difference(<String>{id}),
      ),
    );

    if (selectedId != null) {
      await _store.writeSelectedId(selectedId);
    }
  }

  /// Saves [token] for the environment; an empty one removes it.
  Future<void> setToken(String id, String token) async {
    final current = await future;
    final withToken = await _writeToken(id, token, current.withToken);

    state = AsyncData<EnvironmentsState>(
      EnvironmentsState(
        environments: current.environments,
        selectedId: current.selectedId,
        withToken: withToken,
      ),
    );
  }

  /// Names a placeholder host after the `host` of its first successful
  /// response (a v1 response's environment name). Does nothing for one the
  /// user named.
  Future<void> adoptResponseName(String id, String name) async {
    final current = await future;
    final environment = current.byId(id);

    if (environment == null ||
        !environment.nameFromResponse ||
        name.trim().isEmpty) {
      return;
    }

    await save(
      environment.copyWith(name: name.trim(), nameFromResponse: false),
    );
  }

  /// Adds the offline demo, or selects it when it is already there.
  Future<void> addDemo() async {
    final current = await future;

    if (current.byId(Environment.demoId) == null) {
      await save(Environment.demo());
    } else {
      await select(Environment.demoId);
    }
  }

  Future<Set<String>> _writeToken(
    String id,
    String token,
    Set<String> withToken,
  ) async {
    final trimmed = token.trim();

    if (trimmed.isEmpty) {
      await _tokens.delete(id);

      return withToken.difference(<String>{id});
    }

    await _tokens.write(id, trimmed);

    return <String>{...withToken, id};
  }
}

/// A new, unique environment id.
String newEnvironmentId() =>
    'env-${DateTime.now().microsecondsSinceEpoch.toRadixString(36)}';
