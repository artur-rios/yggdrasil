import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:yggdrasil_console/features/status/data/status_source.dart';
import 'package:yggdrasil_console/features/status/domain/status.dart';

import '../../../helpers.dart';

SystemStatus systemOf(HostStatus host, String id) =>
    host.systems.firstWhere((system) => system.id == id);

SystemEnvironment inEnvironment(SystemStatus system, String environment) =>
    system.environments.firstWhere((entry) => entry.environment == environment);

ApplicationStatus applicationOf(
  HostStatus host,
  String system,
  String environment,
  String id,
) => inEnvironment(
  systemOf(host, system),
  environment,
).applications.firstWhere((application) => application.id == id);

void main() {
  group('HostStatus.fromJson with the v2 contract example', () {
    late HostStatus status;

    setUp(() => status = parseStatusBody(contractExampleJson()));

    test('reads the host fields', () {
      expect(status.contractVersion, 2);
      expect(status.host, 'example.com');
      expect(status.generatedAt, DateTime.utc(2026, 10, 8, 18, 4, 11));
      expect(status.status, Status.up);
    });

    test('reads the environments in order', () {
      expect(
        status.environments.map((e) => (e.id, e.name, e.onDemand, e.status)),
        <(String, String, bool, Status)>[
          ('development', 'Development', true, Status.stopped),
          ('homologation', 'Homologation', true, Status.stopped),
          ('production', 'Production', false, Status.up),
        ],
      );
      expect(status.environment('production')!.name, 'Production');
      expect(status.environment('local'), isNull);
      expect(status.environmentName('local'), 'local');
    });

    test('reads each system per environment', () {
      expect(status.systems.map((system) => system.id), <String>[
        'heimdall',
        'fortuna',
        'yggdrasil',
      ]);

      final heimdall = systemOf(status, 'heimdall');
      expect(heimdall.name, 'Heimdall');
      expect(heimdall.description, 'Identity and access management');
      expect(heimdall.status, Status.up);
      expect(
        heimdall.environments.map((e) => (e.environment, e.status)),
        <(String, Status)>[
          ('development', Status.stopped),
          ('homologation', Status.stopped),
          ('production', Status.up),
        ],
      );
      expect(
        inEnvironment(heimdall, 'production').applications.map((a) => a.id),
        <String>['heimdall-api', 'heimdall-ui'],
      );
      // Every application of every environment.
      expect(heimdall.applications, hasLength(6));
    });

    test('the platform system appears in every environment', () {
      final yggdrasil = systemOf(status, 'yggdrasil');

      expect(yggdrasil.environments, hasLength(3));
      expect(
        yggdrasil.environments.map((e) => e.status),
        everyElement(Status.up),
      );
    });

    test('reads every application field', () {
      final application = applicationOf(
        status,
        'heimdall',
        'production',
        'heimdall-api',
      );

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
      expect(deployment.deployedAt, DateTime.utc(2026, 10, 7, 21, 40, 2));
      expect(deployment.image, 'heimdall-api:production-1.4.0-3f2a9c1');

      final container = application.container!;
      expect(container.state, 'running');
      expect(container.health, 'healthy');
      expect(container.startedAt, DateTime.utc(2026, 10, 7, 21, 40, 5));
      // The status API no longer has the count: null, as the contract says.
      expect(container.restartCount, isNull);

      final probe = application.probe!;
      expect(probe.healthy, isTrue);
      expect(probe.statusCode, 200);
      expect(probe.latencyMs, 12);
      expect(probe.checkedAt, DateTime.utc(2026, 10, 8, 18, 4, 9));
      expect(probe.error, isNull);
    });

    test('reads a stopped application: a container, no probe', () {
      final application = applicationOf(
        status,
        'heimdall',
        'development',
        'heimdall-api',
      );

      expect(application.status, Status.stopped);
      expect(application.url, 'https://heimdall-api-dev.example.com');
      expect(application.container!.state, 'exited');
      expect(application.deployment!.version, '1.5.0');
      expect(application.probe, isNull);
    });
  });

  group('HostStatus.fromJson with a v1 response', () {
    late HostStatus status;

    setUp(() => status = v1Status());

    test('reads it as a host with that one environment', () {
      expect(status.contractVersion, 1);
      // The environment's name also names the host, as v1 consoles showed it.
      expect(status.host, 'Production');
      expect(status.status, Status.degraded);
      expect(status.environments, hasLength(1));

      final environment = status.environments.single;
      expect(environment.id, 'production');
      expect(environment.name, 'Production');
      expect(environment.onDemand, isFalse);
      expect(environment.status, Status.degraded);
    });

    test('puts every system\'s applications in that environment', () {
      expect(status.systems.map((system) => system.id), <String>[
        'heimdall',
        'fortuna',
        'huginn',
        'yggdrasil',
      ]);

      for (final system in status.systems) {
        expect(system.environments.single.environment, 'production');
        expect(system.environments.single.status, system.status);
      }

      expect(
        status.systems.expand((system) => system.applications),
        hasLength(13),
      );
    });

    test('without environmentName, the id names the environment and host', () {
      final json = jsonDecode(v1ExampleJson()) as Map<String, dynamic>
        ..remove('environmentName');
      final status = HostStatus.fromJson(json);

      expect(status.host, 'production');
      expect(status.environments.single.name, 'production');
    });

    test('reads the null fields of a platform component', () {
      final traefik = applicationOf(
        status,
        'yggdrasil',
        'production',
        'traefik',
      );

      expect(traefik.kind, ApplicationKind.platform);
      expect(traefik.url, isNull);
      expect(traefik.repository, isNull);
      expect(traefik.deployment!.version, isNull);
      expect(traefik.deployment!.commit, isNull);
      expect(traefik.deployment!.deployedAt, isNull);
      expect(traefik.deployment!.image, 'traefik:v3.5');
    });

    test('reads a not deployed application with no container', () {
      final jenkins = applicationOf(
        status,
        'yggdrasil',
        'production',
        'jenkins',
      );

      expect(jenkins.status, Status.notDeployed);
      expect(jenkins.deployment, isNull);
      expect(jenkins.container, isNull);
      // Not deployed applications are not probed.
      expect(jenkins.probe, isNull);
    });

    test('reads a null container health as no health check', () {
      final fortunaUi = applicationOf(
        status,
        'fortuna',
        'production',
        'fortuna-ui',
      );

      expect(fortunaUi.container!.health, isNull);
      expect(fortunaUi.container!.state, 'running');
    });

    test('a failing HTTP answer has a status code and no error', () {
      final loki = applicationOf(status, 'yggdrasil', 'production', 'loki');

      expect(loki.probe!.healthy, isFalse);
      expect(loki.probe!.statusCode, 503);
      expect(loki.probe!.error, isNull);
    });

    test('reads the worker kind', () {
      final worker = systemOf(status, 'huginn').applications.single;

      expect(worker.kind, ApplicationKind.worker);
      expect(worker.status, Status.down);
      expect(worker.container!.restartCount, isNull);
    });
  });

  group('HostStatus.fromJson with the demo fixture', () {
    late HostStatus status;

    setUp(() => status = demoStatus());

    test('reads the default setup', () {
      expect(status.host, 'example.com');
      expect(status.status, Status.up);
      expect(status.environments.map((e) => e.id), <String>[
        'development',
        'homologation',
        'production',
      ]);
      expect(status.systems.map((system) => system.id), <String>[
        'heimdall',
        'fortuna',
        'yggdrasil',
      ]);
    });

    test('the platform components are the same in every environment', () {
      final yggdrasil = systemOf(status, 'yggdrasil');

      for (final environment in yggdrasil.environments) {
        expect(
          environment.applications.map((a) => (a.id, a.status)),
          <(String, Status)>[
            ('traefik', Status.up),
            ('status', Status.up),
            ('console', Status.up),
            ('prometheus', Status.up),
            ('loki', Status.up),
            ('alloy', Status.up),
            ('grafana', Status.up),
            ('jenkins', Status.notDeployed),
          ],
        );
      }
    });
  });

  group('lenient parsing', () {
    Map<String, dynamic> minimal({
      Object? status = 'up',
      Object? kind = 'api',
      Map<String, dynamic>? extra,
    }) => <String, dynamic>{
      'host': 'example.com',
      'generatedAt': '2026-10-08T18:04:11Z',
      'status': status,
      'environments': <Object>[
        <String, dynamic>{
          'id': 'homologation',
          'name': 'Homologation',
          'onDemand': true,
          'status': status,
        },
      ],
      'systems': <Object>[
        <String, dynamic>{
          'id': 'sys',
          'name': 'System',
          'status': status,
          'environments': <Object>[
            <String, dynamic>{
              'environment': 'homologation',
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
        },
      ],
    };

    ApplicationStatus onlyApplication(HostStatus status) =>
        status.systems.single.environments.single.applications.single;

    Map<String, dynamic> firstSystem(Map<String, dynamic> json) =>
        (json['systems'] as List<Object>).single as Map<String, dynamic>;

    test('an unknown status string reads as unknown', () {
      final status = HostStatus.fromJson(minimal(status: 'rebooting'));

      expect(status.status, Status.unknown);
      expect(status.environments.single.status, Status.unknown);
      expect(status.systems.single.status, Status.unknown);
      expect(status.systems.single.environments.single.status, Status.unknown);
      expect(onlyApplication(status).status, Status.unknown);
    });

    test('stopped is read everywhere', () {
      final status = HostStatus.fromJson(minimal(status: 'stopped'));

      expect(status.environments.single.status, Status.stopped);
      expect(onlyApplication(status).status, Status.stopped);
      expect(Status.parse('stopped'), Status.stopped);
    });

    test('a missing or non-string status reads as unknown', () {
      expect(Status.parse(null), Status.unknown);
      expect(Status.parse(3), Status.unknown);
      expect(Status.parse('not_deployed'), Status.notDeployed);
    });

    test('an unknown kind keeps its wire value', () {
      final application = onlyApplication(
        HostStatus.fromJson(minimal(kind: 'cron')),
      );

      expect(application.kind, ApplicationKind.other);
      expect(application.kindWire, 'cron');
    });

    test('absent optional objects and fields read as null', () {
      final status = HostStatus.fromJson(minimal());
      final application = onlyApplication(status);

      expect(application.url, isNull);
      expect(application.repository, isNull);
      expect(application.deployment, isNull);
      expect(application.container, isNull);
      expect(application.probe, isNull);
      expect(status.systems.single.description, isNull);
    });

    test('explicit nulls inside objects read as null', () {
      final application = onlyApplication(
        HostStatus.fromJson(
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
        ),
      );

      expect(application.deployment!.version, isNull);
      expect(application.deployment!.deployedAt, isNull);
      expect(application.container!.restartCount, isNull);
      expect(application.probe!.healthy, isNull);
      expect(application.probe!.checkedAt, isNull);
    });

    test('a deployedAt that is not a date is kept as written', () {
      final application = onlyApplication(
        HostStatus.fromJson(
          minimal(
            extra: <String, dynamic>{
              'deployment': <String, dynamic>{'deployedAt': 'last tuesday'},
            },
          ),
        ),
      );

      expect(application.deployment!.deployedAt, isNull);
      expect(application.deployment!.deployedAtText, 'last tuesday');
    });

    test('an unreadable date reads as null', () {
      final json = minimal()..['generatedAt'] = 'yesterday';

      expect(HostStatus.fromJson(json).generatedAt, isNull);
    });

    test('an environment without a name is called by its id', () {
      final json = minimal();
      ((json['environments'] as List<Object>).single as Map<String, dynamic>)
        ..remove('name')
        ..['onDemand'] = 'yes';
      final environment = HostStatus.fromJson(json).environments.single;

      expect(environment.name, 'homologation');
      // Only a JSON true is on demand.
      expect(environment.onDemand, isFalse);
    });

    test('a system environment the host does not list is still shown', () {
      final json = minimal();
      final environments = firstSystem(json)['environments'] as List<Object>;
      environments.insert(0, <String, dynamic>{
        'environment': 'staging',
        'status': 'up',
        'applications': <Object>[],
      });
      final status = HostStatus.fromJson(json);

      // In the host's order; unknown ones after the known ones.
      expect(
        status.systems.single.environments.map((e) => e.environment),
        <String>['homologation', 'staging'],
      );
      expect(status.environmentName('staging'), 'staging');
    });

    test('unknown extra fields are ignored', () {
      final json = minimal()..['future'] = <String, dynamic>{'x': 1};

      expect(HostStatus.fromJson(json).host, 'example.com');
    });

    test('a missing systems list is an invalid response', () {
      expect(
        () => parseStatusBody(
          jsonEncode(<String, dynamic>{
            'host': 'x',
            'environments': <Object>[],
          }),
        ),
        throwsA(
          isA<StatusException>().having(
            (error) => error.kind,
            'kind',
            StatusFailureKind.invalidResponse,
          ),
        ),
      );
      expect(
        () =>
            parseStatusBody(jsonEncode(<String, dynamic>{'environment': 'x'})),
        throwsA(isA<StatusException>()),
      );
    });

    test('a v2 response without a host or environments is invalid', () {
      expect(
        () => parseStatusBody(jsonEncode(minimal()..remove('host'))),
        throwsA(isA<StatusException>()),
      );
      expect(
        () => parseStatusBody(jsonEncode(minimal()..remove('environments'))),
        throwsA(isA<StatusException>()),
      );
    });

    test('a v2 system without its environments list is invalid', () {
      final json = minimal();
      firstSystem(json).remove('environments');

      expect(
        () => parseStatusBody(jsonEncode(json)),
        throwsA(isA<StatusException>()),
      );
    });

    test('a system without an id is an invalid response', () {
      final json = minimal();
      firstSystem(json).remove('id');

      expect(
        () => parseStatusBody(jsonEncode(json)),
        throwsA(isA<StatusException>()),
      );
    });

    test('an environment without an id is an invalid response', () {
      final json = minimal();
      ((json['environments'] as List<Object>).single as Map<String, dynamic>)
          .remove('id');

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
