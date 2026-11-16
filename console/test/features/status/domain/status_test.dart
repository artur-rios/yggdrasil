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

Matcher get isInvalidResponse => throwsA(
  isA<StatusException>().having(
    (error) => error.kind,
    'kind',
    StatusFailureKind.invalidResponse,
  ),
);

void main() {
  group('Status', () {
    test('given every status, when sorted by severity, then down > degraded '
        '> unknown > up > stopped > not deployed', () {
      final sorted = Status.values.toList()
        ..sort((a, b) => b.severity.compareTo(a.severity));

      expect(sorted, <Status>[
        Status.down,
        Status.degraded,
        Status.unknown,
        Status.up,
        Status.stopped,
        Status.notDeployed,
      ]);
    });

    test('given every status, when asked which are problems, then down, '
        'degraded and unknown are and stopped is not', () {
      expect(
        Status.values.where((status) => status.isProblem),
        unorderedEquals(<Status>[Status.down, Status.degraded, Status.unknown]),
      );
    });

    test('given each wire value, when parsed, then it is its own status', () {
      for (final status in Status.values) {
        expect(Status.parse(status.wire), status, reason: status.wire);
      }
    });

    test('given a missing, non-string or differently cased value, when '
        'parsed, then it is unknown', () {
      expect(Status.parse(null), Status.unknown);
      expect(Status.parse(3), Status.unknown);
      expect(Status.parse('UP'), Status.unknown);
      expect(Status.parse('notDeployed'), Status.unknown);
    });
  });

  group('ApplicationKind', () {
    test(
      'given each known wire value, when parsed, then it is its own kind',
      () {
        for (final kind in ApplicationKind.values) {
          expect(ApplicationKind.parse(kind.wire), kind, reason: kind.wire);
        }
      },
    );

    test('given an unknown or missing kind, when parsed, then it is other', () {
      expect(ApplicationKind.parse('cron'), ApplicationKind.other);
      expect(ApplicationKind.parse(null), ApplicationKind.other);
    });
  });

  group('IntegrationHealth and DetailsState', () {
    test('given each wire value, when parsed, then it is its own value', () {
      for (final health in IntegrationHealth.values) {
        expect(IntegrationHealth.parse(health.wire), health);
      }
      for (final state in DetailsState.values) {
        expect(DetailsState.parse(state.wire), state);
      }
    });

    test('given a missing value, when parsed, then the health is unknown and '
        'the details not checked', () {
      expect(IntegrationHealth.parse(null), IntegrationHealth.unknown);
      expect(DetailsState.parse(null), DetailsState.notChecked);
    });
  });

  group('HostStatus.fromJson with the v2 contract example', () {
    late HostStatus status;

    setUp(() => status = parseStatusBody(contractExampleJson()));

    test('given the example, when parsed, then the host fields are read', () {
      expect(status.contractVersion, 2);
      expect(status.host, 'example.com');
      expect(status.generatedAt, DateTime.utc(2026, 10, 8, 18, 4, 11));
      expect(status.status, Status.up);
    });

    test('given the example, when parsed, then the environments are read in '
        'order and found by id', () {
      expect(
        status.environments.map((e) => (e.id, e.name, e.onDemand, e.status)),
        <(String, String, bool, Status)>[
          ('development', 'Development', true, Status.stopped),
          ('homologation', 'Homologation', true, Status.stopped),
          ('production', 'Production', false, Status.up),
        ],
      );
      expect(status.environment('production')!.name, 'Production');
      expect(status.environmentName('production'), 'Production');
      expect(status.environment('local'), isNull);
      expect(status.environmentName('local'), 'local');
    });

    test('given the example, when parsed, then each system is read per '
        'environment', () {
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

    test('given the example, when parsed, then the platform system appears '
        'in every environment', () {
      final yggdrasil = systemOf(status, 'yggdrasil');

      expect(yggdrasil.environments, hasLength(3));
      // Stopped where the environment is; its components still report up.
      expect(yggdrasil.environments.map((e) => e.status), <Status>[
        Status.stopped,
        Status.stopped,
        Status.up,
      ]);
      expect(
        yggdrasil.environments
            .expand((e) => e.applications)
            .map((a) => a.status),
        everyElement(Status.up),
      );
    });

    test('given the example, when parsed, then every application field is '
        'read', () {
      final application = applicationOf(
        status,
        'heimdall',
        'production',
        'heimdall-api',
      );

      expect(application.id, 'heimdall-api');
      expect(application.name, 'Heimdall API');
      expect(application.kind, ApplicationKind.api);
      expect(application.kindWire, 'api');
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

    test('given a stopped application, when parsed, then it has a container '
        'and no probe', () {
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

    test('given a v1 response, when parsed, then it is a host with that one '
        'environment, named after it', () {
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

    test("given a v1 response, when parsed, then every system's applications "
        'are in that environment', () {
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

    test('given a v1 response without environmentName, when parsed, then the '
        'id names the environment and the host', () {
      final json = jsonDecode(v1ExampleJson()) as Map<String, dynamic>
        ..remove('environmentName');
      final status = HostStatus.fromJson(json);

      expect(status.host, 'production');
      expect(status.environments.single.name, 'production');
    });

    test('given a v1 response without its environment, when parsed, then it '
        'is an invalid response', () {
      final json = jsonDecode(v1ExampleJson()) as Map<String, dynamic>
        ..remove('environment');

      expect(() => parseStatusBody(jsonEncode(json)), isInvalidResponse);
    });

    test('given a v1 system without applications, when parsed, then it is an '
        'invalid response', () {
      final json = jsonDecode(v1ExampleJson()) as Map<String, dynamic>;
      ((json['systems'] as List<dynamic>).first as Map<String, dynamic>).remove(
        'applications',
      );

      expect(() => parseStatusBody(jsonEncode(json)), isInvalidResponse);
    });

    test('given a platform component, when parsed, then its null fields read '
        'as null', () {
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

    test('given a not deployed application, when parsed, then it has no '
        'container and no probe', () {
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

    test('given a null container health, when parsed, then it reads as no '
        'health check', () {
      final fortunaUi = applicationOf(
        status,
        'fortuna',
        'production',
        'fortuna-ui',
      );

      expect(fortunaUi.container!.health, isNull);
      expect(fortunaUi.container!.state, 'running');
    });

    test('given a failing HTTP answer, when parsed, then the probe has a '
        'status code and no error', () {
      final loki = applicationOf(status, 'yggdrasil', 'production', 'loki');

      expect(loki.probe!.healthy, isFalse);
      expect(loki.probe!.statusCode, 503);
      expect(loki.probe!.error, isNull);
    });

    test('given a worker, when parsed, then its kind is read', () {
      final worker = systemOf(status, 'huginn').applications.single;

      expect(worker.kind, ApplicationKind.worker);
      expect(worker.status, Status.down);
      expect(worker.container!.restartCount, isNull);
    });
  });

  group('HostStatus.fromJson with the demo fixture', () {
    late HostStatus status;

    setUp(() => status = demoStatus());

    test('given the demo, when parsed, then it is the default setup', () {
      expect(status.host, 'example.com');
      // Hermes does not answer and Heimdall depends on it.
      expect(status.status, Status.degraded);
      expect(status.environments.map((e) => e.id), <String>[
        'development',
        'homologation',
        'production',
      ]);
      expect(status.systems.map((system) => system.id), <String>[
        'heimdall',
        'hermes',
        'fortuna',
        'yggdrasil',
      ]);
      expect(status.systems.map((system) => system.icon), <String?>[
        'shield',
        'mail',
        'savings',
        'park',
      ]);
    });

    test('given the demo, when parsed, then the platform components are the '
        'same in every environment', () {
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

    Map<String, dynamic> firstEnvironment(Map<String, dynamic> json) =>
        (json['environments'] as List<Object>).single as Map<String, dynamic>;

    Map<String, dynamic> firstApplication(Map<String, dynamic> json) =>
        (((firstSystem(json)['environments'] as List<Object>).single
                        as Map<String, dynamic>)['applications']
                    as List<Object>)
                .single
            as Map<String, dynamic>;

    test('given an unknown status string, when parsed, then it reads as '
        'unknown at every level', () {
      final status = HostStatus.fromJson(minimal(status: 'rebooting'));

      expect(status.status, Status.unknown);
      expect(status.environments.single.status, Status.unknown);
      expect(status.systems.single.status, Status.unknown);
      expect(status.systems.single.environments.single.status, Status.unknown);
      expect(onlyApplication(status).status, Status.unknown);
    });

    test('given stopped everywhere, when parsed, then stopped is read at '
        'every level', () {
      final status = HostStatus.fromJson(minimal(status: 'stopped'));

      expect(status.environments.single.status, Status.stopped);
      expect(onlyApplication(status).status, Status.stopped);
      expect(Status.parse('stopped'), Status.stopped);
    });

    test('given an unknown kind, when parsed, then it is other and keeps its '
        'wire value', () {
      final application = onlyApplication(
        HostStatus.fromJson(minimal(kind: 'cron')),
      );

      expect(application.kind, ApplicationKind.other);
      expect(application.kindWire, 'cron');
    });

    test('given a kind that is not a string, when parsed, then it is other '
        'with no wire value', () {
      final application = onlyApplication(
        HostStatus.fromJson(minimal(kind: 7)),
      );

      expect(application.kind, ApplicationKind.other);
      expect(application.kindWire, isNull);
    });

    test('given absent optional objects and fields, when parsed, then they '
        'read as null or empty', () {
      final status = HostStatus.fromJson(minimal());
      final application = onlyApplication(status);

      expect(application.url, isNull);
      expect(application.repository, isNull);
      expect(application.deployment, isNull);
      expect(application.container, isNull);
      expect(application.probe, isNull);
      expect(application.details, isNull);
      expect(status.systems.single.description, isNull);
      expect(status.systems.single.icon, isNull);
      expect(status.systems.single.environments.single.integrations, isEmpty);
    });

    test('given explicit nulls inside objects, when parsed, then they read as '
        'null', () {
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
      expect(application.deployment!.deployedAtText, isNull);
      expect(application.container!.restartCount, isNull);
      expect(application.probe!.healthy, isNull);
      expect(application.probe!.checkedAt, isNull);
    });

    test('given optional objects that are not objects, when parsed, then they '
        'read as null', () {
      final application = onlyApplication(
        HostStatus.fromJson(
          minimal(
            extra: <String, dynamic>{
              'deployment': 'v1',
              'container': <Object>[],
              'probe': true,
              'details': 'reported',
            },
          ),
        ),
      );

      expect(application.deployment, isNull);
      expect(application.container, isNull);
      expect(application.probe, isNull);
      expect(application.details, isNull);
    });

    test('given numbers sent as decimals and a healthy that is not a bool, '
        'when parsed, then the numbers are truncated and healthy is null', () {
      final application = onlyApplication(
        HostStatus.fromJson(
          minimal(
            extra: <String, dynamic>{
              'container': <String, dynamic>{'restartCount': 2.0},
              'probe': <String, dynamic>{
                'healthy': 'yes',
                'statusCode': 200.0,
                'latencyMs': 12.7,
              },
            },
          ),
        ),
      );

      expect(application.container!.restartCount, 2);
      expect(application.probe!.statusCode, 200);
      expect(application.probe!.latencyMs, 12);
      expect(application.probe!.healthy, isNull);
    });

    test(
      'given numbers sent as strings, when parsed, then they read as null',
      () {
        final application = onlyApplication(
          HostStatus.fromJson(
            minimal(
              extra: <String, dynamic>{
                'probe': <String, dynamic>{
                  'statusCode': '200',
                  'latencyMs': '5',
                },
              },
            ),
          ),
        );

        expect(application.probe!.statusCode, isNull);
        expect(application.probe!.latencyMs, isNull);
      },
    );

    test('given a deployedAt that is not a date, when parsed, then it is kept '
        'as written', () {
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

    test('given an unreadable or non-string date, when parsed, then it reads '
        'as null', () {
      expect(
        HostStatus.fromJson(minimal()..['generatedAt'] = 'yesterday')
            .generatedAt,
        isNull,
      );
      expect(
        HostStatus.fromJson(minimal()..['generatedAt'] = 1760000000)
            .generatedAt,
        isNull,
      );
    });

    test('given a date with an offset, when parsed, then it is the same '
        'moment in UTC', () {
      final status = HostStatus.fromJson(
        minimal()..['generatedAt'] = '2026-10-08T15:04:11-03:00',
      );

      expect(status.generatedAt, DateTime.utc(2026, 10, 8, 18, 4, 11));
      expect(status.generatedAt!.isUtc, isTrue);
    });

    test('given an environment without a name and a non-boolean onDemand, '
        'when parsed, then it is called by its id and is not on demand', () {
      final json = minimal();
      firstEnvironment(json)
        ..remove('name')
        ..['onDemand'] = 'yes';
      final environment = HostStatus.fromJson(json).environments.single;

      expect(environment.name, 'homologation');
      // Only a JSON true is on demand.
      expect(environment.onDemand, isFalse);
    });

    test('given an environment with a blank name, when parsed, then it is '
        'called by its id', () {
      final json = minimal();
      firstEnvironment(json)['name'] = '   ';

      expect(
        HostStatus.fromJson(json).environments.single.name,
        'homologation',
      );
    });

    test('given a system environment the host does not list, when parsed, '
        'then it is still shown, after the known ones', () {
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

    test(
      'given system environments in another order than the host, when '
      'parsed, then they follow the host, unknown ones in the order sent',
      () {
        final json = minimal();
        json['environments'] = <Object>[
          for (final id in <String>[
            'development',
            'homologation',
            'production',
          ])
            <String, dynamic>{'id': id, 'status': 'up'},
        ];
        firstSystem(json)['environments'] = <Object>[
          for (final id in <String>[
            'qa',
            'production',
            'staging',
            'development',
            'homologation',
          ])
            <String, dynamic>{
              'environment': id,
              'status': 'up',
              'applications': <Object>[],
            },
        ];

        expect(
          HostStatus.fromJson(json).systems.single.environments
              .map((e) => e.environment),
          <String>[
            'development',
            'homologation',
            'production',
            'qa',
            'staging',
          ],
        );
      },
    );

    test('given unknown extra fields, when parsed, then they are ignored', () {
      final json = minimal()..['future'] = <String, dynamic>{'x': 1};

      expect(HostStatus.fromJson(json).host, 'example.com');
    });

    test('given a response without systems, when parsed, then it is an '
        'invalid response', () {
      expect(
        () => parseStatusBody(
          jsonEncode(<String, dynamic>{
            'host': 'x',
            'environments': <Object>[],
          }),
        ),
        isInvalidResponse,
      );
      expect(
        () =>
            parseStatusBody(jsonEncode(<String, dynamic>{'environment': 'x'})),
        isInvalidResponse,
      );
    });

    test('given a v2 response without a host or environments, when parsed, '
        'then it is an invalid response', () {
      expect(
        () => parseStatusBody(jsonEncode(minimal()..remove('host'))),
        isInvalidResponse,
      );
      expect(
        () => parseStatusBody(jsonEncode(minimal()..['host'] = '')),
        isInvalidResponse,
      );
      expect(
        () => parseStatusBody(jsonEncode(minimal()..remove('environments'))),
        isInvalidResponse,
      );
    });

    test('given a v2 system without its environments list, when parsed, then '
        'it is an invalid response', () {
      final json = minimal();
      firstSystem(json).remove('environments');

      expect(() => parseStatusBody(jsonEncode(json)), isInvalidResponse);
    });

    test('given a system without an id or a name, when parsed, then it is an '
        'invalid response', () {
      for (final key in <String>['id', 'name']) {
        final json = minimal();
        firstSystem(json).remove(key);

        expect(
          () => parseStatusBody(jsonEncode(json)),
          isInvalidResponse,
          reason: key,
        );
      }
    });

    test('given an environment without an id, when parsed, then it is an '
        'invalid response', () {
      final json = minimal();
      firstEnvironment(json).remove('id');

      expect(() => parseStatusBody(jsonEncode(json)), isInvalidResponse);
    });

    test('given an application without an id or a name, when parsed, then it '
        'is an invalid response', () {
      for (final key in <String>['id', 'name']) {
        final json = minimal();
        firstApplication(json).remove(key);

        expect(
          () => parseStatusBody(jsonEncode(json)),
          isInvalidResponse,
          reason: key,
        );
      }
    });

    test('given a list with an element that is not an object, when parsed, '
        'then it is an invalid response', () {
      final json = minimal();
      (json['systems'] as List<Object>).add('heimdall');

      expect(() => parseStatusBody(jsonEncode(json)), isInvalidResponse);
    });

    test('given integrations that are not a list, when parsed, then the '
        'system has none', () {
      final json = minimal();
      ((firstSystem(json)['environments'] as List<Object>).single
              as Map<String, dynamic>)['integrations'] =
          'none';

      expect(
        HostStatus.fromJson(json)
            .systems
            .single
            .environments
            .single
            .integrations,
        isEmpty,
      );
    });
  });

  group('details and integrations', () {
    Map<String, dynamic> withNewFields() {
      final json = jsonDecode(contractExampleJson()) as Map<String, dynamic>;
      final system =
          (json['systems'] as List<dynamic>).first as Map<String, dynamic>;
      final environment =
          (system['environments'] as List<dynamic>).last
              as Map<String, dynamic>;
      final application =
          (environment['applications'] as List<dynamic>).first
              as Map<String, dynamic>;

      system['icon'] = 'shield';
      environment['onDemand'] = true;
      environment['integrations'] = <Object?>[
        <String, Object?>{
          'from': 'heimdall-api',
          'system': 'hermes',
          'external': null,
          'check': 'Hermes',
          'status': 'down',
          'reason': 'connection refused',
        },
        <String, Object?>{
          'from': 'heimdall-api',
          'system': null,
          'external': 'SMTP',
          'check': 'Smtp',
          'status': 'not_configured',
          'reason': null,
        },
      ];
      application['details'] = <String, Object?>{
        'state': 'reported',
        'checkedAt': '2026-10-08T18:04:10Z',
        'reason': null,
        'status': 'Degraded',
        'services': <Object?>[
          <String, Object?>{
            'name': 'Hermes',
            'status': 'Unhealthy',
            'reason': 'connection refused',
          },
          <String, Object?>{'name': 'Database', 'status': 'Healthy'},
        ],
      };

      return json;
    }

    test('given icon, onDemand, integrations and details, when parsed, then '
        'all are read', () {
      final status = HostStatus.fromJson(withNewFields());
      final system = status.systems.first;
      final environment = system.environments.last;
      final details = environment.applications.first.details!;

      expect(system.icon, 'shield');
      expect(environment.onDemand, isTrue);
      expect(environment.integrations, hasLength(2));
      expect(environment.integrations.first.from, 'heimdall-api');
      expect(environment.integrations.first.system, 'hermes');
      expect(environment.integrations.first.external, isNull);
      expect(environment.integrations.first.check, 'Hermes');
      expect(environment.integrations.first.status, IntegrationHealth.down);
      expect(environment.integrations.first.reason, 'connection refused');
      expect(environment.integrations.last.system, isNull);
      expect(environment.integrations.last.external, 'SMTP');
      expect(
        environment.integrations.last.status,
        IntegrationHealth.notConfigured,
      );
      expect(details.state, DetailsState.reported);
      expect(details.checkedAt, DateTime.utc(2026, 10, 8, 18, 4, 10));
      expect(details.status, 'Degraded');
      expect(details.services.map((service) => service.name), <String>[
        'Hermes',
        'Database',
      ]);
      expect(details.services.first.status, 'Unhealthy');
      expect(details.services.first.reason, 'connection refused');
      expect(details.services.last.reason, isNull);
    });

    test('given responses without the new fields, when parsed, then they '
        'still parse with none of them', () {
      for (final status in <HostStatus>[
        parseStatusBody(contractExampleJson()),
        v1Status(),
      ]) {
        for (final system in status.systems) {
          expect(system.icon, isNull);

          for (final environment in system.environments) {
            expect(environment.onDemand, isFalse);
            expect(environment.integrations, isEmpty);

            for (final application in environment.applications) {
              expect(application.details, isNull);
            }
          }
        }
      }
    });

    test('given an unrecognised integration status, when parsed, then it is '
        'unknown', () {
      final json = withNewFields();
      final system =
          (json['systems'] as List<dynamic>).first as Map<String, dynamic>;
      final environment =
          (system['environments'] as List<dynamic>).last
              as Map<String, dynamic>;

      ((environment['integrations'] as List<dynamic>).first
              as Map<String, dynamic>)['status'] =
          'wobbly';

      final status = HostStatus.fromJson(json);

      expect(
        status.systems.first.environments.last.integrations.first.status,
        IntegrationHealth.unknown,
      );
    });

    test('given an integration without a check, when parsed, then the check '
        'is empty', () {
      final integration = IntegrationStatus.fromJson(<String, dynamic>{
        'from': 'heimdall-api',
        'system': 'hermes',
      });

      expect(integration.check, isEmpty);
      expect(integration.status, IntegrationHealth.unknown);
      expect(integration.reason, isNull);
    });

    test('given an integration without its caller, when parsed, then it '
        'throws a format error', () {
      expect(
        () => IntegrationStatus.fromJson(<String, dynamic>{'system': 'hermes'}),
        throwsFormatException,
      );
    });

    test('given an unrecognised details state, when parsed, then it is not '
        'checked with no services', () {
      final details = ApplicationDetails.fromJson(<String, dynamic>{
        'state': 'sleeping',
      });

      expect(details.state, DetailsState.notChecked);
      expect(details.services, isEmpty);
      expect(details.checkedAt, isNull);
    });

    test('given a reported service without a status, when parsed, then the '
        'status is empty', () {
      final service = ReportedService.fromJson(<String, dynamic>{
        'name': 'Cache',
      });

      expect(service.status, isEmpty);
      expect(service.reason, isNull);
    });

    test('given a reported service without a name, when parsed, then it '
        'throws a format error', () {
      expect(
        () => ReportedService.fromJson(<String, dynamic>{'status': 'Healthy'}),
        throwsFormatException,
      );
    });
  });

  group("the fixtures follow the contract's rollup rule", () {
    // docs/status-api.md: the one rule used at every level.
    Status rollup(Iterable<Status> members) {
      final deployed = members.where((s) => s != Status.notDeployed).toList();

      if (deployed.isEmpty) {
        return Status.notDeployed;
      }

      final running = deployed.where((s) => s != Status.stopped).toList();

      if (running.isEmpty) {
        return Status.stopped;
      }

      if (running.every((s) => s == Status.down)) {
        return Status.down;
      }

      if (running.any((s) => s == Status.down || s == Status.degraded)) {
        return Status.degraded;
      }

      return running.contains(Status.unknown) ? Status.unknown : Status.up;
    }

    for (final (name, load) in <(String, HostStatus Function())>[
      ('demo', demoStatus),
      ('contract example', () => parseStatusBody(contractExampleJson())),
      ('v1 example', v1Status),
    ]) {
      test('given the $name fixture, when each level is rolled up, then it '
          'matches the status the fixture states', () {
        final host = load();

        // Platform components count as stopped where the environment is.
        final stopped = <String>{
          for (final e in host.environments)
            if (e.status == Status.stopped) e.id,
        };

        for (final system in host.systems) {
          for (final environment in system.environments) {
            expect(
              environment.status,
              rollup(
                environment.applications.map(
                  (a) =>
                      a.kind == ApplicationKind.platform &&
                          stopped.contains(environment.environment)
                      ? Status.stopped
                      : a.status,
                ),
              ),
              reason: '${system.id} in ${environment.environment}',
            );
          }

          expect(
            system.status,
            rollup(system.environments.map((e) => e.status)),
            reason: system.id,
          );
        }

        expect(host.status, rollup(host.environments.map((e) => e.status)));
      });
    }
  });
}
