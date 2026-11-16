import 'package:flutter_test/flutter_test.dart';
import 'package:yggdrasil_console/features/status/data/status_repository.dart';
import 'package:yggdrasil_console/features/status/data/status_source.dart';

import '../../../helpers.dart';

StatusException network() => const StatusException(StatusFailureKind.network);

void main() {
  group('StatusRepository.refresh', () {
    test('given a success, when refreshed, then the snapshot is returned with '
        'when it was received, and remembered', () async {
      final received = DateTime.utc(2026, 9, 18, 18, 5);
      final repository = StatusRepository(
        FakeStatusSource(),
        now: () => received,
      );

      final report = await repository.refresh(production, 't');

      expect(report.failure, isNull);
      expect(report.isStale, isFalse);
      expect(report.snapshot!.receivedAt, received);
      expect(report.snapshot!.status.host, 'example.com');
      expect(repository.lastFor(production), same(report.snapshot));
    });

    test('given a token, when refreshed, then the source gets it', () async {
      final source = FakeStatusSource();

      await StatusRepository(source).refresh(production, 'secret');

      expect(source.calls.single, (production, 'secret'));
    });

    test('given a 401 and nothing before, when refreshed, then it is an auth '
        'failure with no snapshot, not stale', () async {
      final repository = StatusRepository(
        FakeStatusSource(
          (_, _) => throw const StatusException(
            StatusFailureKind.unauthorized,
            statusCode: 401,
          ),
        ),
      );

      final report = await repository.refresh(production, null);

      expect(report.failure!.kind, StatusFailureKind.unauthorized);
      expect(report.snapshot, isNull);
      expect(report.isStale, isFalse);
    });

    test('given an earlier success, when a refresh fails with a network '
        'error, then the last snapshot is kept, stale', () async {
      final source = FakeStatusSource();
      final repository = StatusRepository(source);

      final first = await repository.refresh(production, 't');
      source.respond = (_, _) => throw network();
      final second = await repository.refresh(production, 't');

      expect(second.failure!.kind, StatusFailureKind.network);
      expect(second.snapshot, same(first.snapshot));
      expect(second.isStale, isTrue);
    });

    test('given an earlier success, when the API answers it is starting, '
        'then the last snapshot is kept and not stale', () async {
      final source = FakeStatusSource();
      final repository = StatusRepository(source);

      await repository.refresh(production, 't');
      source.respond = (_, _) => throw const StatusException(
        StatusFailureKind.starting,
        statusCode: 503,
        retryAfter: Duration(seconds: 5),
      );
      final report = await repository.refresh(production, 't');

      expect(report.failure!.isStarting, isTrue);
      expect(report.snapshot, isNotNull);
      expect(report.isStale, isFalse);
    });

    test('given a success for one environment, when another fails, then it '
        'has no snapshot of its own', () async {
      final source = FakeStatusSource();
      final repository = StatusRepository(source);

      await repository.refresh(production, 't');
      source.respond = (_, _) => throw network();
      final report = await repository.refresh(homologation, 't');

      expect(report.snapshot, isNull);
      expect(repository.lastFor(production), isNotNull);
    });

    test('given an error that is not a StatusException, when refreshed, then '
        'it is an invalid response carrying the error', () async {
      final repository = StatusRepository(
        FakeStatusSource((_, _) => throw StateError('boom')),
      );

      final report = await repository.refresh(production, 't');

      expect(report.failure!.kind, StatusFailureKind.invalidResponse);
      expect(report.failure!.detail, contains('boom'));
    });

    test('given a snapshot from the URL an environment had before, when the '
        'new URL fails, then the old snapshot is not reused', () async {
      final source = FakeStatusSource();
      final repository = StatusRepository(source);
      final moved = production.copyWith(
        url: 'https://yggdrasil.new.example.com',
      );

      await repository.refresh(production, 't');
      source.respond = (_, _) => throw network();
      final report = await repository.refresh(moved, 't');

      expect(report.snapshot, isNull);
      expect(repository.lastFor(moved), isNull);
    });

    test('given an environment renamed, when its last snapshot is read, then '
        'it is still found', () async {
      final repository = StatusRepository(FakeStatusSource());

      await repository.refresh(production, 't');

      expect(
        repository.lastFor(production.copyWith(name: 'renamed')),
        isNotNull,
      );
    });
  });

  group('StatusRepository.refreshDetails', () {
    test('given a success, when details are refreshed, then the snapshot is '
        'returned and remembered', () async {
      final repository = StatusRepository(FakeStatusSource());

      final report = await repository.refreshDetails(production, 't');

      expect(report.failure, isNull);
      expect(repository.lastFor(production), same(report.snapshot));
    });

    test('given an earlier snapshot, when the details answer null, then that '
        'snapshot is kept with no failure', () async {
      final source = FakeStatusSource();
      final repository = StatusRepository(source);
      final first = await repository.refresh(production, 't');

      source.respondDetails = (_, _) async => null;

      final report = await repository.refreshDetails(production, 't');

      expect(report.failure, isNull);
      expect(report.snapshot, same(first.snapshot));
      expect(repository.lastFor(production), same(first.snapshot));
    });

    test('given nothing before, when the details answer null, then there is '
        'neither a snapshot nor a failure', () async {
      final source = FakeStatusSource()..respondDetails = (_, _) async => null;

      final report = await StatusRepository(source)
          .refreshDetails(production, 't');

      expect(report.snapshot, isNull);
      expect(report.failure, isNull);
      expect(report.isStale, isFalse);
    });

    test('given an earlier snapshot, when the details fail, then the last '
        'snapshot is kept, stale', () async {
      final source = FakeStatusSource();
      final repository = StatusRepository(source);
      final first = await repository.refresh(production, 't');

      source.respondDetails = (_, _) => throw const StatusException(
        StatusFailureKind.unauthorized,
        statusCode: 401,
      );

      final report = await repository.refreshDetails(production, 't');

      expect(report.failure!.kind, StatusFailureKind.unauthorized);
      expect(report.snapshot, same(first.snapshot));
      expect(report.isStale, isTrue);
    });

    test('given an unexpected error, when details are refreshed, then it is '
        'an invalid response', () async {
      final source = FakeStatusSource()
        ..respondDetails = (_, _) => throw const FormatException('bad');

      final report = await StatusRepository(source)
          .refreshDetails(production, 't');

      expect(report.failure!.kind, StatusFailureKind.invalidResponse);
    });
  });

  group('StatusRepository.forget', () {
    test('given snapshots of two environments, when one is forgotten, then '
        'only its snapshot goes', () async {
      final repository = StatusRepository(FakeStatusSource());

      await repository.refresh(production, 't');
      await repository.refresh(homologation, 't');
      repository.forget('production');

      expect(repository.lastFor(production), isNull);
      expect(repository.lastFor(homologation), isNotNull);
    });

    test('given snapshots of an environment at two URLs, when forgotten, then '
        'both go', () async {
      final repository = StatusRepository(FakeStatusSource());
      final moved = production.copyWith(url: 'https://new.example.com');

      await repository.refresh(production, 't');
      await repository.refresh(moved, 't');
      repository.forget('production');

      expect(repository.lastFor(production), isNull);
      expect(repository.lastFor(moved), isNull);
    });
  });

  group('StatusReport', () {
    test('given a failure but no snapshot, when asked if stale, then it is '
        'not: there is nothing to be stale', () {
      expect(StatusReport(failure: network()).isStale, isFalse);
    });
  });
}
