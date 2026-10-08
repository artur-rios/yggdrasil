import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:yggdrasil_console/features/environments/domain/environment.dart';
import 'package:yggdrasil_console/features/status/data/status_repository.dart';
import 'package:yggdrasil_console/features/status/data/status_source.dart';
import 'package:yggdrasil_console/features/status/domain/status.dart';

import '../../../helpers.dart';

Matcher failsWith(StatusFailureKind kind) =>
    throwsA(isA<StatusException>().having((error) => error.kind, 'kind', kind));

void main() {
  group('HttpStatusSource', () {
    test('GETs <base>/api/status with the bearer token', () async {
      late http.Request seen;
      final source = HttpStatusSource(
        MockClient((request) async {
          seen = request;

          return http.Response(contractExampleJson(), 200);
        }),
      );

      final status = await source.fetch(
        const Environment(
          id: 'p',
          name: 'p',
          url: 'https://yggdrasil.example.com/',
        ),
        'secret',
      );

      expect(seen.method, 'GET');
      expect(seen.url.toString(), 'https://yggdrasil.example.com/api/status');
      expect(seen.headers['Authorization'], 'Bearer secret');
      expect(status.environment, 'production');
    });

    test('sends no Authorization header without a token', () async {
      late http.Request seen;
      final source = HttpStatusSource(
        MockClient((request) async {
          seen = request;

          return http.Response(contractExampleJson(), 200);
        }),
      );

      await source.fetch(production, null);

      expect(seen.headers.containsKey('Authorization'), isFalse);
    });

    test('reads the body as UTF-8 whatever the content type says', () async {
      final source = HttpStatusSource(
        MockClient(
          (_) async => http.Response.bytes(
            utf8.encode(
              contractExampleJson().replaceFirst(
                'Identity and access management',
                'Identität',
              ),
            ),
            200,
          ),
        ),
      );

      final status = await source.fetch(production, 't');

      expect(status.systems.single.description, 'Identität');
    });

    test('401 is an unauthorized failure', () {
      final source = HttpStatusSource(
        MockClient((_) async => http.Response('', 401)),
      );

      expect(
        source.fetch(production, 'wrong'),
        failsWith(StatusFailureKind.unauthorized),
      );
    });

    test('another non-2xx is a server failure with its code', () {
      final source = HttpStatusSource(
        MockClient((_) async => http.Response('bad gateway', 502)),
      );

      expect(
        source.fetch(production, 't'),
        throwsA(
          isA<StatusException>()
              .having((e) => e.kind, 'kind', StatusFailureKind.server)
              .having((e) => e.statusCode, 'statusCode', 502),
        ),
      );
    });

    test('503 with Retry-After is the API starting, not an error', () {
      final source = HttpStatusSource(
        MockClient(
          (_) async => http.Response(
            '',
            503,
            headers: <String, String>{'retry-after': '5'},
          ),
        ),
      );

      expect(
        source.fetch(production, 't'),
        throwsA(
          isA<StatusException>()
              .having((e) => e.kind, 'kind', StatusFailureKind.starting)
              .having((e) => e.isStarting, 'isStarting', isTrue)
              .having(
                (e) => e.retryAfter,
                'retryAfter',
                const Duration(seconds: 5),
              ),
        ),
      );
    });

    test('503 without Retry-After is a server failure', () {
      final source = HttpStatusSource(
        MockClient((_) async => http.Response('no available server', 503)),
      );

      expect(
        source.fetch(production, 't'),
        failsWith(StatusFailureKind.server),
      );
    });

    test('a client exception is a network failure', () {
      final source = HttpStatusSource(
        MockClient(
          (_) async => throw http.ClientException('XMLHttpRequest error'),
        ),
      );

      expect(
        source.fetch(production, 't'),
        failsWith(StatusFailureKind.network),
      );
    });

    test('no answer within the timeout is a network failure', () {
      final source = HttpStatusSource(
        MockClient((_) => Completer<http.Response>().future),
        timeout: const Duration(milliseconds: 10),
      );

      expect(
        source.fetch(production, 't'),
        failsWith(StatusFailureKind.network),
      );
    });

    test('a 200 that is not the contract is an invalid response', () {
      final source = HttpStatusSource(
        MockClient((_) async => http.Response('<html></html>', 200)),
      );

      expect(
        source.fetch(production, 't'),
        failsWith(StatusFailureKind.invalidResponse),
      );
    });
  });

  group('DemoStatusSource', () {
    test('moves every timestamp so generatedAt is now', () async {
      final now = DateTime.utc(2030, 1, 1, 12);
      final source = DemoStatusSource(() async => demoJson(), now: () => now);

      final status = await source.fetch(Environment.demo(), null);
      final heimdallApi = status.systems.first.applications.first;

      expect(status.generatedAt, now);
      // 20 h 24 min 9 s before generatedAt in the fixture.
      expect(
        heimdallApi.deployment!.deployedAt,
        now.subtract(const Duration(hours: 20, minutes: 24, seconds: 9)),
      );
      expect(status.systems, hasLength(4));
    });
  });

  test('RoutingStatusSource sends the demo to the demo source', () async {
    final remote = FakeStatusSource();
    final demo = FakeStatusSource();
    final source = RoutingStatusSource(http: remote, demo: demo);

    await source.fetch(Environment.demo(), null);
    await source.fetch(production, 't');

    expect(demo.calls.single.$1.id, 'demo');
    expect(remote.calls.single.$1.id, 'production');
  });

  group('StatusRepository', () {
    test('a success returns and remembers the snapshot', () async {
      final received = DateTime.utc(2026, 9, 18, 18, 5);
      final repository = StatusRepository(
        FakeStatusSource(),
        now: () => received,
      );

      final report = await repository.refresh(production, 't');

      expect(report.failure, isNull);
      expect(report.isStale, isFalse);
      expect(report.snapshot!.receivedAt, received);
      expect(report.snapshot!.status.environment, 'production');
      expect(repository.lastFor(production), same(report.snapshot));
    });

    test('401 is an auth failure', () async {
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
    });

    test('a network error keeps the last snapshot', () async {
      final source = FakeStatusSource();
      final repository = StatusRepository(source);

      final first = await repository.refresh(production, 't');
      source.respond = (_, _) =>
          throw const StatusException(StatusFailureKind.network);
      final second = await repository.refresh(production, 't');

      expect(second.failure!.kind, StatusFailureKind.network);
      expect(second.snapshot, same(first.snapshot));
      expect(second.isStale, isTrue);
    });

    test('snapshots are kept per environment', () async {
      final source = FakeStatusSource();
      final repository = StatusRepository(source);

      await repository.refresh(production, 't');
      source.respond = (_, _) =>
          throw const StatusException(StatusFailureKind.network);
      final report = await repository.refresh(homologation, 't');

      expect(report.snapshot, isNull);
      expect(repository.lastFor(production), isNotNull);
    });

    test('an unexpected error is an invalid response', () async {
      final repository = StatusRepository(
        FakeStatusSource((_, _) => throw StateError('boom')),
      );

      final report = await repository.refresh(production, 't');

      expect(report.failure!.kind, StatusFailureKind.invalidResponse);
    });

    test('forget drops the snapshot', () async {
      final repository = StatusRepository(FakeStatusSource());

      await repository.refresh(production, 't');
      repository.forget('production');

      expect(repository.lastFor(production), isNull);
    });

    test(
      'a snapshot from the URL an environment had before is not reused',
      () async {
        final source = FakeStatusSource();
        final repository = StatusRepository(source);
        final moved = production.copyWith(
          url: 'https://yggdrasil.new.example.com',
        );

        await repository.refresh(production, 't');
        source.respond = (_, _) =>
            throw const StatusException(StatusFailureKind.network);
        final report = await repository.refresh(moved, 't');

        expect(report.snapshot, isNull);
        expect(repository.lastFor(moved), isNull);
      },
    );
  });

  test('the demo environment follows the contract rules', () {
    expect(demoStatus().status, Status.degraded);
  });

  group('Retry-After', () {
    test('reads seconds, clamped to 1 s .. 1 min', () {
      expect(parseRetryAfter('5'), const Duration(seconds: 5));
      expect(parseRetryAfter(' 0 '), const Duration(seconds: 1));
      expect(parseRetryAfter('3600'), const Duration(seconds: 60));
    });

    test('anything else means 5 s', () {
      expect(
        parseRetryAfter('Wed, 21 Oct 2026 07:28:00 GMT'),
        const Duration(seconds: 5),
      );
    });
  });

  test('a starting answer does not make the last snapshot stale', () async {
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
}
