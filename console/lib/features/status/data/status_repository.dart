import '../../environments/domain/environment.dart';
import '../domain/status.dart';
import 'status_source.dart';

/// A successful response and when this device received it.
class StatusSnapshot {
  const StatusSnapshot({required this.status, required this.receivedAt});

  final EnvironmentStatus status;
  final DateTime receivedAt;
}

/// The outcome of one refresh: the latest good data this environment has had,
/// and the failure of this attempt if it failed.
///
/// A failed refresh keeps the last snapshot, so the console goes on showing
/// what it knew, marked as stale, instead of an empty screen.
class StatusReport {
  const StatusReport({this.snapshot, this.failure});

  final StatusSnapshot? snapshot;
  final StatusException? failure;

  bool get isStale =>
      failure != null && !failure!.isStarting && snapshot != null;
}

/// Fetches statuses and remembers the last good one per environment and URL.
///
/// Keyed by the URL too: an answer still in flight when the environment's URL
/// is edited came from the other host, and must never be shown, even as stale
/// data, for the new one.
class StatusRepository {
  StatusRepository(this._source, {DateTime Function()? now})
    : _now = now ?? DateTime.now;

  final StatusSource _source;
  final DateTime Function() _now;
  final Map<(String, String), StatusSnapshot> _last =
      <(String, String), StatusSnapshot>{};

  static (String, String) _key(Environment environment) =>
      (environment.id, environment.baseUrl);

  StatusSnapshot? lastFor(Environment environment) => _last[_key(environment)];

  Future<StatusReport> refresh(Environment environment, String? token) async {
    final key = _key(environment);

    try {
      final status = await _source.fetch(environment, token);
      final snapshot = StatusSnapshot(status: status, receivedAt: _now());

      _last[key] = snapshot;

      return StatusReport(snapshot: snapshot);
    } on StatusException catch (failure) {
      return StatusReport(snapshot: _last[key], failure: failure);
    } on Object catch (error) {
      // Anything else is a response shape the parser did not expect.
      return StatusReport(
        snapshot: _last[key],
        failure: StatusException(
          StatusFailureKind.invalidResponse,
          detail: '$error',
        ),
      );
    }
  }

  /// Drops what is remembered for an environment, e.g. after its URL changed.
  void forget(String environmentId) =>
      _last.removeWhere((key, _) => key.$1 == environmentId);
}
