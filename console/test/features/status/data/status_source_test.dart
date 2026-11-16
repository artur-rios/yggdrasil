import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:yggdrasil_console/features/environments/domain/environment.dart';
import 'package:yggdrasil_console/features/status/data/status_source.dart';
import 'package:yggdrasil_console/features/status/domain/status.dart';

import '../../../helpers.dart';

Matcher failsWith(StatusFailureKind kind) =>
    throwsA(isA<StatusException>().having((error) => error.kind, 'kind', kind));

/// A source over a client that records each request and answers [response].
(HttpStatusSource, List<http.Request>) recording(
  http.Response Function() response,
) {
  final seen = <http.Request>[];

  return (
    HttpStatusSource(
      MockClient((request) async {
        seen.add(request);

        return response();
      }),
    ),
    seen,
  );
}

HttpStatusSource answering(http.Response response) =>
    HttpStatusSource(MockClient((_) async => response));

const Environment withSlash = Environment(
  id: 'p',
  name: 'p',
  url: 'https://yggdrasil.example.com/',
);

void main() {
  group('HttpStatusSource', () {
    test('given a token, when fetched, then it GETs <base>/api/status as JSON '
        'with the bearer token', () async {
      final (source, seen) = recording(
        () => http.Response(contractExampleJson(), 200),
      );

      final status = await source.fetch(withSlash, 'secret');

      expect(seen.single.method, 'GET');
      expect(
        seen.single.url.toString(),
        'https://yggdrasil.example.com/api/status',
      );
      expect(seen.single.headers['Authorization'], 'Bearer secret');
      expect(seen.single.headers['Accept'], 'application/json');
      expect(status.host, 'example.com');
    });

    test('given no token or an empty one, when fetched, then no '
        'Authorization header is sent', () async {
      final (source, seen) = recording(
        () => http.Response(contractExampleJson(), 200),
      );

      await source.fetch(production, null);
      await source.fetch(production, '');

      for (final request in seen) {
        expect(request.headers.containsKey('Authorization'), isFalse);
      }
    });

    test('given a v1 body, when fetched, then it reads as a one-environment '
        'host', () async {
      final status = await answering(http.Response(v1ExampleJson(), 200))
          .fetch(production, 't');

      expect(status.contractVersion, 1);
      expect(status.environments.single.id, 'production');
    });

    test('given a UTF-8 body without a charset, when fetched, then it is read '
        'as UTF-8', () async {
      final source = answering(
        http.Response.bytes(
          utf8.encode(
            contractExampleJson().replaceFirst(
              'Identity and access management',
              'Identität',
            ),
          ),
          200,
        ),
      );

      final status = await source.fetch(production, 't');

      expect(status.systems.first.description, 'Identität');
    });

    test(
      'given a 401, when fetched, then it is an unauthorized failure',
      () async {
        await expectLater(
          answering(http.Response('', 401)).fetch(production, 'wrong'),
          throwsA(
            isA<StatusException>()
                .having((e) => e.kind, 'kind', StatusFailureKind.unauthorized)
                .having((e) => e.statusCode, 'statusCode', 401),
          ),
        );
      },
    );

    test('given another non-2xx, when fetched, then it is a server failure '
        'with its code', () async {
      await expectLater(
        answering(http.Response('bad gateway', 502)).fetch(production, 't'),
        throwsA(
          isA<StatusException>()
              .having((e) => e.kind, 'kind', StatusFailureKind.server)
              .having((e) => e.statusCode, 'statusCode', 502),
        ),
      );
    });

    test('given a redirect that was not followed, when fetched, then it is a '
        'server failure', () async {
      await expectLater(
        answering(http.Response('', 302)).fetch(production, 't'),
        failsWith(StatusFailureKind.server),
      );
    });

    test('given a 503 with Retry-After, when fetched, then it is the API '
        'starting, not an error', () async {
      await expectLater(
        answering(
          http.Response('', 503, headers: <String, String>{'retry-after': '5'}),
        ).fetch(production, 't'),
        throwsA(
          isA<StatusException>()
              .having((e) => e.kind, 'kind', StatusFailureKind.starting)
              .having((e) => e.isStarting, 'isStarting', isTrue)
              .having((e) => e.statusCode, 'statusCode', 503)
              .having(
                (e) => e.retryAfter,
                'retryAfter',
                const Duration(seconds: 5),
              ),
        ),
      );
    });

    test('given a 503 with an unreadable Retry-After, when fetched, then it '
        'is the API starting with the default wait', () async {
      await expectLater(
        answering(
          http.Response(
            '',
            503,
            headers: <String, String>{'retry-after': 'soon'},
          ),
        ).fetch(production, 't'),
        throwsA(
          isA<StatusException>().having(
            (e) => e.retryAfter,
            'retryAfter',
            const Duration(seconds: 5),
          ),
        ),
      );
    });

    test('given a 503 without Retry-After, when fetched, then it is a server '
        'failure', () async {
      await expectLater(
        answering(http.Response('no available server', 503))
            .fetch(production, 't'),
        failsWith(StatusFailureKind.server),
      );
    });

    test('given a client exception, when fetched, then it is a network '
        'failure with its detail', () async {
      final source = HttpStatusSource(
        MockClient(
          (_) async => throw http.ClientException('XMLHttpRequest error'),
        ),
      );

      await expectLater(
        source.fetch(production, 't'),
        throwsA(
          isA<StatusException>()
              .having((e) => e.kind, 'kind', StatusFailureKind.network)
              .having(
                (e) => e.detail,
                'detail',
                contains('XMLHttpRequest error'),
              ),
        ),
      );
    });

    test('given no answer within the timeout, when fetched, then it is a '
        'network timeout', () async {
      final source = HttpStatusSource(
        MockClient((_) => Completer<http.Response>().future),
        timeout: const Duration(milliseconds: 10),
      );

      await expectLater(
        source.fetch(production, 't'),
        throwsA(
          isA<StatusException>()
              .having((e) => e.kind, 'kind', StatusFailureKind.network)
              .having((e) => e.detail, 'detail', 'timeout'),
        ),
      );
    });

    test('given a 200 that is not the contract, when fetched, then it is an '
        'invalid response', () async {
      await expectLater(
        answering(http.Response('<html></html>', 200)).fetch(production, 't'),
        failsWith(StatusFailureKind.invalidResponse),
      );
    });
  });

  group('HttpStatusSource.refreshDetails', () {
    test('given a token, when refreshing details, then it POSTs '
        '<base>/api/status/refresh with the bearer token', () async {
      final (source, seen) = recording(
        () => http.Response(contractExampleJson(), 200),
      );

      final status = await source.refreshDetails(withSlash, 'secret');

      expect(seen.single.method, 'POST');
      expect(
        seen.single.url.toString(),
        'https://yggdrasil.example.com/api/status/refresh',
      );
      expect(seen.single.headers['Authorization'], 'Bearer secret');
      expect(status!.host, 'example.com');
    });

    test('given a 404 or a 405, when refreshing details, then it answers '
        'null: no such endpoint', () async {
      for (final code in <int>[404, 405]) {
        expect(
          await answering(http.Response('', code))
              .refreshDetails(production, 't'),
          isNull,
          reason: '$code',
        );
      }
    });

    test('given a 401, when refreshing details, then it is an unauthorized '
        'failure', () async {
      await expectLater(
        answering(http.Response('', 401)).refreshDetails(production, 'wrong'),
        failsWith(StatusFailureKind.unauthorized),
      );
    });

    test('given a 503 with Retry-After, when refreshing details, then it is '
        'the API starting', () async {
      await expectLater(
        answering(
          http.Response('', 503, headers: <String, String>{'retry-after': '5'}),
        ).refreshDetails(production, 't'),
        throwsA(
          isA<StatusException>()
              .having((e) => e.kind, 'kind', StatusFailureKind.starting)
              .having(
                (e) => e.retryAfter,
                'retryAfter',
                const Duration(seconds: 5),
              ),
        ),
      );
    });

    test('given another non-2xx, when refreshing details, then it is a server '
        'failure', () async {
      await expectLater(
        answering(http.Response('', 500)).refreshDetails(production, 't'),
        failsWith(StatusFailureKind.server),
      );
    });

    test('given a 200 that is not the contract, when refreshing details, then '
        'it is an invalid response', () async {
      await expectLater(
        answering(http.Response('[]', 200)).refreshDetails(production, 't'),
        failsWith(StatusFailureKind.invalidResponse),
      );
    });

    test('given a client exception, when refreshing details, then it is a '
        'network failure', () async {
      final source = HttpStatusSource(
        MockClient((_) async => throw http.ClientException('offline')),
      );

      await expectLater(
        source.refreshDetails(production, 't'),
        failsWith(StatusFailureKind.network),
      );
    });

    test('given a request slower than the refresh timeout, when refreshing '
        'details, then it is a network failure', () async {
      final source = HttpStatusSource(
        MockClient((_) => Completer<http.Response>().future),
        refreshTimeout: const Duration(milliseconds: 10),
      );

      await expectLater(
        source.refreshDetails(production, 't'),
        failsWith(StatusFailureKind.network),
      );
    });

    test('given defaults, when constructed, then the details refresh waits '
        'longer than a plain fetch', () {
      final source = HttpStatusSource(
        MockClient((_) async => http.Response('', 200)),
      );

      expect(source.timeout, const Duration(seconds: 10));
      expect(source.refreshTimeout, const Duration(seconds: 15));
    });
  });

  group('StatusException', () {
    test('given each kind of failure, when its message is read, then it is '
        'one line for the user', () {
      expect(
        const StatusException(StatusFailureKind.unauthorized).message,
        'The status API needs a valid token for this host.',
      );
      expect(
        const StatusException(StatusFailureKind.network).message,
        'The status API could not be reached.',
      );
      expect(
        const StatusException(
          StatusFailureKind.server,
          statusCode: 502,
        ).message,
        'The status API answered with HTTP 502.',
      );
      expect(
        const StatusException(StatusFailureKind.invalidResponse).message,
        'The status API sent a response this console cannot read.',
      );
      expect(
        const StatusException(StatusFailureKind.starting).message,
        'The status API is starting and has no results yet.',
      );
    });

    test('given a server failure without a code, when its message is read, '
        'then the code reads as ?', () {
      expect(
        const StatusException(StatusFailureKind.server).message,
        'The status API answered with HTTP ?.',
      );
    });

    test('given each kind, when asked whether it is starting, then only '
        'starting is', () {
      for (final kind in StatusFailureKind.values) {
        expect(
          StatusException(kind).isStarting,
          kind == StatusFailureKind.starting,
          reason: '$kind',
        );
      }
    });
  });

  group('parseRetryAfter', () {
    test('given seconds, when parsed, then they are kept between 1 s and '
        '1 min', () {
      expect(parseRetryAfter('5'), const Duration(seconds: 5));
      expect(parseRetryAfter(' 0 '), const Duration(seconds: 1));
      expect(parseRetryAfter('-3'), const Duration(seconds: 1));
      expect(parseRetryAfter('60'), const Duration(seconds: 60));
      expect(parseRetryAfter('3600'), const Duration(seconds: 60));
    });

    test('given anything but seconds, when parsed, then it means 5 s', () {
      for (final value in <String>[
        'Wed, 21 Oct 2026 07:28:00 GMT',
        '',
        '2.5',
      ]) {
        expect(
          parseRetryAfter(value),
          const Duration(seconds: 5),
          reason: value,
        );
      }
    });
  });

  group('parseStatusBody', () {
    test('given JSON that is not an object, when parsed, then it is an '
        'invalid response saying so', () {
      expect(
        () => parseStatusBody('[]'),
        throwsA(
          isA<StatusException>()
              .having((e) => e.kind, 'kind', StatusFailureKind.invalidResponse)
              .having((e) => e.detail, 'detail', 'Expected a JSON object'),
        ),
      );
    });

    test('given a body that is not JSON, when parsed, then it is an invalid '
        'response', () {
      expect(
        () => parseStatusBody('<html>502 Bad Gateway</html>'),
        throwsA(
          isA<StatusException>().having(
            (error) => error.kind,
            'kind',
            StatusFailureKind.invalidResponse,
          ),
        ),
      );
    });
  });

  group('DemoStatusSource', () {
    final now = DateTime.utc(2030, 1, 1, 12);

    test('given the fixture, when fetched, then every timestamp moves so '
        'generatedAt is now', () async {
      final source = DemoStatusSource(() async => demoJson(), now: () => now);

      final status = await source.fetch(Environment.demo(), null);
      final heimdallApi = status.systems.first.environments.last.applications
          .firstWhere((application) => application.id == 'heimdall-api');

      expect(status.generatedAt, now);
      // Production's: 20 h 24 min 9 s before generatedAt in the fixture.
      expect(
        heimdallApi.deployment!.deployedAt,
        now.subtract(const Duration(hours: 20, minutes: 24, seconds: 9)),
      );
      expect(status.systems, hasLength(4));
    });

    test('given the fixture, when details are refreshed, then the answer is '
        'the fixture again', () async {
      final source = DemoStatusSource(() async => demoJson(), now: () => now);

      final status = await source.refreshDetails(Environment.demo(), null);

      expect(status!.generatedAt, now);
      expect(status.systems, hasLength(4));
    });

    test('given null dates and fields that are not dates, when fetched, then '
        'only the dates move', () async {
      final fixture = jsonEncode(<String, dynamic>{
        'host': 'example.com',
        'generatedAt': '2026-10-08T18:00:00Z',
        'status': 'up',
        'environments': <Object>[
          <String, dynamic>{'id': 'production', 'status': 'up'},
        ],
        'systems': <Object>[
          <String, dynamic>{
            'id': 'sys',
            'name': 'System',
            'status': 'up',
            'environments': <Object>[
              <String, dynamic>{
                'environment': 'production',
                'status': 'up',
                'applications': <Object>[
                  <String, dynamic>{
                    'id': 'app',
                    'name': 'App',
                    'kind': 'api',
                    'status': 'up',
                    'deployment': <String, dynamic>{
                      'deployedAt': null,
                      'version': '2026-10-08T00:00:00Z',
                    },
                    'probe': <String, dynamic>{
                      'checkedAt': '2026-10-08T17:59:00Z',
                    },
                  },
                ],
              },
            ],
          },
        ],
      });
      final source = DemoStatusSource(() async => fixture, now: () => now);

      final application = (await source.fetch(
        Environment.demo(),
        null,
      )).systems.single.environments.single.applications.single;

      expect(application.deployment!.deployedAt, isNull);
      expect(application.deployment!.version, '2026-10-08T00:00:00Z');
      expect(
        application.probe!.checkedAt,
        now.subtract(const Duration(minutes: 1)),
      );
    });
  });

  group('the demo', () {
    test('given the demo, when its production is read, then an integration '
        'is failing and an API does not answer', () {
      final production = demoStatus().systems
          .expand((system) => system.environments)
          .where((environment) => environment.environment == 'production')
          .toList();
      final integrations = production.expand((e) => e.integrations).toList();
      final heimdallToHermes = integrations.singleWhere(
        (i) => i.from == 'heimdall-api' && i.system == 'hermes',
      );
      final unavailable = production
          .expand((e) => e.applications)
          .where((a) => a.details?.state == DetailsState.unavailable)
          .toList();

      expect(heimdallToHermes.status, IntegrationHealth.down);
      expect(heimdallToHermes.reason, 'connection refused');
      expect(unavailable.map((a) => a.id), <String>['hermes-api']);
      expect(unavailable.single.details!.reason, 'connection refused');
      // The caller did not answer, so what it depends on is not known.
      expect(
        integrations.singleWhere((i) => i.from == 'hermes-api').status,
        IntegrationHealth.unknown,
      );
    });

    test('given the demo, when its environments are read, then development '
        'and homologation are on demand', () {
      final status = demoStatus();

      expect(
        status.environments.where((e) => e.onDemand).map((e) => e.id),
        <String>['development', 'homologation'],
      );

      final heimdall = status.systems.firstWhere((s) => s.id == 'heimdall');

      expect(
        heimdall.environments.map((e) => (e.environment, e.onDemand)),
        <(String, bool)>[
          ('development', true),
          ('homologation', true),
          ('production', false),
        ],
      );
      expect(
        heimdall.environments.first.integrations.single.status,
        IntegrationHealth.stopped,
      );
    });
  });

  group('RoutingStatusSource', () {
    test('given the demo and a remote host, when fetched, then each goes to '
        'its own source', () async {
      final remote = FakeStatusSource();
      final demo = FakeStatusSource();
      final source = RoutingStatusSource(http: remote, demo: demo);

      await source.fetch(Environment.demo(), null);
      await source.fetch(production, 't');

      expect(demo.calls.single.$1.id, 'demo');
      expect(remote.calls.single.$1.id, 'production');
    });

    test('given the demo and a remote host, when details are refreshed, then '
        'they are routed like fetch', () async {
      final remote = FakeStatusSource();
      final demo = FakeStatusSource();
      final source = RoutingStatusSource(http: remote, demo: demo);

      await source.refreshDetails(Environment.demo(), null);
      await source.refreshDetails(production, 't');

      expect(demo.detailsCalls.single.$1.id, 'demo');
      expect(remote.detailsCalls.single.$1.id, 'production');
      expect(demo.calls, isEmpty);
      expect(remote.calls, isEmpty);
    });
  });
}
