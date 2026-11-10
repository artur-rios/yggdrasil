/// The `GET /api/status` response, as `docs/status-api.md` defines it.
///
/// Pure Dart: nothing here imports Flutter, so the parsing is tested without a
/// widget binding. Parsing is strict about what identifies an element (ids,
/// names, the environments, systems and applications lists) and lenient about
/// everything the contract allows to be `null`, so a status API that adds
/// fields or a status value this build does not know yet still renders.
///
/// The contract is v2: one host, the environments it runs, and per system one
/// entry per environment. A v1 response (one environment per host: a top-level
/// `environment` and `systems[].applications`) is read as a host with that one
/// environment, so the console still shows a host that was not upgraded.
library;

/// The status of an application, a system, an environment or a whole host.
///
/// The declaration order is not the severity order; see [severity].
enum Status {
  up('up'),
  degraded('degraded'),
  down('down'),
  stopped('stopped'),
  notDeployed('not_deployed'),
  unknown('unknown');

  const Status(this.wire);

  /// The value on the wire.
  final String wire;

  /// Reads a wire value. Anything unrecognised is [Status.unknown]: the console
  /// must not break when the API learns a new state.
  static Status parse(Object? value) {
    for (final status in values) {
      if (status.wire == value) {
        return status;
      }
    }

    return Status.unknown;
  }

  /// How bad the status is, for sorting worst-first.
  ///
  /// The contract ranks `down` > `degraded` > `unknown` > `up`, with `stopped`
  /// (an on-demand environment that is off) and `not_deployed` neutral; for
  /// display they sort last, below `up`, `stopped` first.
  int get severity => switch (this) {
    Status.down => 5,
    Status.degraded => 4,
    Status.unknown => 3,
    Status.up => 2,
    Status.stopped => 1,
    Status.notDeployed => 0,
  };

  /// Whether this status needs somebody's attention. `stopped` does not: an
  /// on-demand environment is meant to be off when nobody uses it.
  bool get isProblem =>
      this == Status.down || this == Status.degraded || this == Status.unknown;
}

/// What an application is, which decides the icon the console shows.
enum ApplicationKind {
  api('api'),
  web('web'),
  worker('worker'),
  platform('platform'),
  other('other');

  const ApplicationKind(this.wire);

  final String wire;

  static ApplicationKind parse(Object? value) {
    for (final kind in values) {
      if (kind != ApplicationKind.other && kind.wire == value) {
        return kind;
      }
    }

    return ApplicationKind.other;
  }
}

/// The whole host in one response: every environment it runs and every
/// system in them.
class HostStatus {
  const HostStatus({
    required this.host,
    required this.generatedAt,
    required this.status,
    required this.environments,
    required this.systems,
    this.contractVersion = 2,
  });

  /// Reads a v2 response, or a v1 one (recognised by having neither `host`
  /// nor `environments`, see [HostStatus.fromV1Json]).
  factory HostStatus.fromJson(Map<String, dynamic> json) {
    if (!json.containsKey('host') && !json.containsKey('environments')) {
      return HostStatus.fromV1Json(json);
    }

    final environments = _list(json, 'environments', HostEnvironment.fromJson);
    final order = <String, int>{
      for (final (index, environment) in environments.indexed)
        environment.id: index,
    };

    return HostStatus(
      host: _requiredString(json, 'host'),
      generatedAt: _date(json['generatedAt']),
      status: Status.parse(json['status']),
      environments: environments,
      systems: _list(
        json,
        'systems',
        (system) => SystemStatus.fromJson(system, order),
      ),
    );
  }

  /// Reads a v1 response as a host with one environment: the response's
  /// `environment`, named `environmentName` (else its id), which also names
  /// the host. Each system's applications become that environment's.
  factory HostStatus.fromV1Json(Map<String, dynamic> json) {
    final id = _requiredString(json, 'environment');
    final name = _string(json['environmentName']) ?? id;
    final status = Status.parse(json['status']);

    return HostStatus(
      host: name,
      generatedAt: _date(json['generatedAt']),
      status: status,
      environments: <HostEnvironment>[
        HostEnvironment(id: id, name: name, onDemand: false, status: status),
      ],
      systems: _list(
        json,
        'systems',
        (system) => SystemStatus.fromV1Json(system, id),
      ),
      contractVersion: 1,
    );
  }

  /// The host's display name: the `host` of a v2 response (its domain, e.g.
  /// `example.com`), the environment's name in a v1 one.
  final String host;

  /// When the status API finished the refresh these results come from. `null`
  /// only when the value was missing or unreadable.
  final DateTime? generatedAt;

