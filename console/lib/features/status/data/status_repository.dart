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

/// Fetches statuses and remembers the last good one per environment.
class StatusRepository {
  StatusRepository(this._source, {DateTime Function()? now})
    : _now = now ?? DateTime.now;

  final StatusSource _source;
  final DateTime Function() _now;
  final Map<String, StatusSnapshot> _last = <String, StatusSnapshot>{};

  StatusSnapshot? lastFor(String environmentId) => _last[environmentId];

  Future<StatusReport> refresh(Environment environment, String? token) async {
    try {
      final status = await _source.fetch(environment, token);
      final snapshot = StatusSnapshot(status: status, receivedAt: _now());

      _last[environment.id] = snapshot;

      return StatusReport(snapshot: snapshot);
    } on StatusException catch (failure) {
      return StatusReport(snapshot: _last[environment.id], failure: failure);
    } on Object catch (error) {
      // Anything else is a response shape the parser did not expect.
      return StatusReport(
        snapshot: _last[environment.id],
        failure: StatusException(
          StatusFailureKind.invalidResponse,
          detail: '$error',
        ),
      );
    }
  }

  /// Drops what is remembered for an environment, e.g. after its URL changed.
  void forget(String environmentId) => _last.remove(environmentId);
}
