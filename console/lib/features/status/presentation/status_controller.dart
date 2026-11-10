import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_riverpod/legacy.dart';

import '../../environments/domain/environment.dart';
import '../../environments/presentation/environments_controller.dart';
import '../data/status_repository.dart';
import '../data/status_source.dart';

/// Overridden in `main.dart` with the HTTP-backed repository, and in tests
/// with one over a fake source.
final Provider<StatusRepository> statusRepositoryProvider =
    Provider<StatusRepository>(
      (ref) => throw UnimplementedError(
        'statusRepositoryProvider must be overridden',
      ),
    );

/// What the overview shows for the selected host (a saved [Environment]).
class StatusViewState {
  const StatusViewState({
    this.environment,
    this.snapshot,
    this.failure,
    this.loading = false,
  });

  final Environment? environment;

  /// The latest good data, possibly older than the last attempt.
  final StatusSnapshot? snapshot;

  /// Why the last attempt failed, or `null` when it succeeded.
  final StatusException? failure;

  /// A request is in flight.
  final bool loading;

  /// The last refresh failed, so what is shown may be out of date. The API
  /// starting up is not a failure of that kind.
  bool get isStale =>
      failure != null && !failure!.isStarting && snapshot != null;

  bool get isStarting => failure?.isStarting ?? false;

  bool get needsToken => failure?.kind == StatusFailureKind.unauthorized;
}

final NotifierProvider<StatusController, StatusViewState>
statusControllerProvider = NotifierProvider<StatusController, StatusViewState>(
  StatusController.new,
);

/// Loads the selected host's status on demand.
///
/// Each load is the map (`GET`) and then the details (`POST`). When to load
/// belongs to the overview screen, which knows whether it is visible; this
/// only fetches when asked, and never twice at once.
class StatusController extends Notifier<StatusViewState> {
  String? _loadedKey;

  @override
  StatusViewState build() {
    final environment = ref.watch(
      environmentsControllerProvider.select(
        (environments) => environments.value?.selected,
      ),
    );

    if (environment == null) {
      _loadedKey = null;

      return const StatusViewState();
    }

    // A rename rebuilds this too; only a different environment or URL needs a
    // new request.
    final key = '${environment.id}|${environment.baseUrl}';

    if (key != _loadedKey) {
      _loadedKey = key;
      Future<void>.microtask(refresh);
    }

    return StatusViewState(
      environment: environment,
      snapshot: ref.read(statusRepositoryProvider).lastFor(environment),
    );
  }

  Future<void> refresh() async {
    final environment = state.environment;

    if (environment == null || state.loading) {
      return;
    }

    state = StatusViewState(
      environment: environment,
      snapshot: state.snapshot,
      failure: state.failure,
      loading: true,
    );

    final environments = ref.read(environmentsControllerProvider.notifier);
    final repository = ref.read(statusRepositoryProvider);
    final token = await environments.tokenFor(environment.id);
    final map = await repository.refresh(environment, token);

    // The user may have switched environments, or pointed this one at another
    // URL, meanwhile: the answer is not about what the screen shows any more.
    if (!_isShowing(environment)) {
      return;
    }

    // The map is drawn at once; `loading` holds until the details are in.
    state = StatusViewState(
      environment: state.environment,
      snapshot: map.snapshot,
      failure: map.failure,
      loading: map.failure == null,
    );

    if (map.failure != null) {
      return;
    }

    final mapSnapshot = map.snapshot;

    if (mapSnapshot != null) {
      await environments.adoptResponseName(
        environment.id,
        mapSnapshot.status.host,
      );
    }

    final details = await repository.refreshDetails(environment, token);

    if (!_isShowing(environment)) {
      return;
    }

    // No snapshot means no endpoint and nothing known: the map stays as it
    // was. A failure keeps the map too, marked stale.
    state = StatusViewState(
      environment: state.environment,
      snapshot: details.snapshot ?? state.snapshot,
      failure: details.failure,
    );
  }

  bool _isShowing(Environment environment) =>
      state.environment?.id == environment.id &&
      state.environment?.baseUrl == environment.baseUrl;
}

/// The "problems only" filter chip.
final StateProvider<bool> problemsOnlyProvider = StateProvider<bool>(
  (ref) => false,
);

/// Which systems are expanded, by id; kept across refreshes and layouts.
final StateProvider<Set<String>> expandedSystemsProvider =
    StateProvider<Set<String>>((ref) => const <String>{});