  final Status status;

  /// The environments the host runs, in catalog order.
  final List<HostEnvironment> environments;

  final List<SystemStatus> systems;

  /// `2`, or `1` for a response converted by [HostStatus.fromV1Json].
  final int contractVersion;

  /// The environment with [id], or `null` when the host does not list it.
  HostEnvironment? environment(String id) {
    for (final environment in environments) {
      if (environment.id == id) {
        return environment;
      }
    }

    return null;
  }

  /// What to call the environment with [id]: its name, else the id itself.
  String environmentName(String id) => environment(id)?.name ?? id;
}

/// One environment of the host, e.g. `production`.
class HostEnvironment {
  const HostEnvironment({
    required this.id,
    required this.name,
    required this.onDemand,
    required this.status,
  });

  factory HostEnvironment.fromJson(Map<String, dynamic> json) {
    final id = _requiredString(json, 'id');

    return HostEnvironment(
      id: id,
      name: _nonEmptyString(json['name']) ?? id,
      onDemand: json['onDemand'] == true,
      status: Status.parse(json['status']),
    );
  }

  /// The environment's id in the catalog, e.g. `production`.
  final String id;

  /// The display name, e.g. `Production`; the id when the API sent none.
  final String name;

  /// Started only when used: `stopped` is its normal state when idle.
  final bool onDemand;

  final Status status;
}

/// One system: a product made of one or more applications, deployed to one or
/// more of the host's environments.
class SystemStatus {
  const SystemStatus({
    required this.id,
    required this.name,
    required this.status,
    required this.environments,
    this.description,
    this.icon,
  });

  /// [order] maps environment ids to their position on the host, so the
  /// entries follow the host's environment order whatever the API sent.
  factory SystemStatus.fromJson(
    Map<String, dynamic> json, [
    Map<String, int> order = const <String, int>{},
  ]) {
    final environments = _list(
      json,
      'environments',
      SystemEnvironment.fromJson,
    );
    final indexed = environments.indexed.toList()
      ..sort((a, b) {
        final byHost = (order[a.$2.environment] ?? order.length).compareTo(
          order[b.$2.environment] ?? order.length,
        );

        return byHost != 0 ? byHost : a.$1.compareTo(b.$1);
      });

    return SystemStatus(
      id: _requiredString(json, 'id'),
      name: _requiredString(json, 'name'),
      description: _string(json['description']),
      icon: _string(json['icon']),
      status: Status.parse(json['status']),
      environments: <SystemEnvironment>[for (final (_, e) in indexed) e],
    );
  }

  /// A v1 system: its applications all in [environment], with the system's
  /// status.
  factory SystemStatus.fromV1Json(
    Map<String, dynamic> json,
    String environment,
  ) {
    final status = Status.parse(json['status']);

    return SystemStatus(
      id: _requiredString(json, 'id'),
      name: _requiredString(json, 'name'),
      description: _string(json['description']),
      status: status,
      environments: <SystemEnvironment>[
        SystemEnvironment(
          environment: environment,
          status: status,
          applications: _list(json, 'applications', ApplicationStatus.fromJson),
        ),
      ],
    );
  }

  final String id;
  final String name;
  final String? description;

  /// The catalog's icon name for the system, or `null` when it has none (or
  /// the status API predates the field).
  final String? icon;

  /// The rollup of [environments]: the overall status the map shows.
  final Status status;

  /// The host's environments this system has applications in, in the host's
  /// order.
  final List<SystemEnvironment> environments;

  /// Every application of every environment, environment by environment.
  List<ApplicationStatus> get applications => <ApplicationStatus>[
    for (final environment in environments) ...environment.applications,
  ];
}

/// A system in one environment: the applications deployed there.
class SystemEnvironment {
  const SystemEnvironment({
    required this.environment,
    required this.status,
    required this.applications,
    this.onDemand = false,
    this.integrations = const <IntegrationStatus>[],
  });

  factory SystemEnvironment.fromJson(Map<String, dynamic> json) =>
      SystemEnvironment(
        environment: _requiredString(json, 'environment'),
        status: Status.parse(json['status']),
        applications: _list(json, 'applications', ApplicationStatus.fromJson),
        onDemand: json['onDemand'] == true,
        integrations: _optionalList(
          json,
          'integrations',
          IntegrationStatus.fromJson,
        ),
      );

  /// The environment's id, as in [HostStatus.environments].
  final String environment;

  final Status status;
  final List<ApplicationStatus> applications;

  /// Whether any application of the system is on demand in this environment.
  /// Per system, not the environment's own flag: an application can override
  /// it.
  final bool onDemand;

