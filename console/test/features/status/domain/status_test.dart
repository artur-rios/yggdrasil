import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:yggdrasil_console/features/status/data/status_source.dart';
import 'package:yggdrasil_console/features/status/domain/status.dart';

import '../../../helpers.dart';

void main() {
  group('EnvironmentStatus.fromJson with the contract example', () {
    late EnvironmentStatus status;

    setUp(() => status = parseStatusBody(contractExampleJson()));

    test('reads the environment fields', () {
      expect(status.environment, 'production');
      expect(status.environmentName, 'Production');
      expect(status.displayName, 'Production');
      expect(status.generatedAt, DateTime.utc(2026, 9, 18, 18, 4, 11));
      expect(status.status, Status.degraded);
      expect(status.systems, hasLength(1));
    });

    test('reads the system fields', () {
      final system = status.systems.single;

      expect(system.id, 'heimdall');
      expect(system.name, 'Heimdall');
      expect(system.description, 'Identity and access management');
      expect(system.status, Status.up);
      expect(system.applications, hasLength(1));
    });

    test('reads every application field', () {
      final application = status.systems.single.applications.single;

      expect(application.id, 'heimdall-api');
      expect(application.name, 'Heimdall API');
      expect(application.kind, ApplicationKind.api);
      expect(application.status, Status.up);
      expect(application.url, 'https://heimdall-api.example.com');
      expect(
        application.repository,
        'https://github.com/artur-rios/heimdall-api',
      );

      final deployment = application.deployment!;
      expect(deployment.version, '1.4.0');
      expect(deployment.commit, '3f2a9c1');
      expect(deployment.deployedAt, DateTime.utc(2026, 9, 17, 21, 40, 2));
      expect(deployment.image, 'heimdall-api:1.4.0-3f2a9c1');

      final container = application.container!;
      expect(container.state, 'running');
      expect(container.health, 'healthy');
      expect(container.startedAt, DateTime.utc(2026, 9, 17, 21, 40, 5));
      // The status API no longer has the count: null, as the contract says.
      expect(container.restartCount, isNull);

      final probe = application.probe!;
      expect(probe.healthy, isTrue);
      expect(probe.statusCode, 200);
      expect(probe.latencyMs, 12);
      expect(probe.checkedAt, DateTime.utc(2026, 9, 18, 18, 4, 9));
      expect(probe.error, isNull);
    });
  });

  group('EnvironmentStatus.fromJson with the demo fixture', () {
    late EnvironmentStatus status;

    setUp(() => status = demoStatus());

    test('reads every system and application', () {
      expect(status.systems.map((system) => system.id), <String>[
        'heimdall',
        'fortuna',
        'huginn',
        'yggdrasil',
      ]);
      expect(
        status.systems.expand((system) => system.applications),
        hasLength(13),
      );
      // One application down next to others up: degraded, not down.
      expect(status.status, Status.degraded);
    });

    test('reads the null fields of a platform component', () {
      final traefik = status.systems
          .firstWhere((system) => system.id == 'yggdrasil')
          .applications
          .firstWhere((application) => application.id == 'traefik');

      expect(traefik.kind, ApplicationKind.platform);
      expect(traefik.url, isNull);
      expect(traefik.repository, isNull);
      expect(traefik.deployment!.version, isNull);
      expect(traefik.deployment!.commit, isNull);
      expect(traefik.deployment!.deployedAt, isNull);
      expect(traefik.deployment!.image, 'traefik:v3.5');
    });

    test('reads a not deployed application with no container', () {
      final jenkins = status.systems
          .firstWhere((system) => system.id == 'yggdrasil')
          .applications
          .firstWhere((application) => application.id == 'jenkins');

      expect(jenkins.status, Status.notDeployed);
      expect(jenkins.deployment, isNull);
      expect(jenkins.container, isNull);
      // Not deployed applications are not probed.
      expect(jenkins.probe, isNull);
    });

    test('reads a null container health as no health check', () {
      final fortunaUi = status.systems
          .firstWhere((system) => system.id == 'fortuna')
          .applications
          .firstWhere((application) => application.id == 'fortuna-ui');

      expect(fortunaUi.container!.health, isNull);
      expect(fortunaUi.container!.state, 'running');
    });

    test('a failing HTTP answer has a status code and no error', () {
      final loki = status.systems
          .firstWhere((system) => system.id == 'yggdrasil')
          .applications
          .firstWhere((application) => application.id == 'loki');

      expect(loki.probe!.healthy, isFalse);
      expect(loki.probe!.statusCode, 503);
      expect(loki.probe!.error, isNull);
    });

    test('reads the worker kind', () {
      final worker = status.systems
          .firstWhere((system) => system.id == 'huginn')
          .applications
          .single;

      expect(worker.kind, ApplicationKind.worker);
      expect(worker.status, Status.down);
      expect(worker.container!.restartCount, isNull);
    });
  });

  group('lenient parsing', () {
    Map<String, dynamic> minimal({
      Object? status = 'up',
      Object? kind = 'api',
      Map<String, dynamic>? extra,
    }) => <String, dynamic>{
      'environment': 'homologation',
      'generatedAt': '2026-09-18T18:04:11Z',
      'status': status,
      'systems': <Object>[
        <String, dynamic>{
          'id': 'sys',
          'name': 'System',
          'status': status,
          'applications': <Object>[
            <String, dynamic>{
              'id': 'app',
              'name': 'App',
              'kind': kind,
              'status': status,
              ...?extra,
            },
          ],
        },
      ],
    };

    test('an unknown status string reads as unknown', () {
      final status = EnvironmentStatus.fromJson(minimal(status: 'rebooting'));

      expect(status.status, Status.unknown);
      expect(status.systems.single.status, Status.unknown);
      expect(status.systems.single.applications.single.status, Status.unknown);
    });

    test('a missing or non-string status reads as unknown', () {
      expect(Status.parse(null), Status.unknown);
      expect(Status.parse(3), Status.unknown);
      expect(Status.parse('not_deployed'), Status.notDeployed);
    });

    test('an unknown kind keeps its wire value', () {
      final application = EnvironmentStatus.fromJson(minimal(kind: 'cron'))
          .systems
          .single
          .applications
          .single;

      expect(application.kind, ApplicationKind.other);
      expect(application.kindWire, 'cron');
    });

    test('absent optional objects and fields read as null', () {
      final application = EnvironmentStatus.fromJson(minimal())
          .systems
          .single
          .applications
          .single;

      expect(application.url, isNull);
      expect(application.repository, isNull);
      expect(application.deployment, isNull);
      expect(application.container, isNull);
      expect(application.probe, isNull);
      expect(
        EnvironmentStatus.fromJson(minimal()).systems.single.description,
        isNull,
      );
    });

    test('explicit nulls inside objects read as null', () {
      final application = EnvironmentStatus.fromJson(
        minimal(
          extra: <String, dynamic>{
            'deployment': <String, dynamic>{
              'version': null,
              'commit': null,
              'deployedAt': null,
              'image': null,
            },
            'container': <String, dynamic>{
              'state': null,
              'health': null,
              'startedAt': null,
              'restartCount': null,
            },
            'probe': <String, dynamic>{
              'healthy': null,
              'statusCode': null,
              'latencyMs': null,
              'checkedAt': null,
              'error': null,
            },
          },
        ),
      ).systems.single.applications.single;

      expect(application.deployment!.version, isNull);
      expect(application.deployment!.deployedAt, isNull);
      expect(application.container!.restartCount, isNull);
      expect(application.probe!.healthy, isNull);
      expect(application.probe!.checkedAt, isNull);
    });

    test('a deployedAt that is not a date is kept as written', () {
      final application = EnvironmentStatus.fromJson(
        minimal(
          extra: <String, dynamic>{
            'deployment': <String, dynamic>{'deployedAt': 'last tuesday'},
          },
        ),
      ).systems.single.applications.single;

      expect(application.deployment!.deployedAt, isNull);
      expect(application.deployment!.deployedAtText, 'last tuesday');
    });

    test('an unreadable date reads as null', () {
      final json = minimal()..['generatedAt'] = 'yesterday';

      expect(EnvironmentStatus.fromJson(json).generatedAt, isNull);
    });

    test('without environmentName, the id is the display name', () {
      final status = EnvironmentStatus.fromJson(minimal());

      expect(status.environmentName, isNull);
      expect(status.displayName, 'homologation');
    });

    test('unknown extra fields are ignored', () {
      final json = minimal()..['future'] = <String, dynamic>{'x': 1};

      expect(EnvironmentStatus.fromJson(json).environment, 'homologation');
    });

    test('a missing systems list is an invalid response', () {
      expect(
        () =>
            parseStatusBody(jsonEncode(<String, dynamic>{'environment': 'x'})),
        throwsA(
          isA<StatusException>().having(
            (error) => error.kind,
            'kind',
            StatusFailureKind.invalidResponse,
          ),
        ),
      );
    });

    test('a system without an id is an invalid response', () {
      final json = minimal();
      ((json['systems'] as List<Object>).single as Map<String, dynamic>).remove(
        'id',
      );

      expect(
        () => parseStatusBody(jsonEncode(json)),
        throwsA(isA<StatusException>()),
      );
    });

    test('a body that is not JSON is an invalid response', () {
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
      expect(() => parseStatusBody('[]'), throwsA(isA<StatusException>()));
    });
  });
}