  /// The system's integrations here, in catalog order; empty when it has none
  /// or the status API predates the field.
  final List<IntegrationStatus> integrations;
}

/// The health of an integration: a caller's dependency on another system or
/// an external service.
enum IntegrationHealth {
  up('up'),
  down('down'),
  notConfigured('not_configured'),
  stopped('stopped'),
  unknown('unknown');

  const IntegrationHealth(this.wire);

  final String wire;

  /// Anything unrecognised is [IntegrationHealth.unknown], as for [Status].
  static IntegrationHealth parse(Object? value) {
    for (final health in values) {
      if (health.wire == value) {
        return health;
      }
    }

    return IntegrationHealth.unknown;
  }
}

/// One integration of a system in an environment: [from] depends on a system
/// or on an external service.
class IntegrationStatus {
  const IntegrationStatus({
    required this.from,
    required this.check,
    required this.status,
    this.system,
    this.external,
    this.reason,
  });

  factory IntegrationStatus.fromJson(Map<String, dynamic> json) =>
      IntegrationStatus(
        from: _requiredString(json, 'from'),
        system: _string(json['system']),
        external: _string(json['external']),
        check: _string(json['check']) ?? '',
        status: IntegrationHealth.parse(json['status']),
        reason: _string(json['reason']),
      );

  /// The calling application's id.
  final String from;

  /// The target system's id, or `null` for an external one.
  final String? system;

  /// The external target's display name, or `null` for a system target.
  final String? external;

  /// The name of the entry in the caller's detailed health report.
  final String check;

  final IntegrationHealth status;

  /// Why the integration is `down`, or `unknown` for a caller that did not
  /// answer; otherwise `null`.
  final String? reason;
}

/// One deployable unit of a system.
class ApplicationStatus {
  const ApplicationStatus({
    required this.id,
    required this.name,
    required this.kind,
    required this.status,
    this.kindWire,
    this.url,
    this.repository,
    this.deployment,
    this.container,
    this.probe,
    this.details,
  });

  factory ApplicationStatus.fromJson(Map<String, dynamic> json) =>
      ApplicationStatus(
        id: _requiredString(json, 'id'),
        name: _requiredString(json, 'name'),
        kind: ApplicationKind.parse(json['kind']),
        kindWire: _string(json['kind']),
        status: Status.parse(json['status']),
        url: _string(json['url']),
        repository: _string(json['repository']),
        deployment: _object(json['deployment'], Deployment.fromJson),
        container: _object(json['container'], ContainerInfo.fromJson),
        probe: _object(json['probe'], Probe.fromJson),
        details: _object(json['details'], ApplicationDetails.fromJson),
      );

  final String id;
  final String name;
  final ApplicationKind kind;

  /// The kind exactly as the API sent it, for when it is not one this build
  /// knows.
  final String? kindWire;

  final Status status;

  /// The public address, or `null` when the application is not public.
  final String? url;

  /// The repository's page on the git server (Forgejo or GitHub), or `null` for platform components.
  final String? repository;

  /// `null` when there is no container.
  final Deployment? deployment;

  /// `null` when there is no container.
  final ContainerInfo? container;

  /// `null` for `not_deployed` applications, which are not probed.
  final Probe? probe;

  /// The detailed health report the status API holds, or `null` when the
  /// application has none configured (or the status API predates the field).
  final ApplicationDetails? details;
}

/// What the status API holds of an application's last detailed health fetch.
enum DetailsState {
  reported('reported'),
  unavailable('unavailable'),
  unsupported('unsupported'),
  notChecked('not_checked');

  const DetailsState(this.wire);

  final String wire;

  /// Anything unrecognised is [DetailsState.notChecked], so an older console
  /// shows nothing for a state it does not know instead of breaking.
  static DetailsState parse(Object? value) {
    for (final state in values) {
      if (state.wire == value) {
        return state;
      }
    }

    return DetailsState.notChecked;
  }
}

/// An application's detailed health: the report's overall status and one
/// entry per dependency.
class ApplicationDetails {
  const ApplicationDetails({
    required this.state,
    this.checkedAt,
    this.reason,
    this.status,
    this.services = const <ReportedService>[],
  });

  factory ApplicationDetails.fromJson(Map<String, dynamic> json) =>
      ApplicationDetails(
        state: DetailsState.parse(json['state']),
        checkedAt: _date(json['checkedAt']),
        reason: _string(json['reason']),
        status: _string(json['status']),
        services: _optionalList(json, 'services', ReportedService.fromJson),
      );

  final DetailsState state;

  /// `null` for [DetailsState.notChecked].
  final DateTime? checkedAt;

  /// Why the fetch failed, for [DetailsState.unavailable].
  final String? reason;

  /// The report's status as written (`Healthy`, `Degraded`, ...); `null`
  /// unless [state] is [DetailsState.reported].
  final String? status;

  final List<ReportedService> services;
}

/// One entry of a detailed health report.
class ReportedService {
  const ReportedService({
    required this.name,
    required this.status,
    this.reason,
  });

  factory ReportedService.fromJson(Map<String, dynamic> json) =>
      ReportedService(
        name: _requiredString(json, 'name'),
        status: _string(json['status']) ?? '',
        reason: _string(json['reason']),
      );

  final String name;

  /// As written by the application: `Healthy`, `Degraded`, `Unhealthy` or
  /// `NotConfigured`.
  final String status;

  final String? reason;
}

/// What was deployed, read from the container's `yggdrasil.*` labels.
class Deployment {
  const Deployment({
    this.version,
    this.commit,
    this.deployedAt,
    this.deployedAtText,
    this.image,
  });

  factory Deployment.fromJson(Map<String, dynamic> json) => Deployment(
    version: _string(json['version']),
    commit: _string(json['commit']),
    deployedAt: _date(json['deployedAt']),
    deployedAtText: _string(json['deployedAt']),
    image: _string(json['image']),
  );

  final String? version;
  final String? commit;

  /// `null` when absent or when the label is not a date.
  final DateTime? deployedAt;

  /// The value as sent: the API passes a label that is not a date through
  /// as written, and the detail view shows it rather than nothing.
  final String? deployedAtText;

  final String? image;
}

/// The container as the Docker API reports it.
class ContainerInfo {
  const ContainerInfo({
    this.state,
    this.health,
    this.startedAt,
    this.restartCount,
  });

  factory ContainerInfo.fromJson(Map<String, dynamic> json) => ContainerInfo(
    state: _string(json['state']),
    health: _string(json['health']),
    startedAt: _date(json['startedAt']),
    restartCount: _int(json['restartCount']),
  );

  /// Docker's state, e.g. `running` or `exited`.
  final String? state;

  /// `healthy`, `unhealthy`, `starting`, or `null` when there is no health
  /// check.
  final String? health;

  final DateTime? startedAt;
  final int? restartCount;
}

/// The last health probe of the application.
class Probe {
  const Probe({
    this.healthy,
    this.statusCode,
    this.latencyMs,
    this.checkedAt,
    this.error,
  });

  factory Probe.fromJson(Map<String, dynamic> json) => Probe(
    healthy: json['healthy'] is bool ? json['healthy'] as bool : null,
    statusCode: _int(json['statusCode']),
    latencyMs: _int(json['latencyMs']),
    checkedAt: _date(json['checkedAt']),
    error: _string(json['error']),
  );

  final bool? healthy;

  /// The HTTP answer, `null` when none came back.
  final int? statusCode;

  final int? latencyMs;
  final DateTime? checkedAt;

  /// Why no HTTP answer came back (`timeout`, `connection refused`, ...), or
  /// `null` whenever one did, even a failing one: then [statusCode] says it.
  final String? error;
}

String _requiredString(Map<String, dynamic> json, String key) {
  final value = json[key];

  if (value is String && value.isNotEmpty) {
    return value;
  }

  throw FormatException('Expected a non-empty string in "$key"', json);
}

String? _string(Object? value) => value is String ? value : null;

String? _nonEmptyString(Object? value) =>
    value is String && value.trim().isNotEmpty ? value : null;

int? _int(Object? value) => value is num ? value.toInt() : null;

DateTime? _date(Object? value) =>
    value is String ? DateTime.tryParse(value)?.toUtc() : null;

T? _object<T>(Object? value, T Function(Map<String, dynamic>) parse) =>
    value is Map<String, dynamic> ? parse(value) : null;

List<T> _list<T>(
  Map<String, dynamic> json,
  String key,
  T Function(Map<String, dynamic>) parse,
) {
  final value = json[key];

  if (value is! List<dynamic>) {
    throw FormatException('Expected a list in "$key"', json);
  }

  return <T>[
    for (final element in value)
      if (element is Map<String, dynamic>)
        parse(element)
      else
        throw FormatException('Expected an object in "$key"', element),
  ];
}

/// Like [_list], but a missing or non-list value is an empty list: the field
/// is optional in the contract.
List<T> _optionalList<T>(
  Map<String, dynamic> json,
  String key,
  T Function(Map<String, dynamic>) parse,
) => json[key] is List<dynamic> ? _list(json, key, parse) : <T>[];
