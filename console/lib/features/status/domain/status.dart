/// The `GET /api/status` response, as `docs/status-api.md` defines it.
///
/// Pure Dart: nothing here imports Flutter, so the parsing is tested without a
/// widget binding. Parsing is strict about what identifies an element (ids,
/// names, the systems and applications lists) and lenient about everything the
/// contract allows to be `null`, so a status API that adds fields or a status
/// value this build does not know yet still renders.
library;

/// The status of an application, a system or a whole environment.
///
/// The declaration order is not the severity order; see [severity].
enum Status {
  up('up'),
  degraded('degraded'),
  down('down'),
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
  /// The contract ranks `down` > `degraded` > `unknown` > `up`, with
  /// `not_deployed` neutral; for display it sorts last, below `up`.
  int get severity => switch (this) {
    Status.down => 4,
    Status.degraded => 3,
    Status.unknown => 2,
    Status.up => 1,
    Status.notDeployed => 0,
  };

  /// Whether this status needs somebody's attention.
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

/// The whole environment in one response.
class EnvironmentStatus {
  const EnvironmentStatus({
    required this.environment,
    required this.generatedAt,
    required this.status,
    required this.systems,
    this.environmentName,
  });

  factory EnvironmentStatus.fromJson(Map<String, dynamic> json) =>
      EnvironmentStatus(
        environment: _requiredString(json, 'environment'),
        environmentName: _string(json['environmentName']),
        generatedAt: _date(json['generatedAt']),
        status: Status.parse(json['status']),
        systems: _list(json, 'systems', SystemStatus.fromJson),
      );

  /// The environment's id in the catalog, e.g. `production`.
  final String environment;

  /// The environment's display name, e.g. `Production`. `null` from a status
  /// API that predates the field.
  final String? environmentName;

  /// What to call the environment: its display name, else its id.
  String get displayName => environmentName ?? environment;

  /// When the status API finished the refresh these results come from. `null`
  /// only when the value was missing or unreadable.
  final DateTime? generatedAt;

  final Status status;
  final List<SystemStatus> systems;
}

/// One system: a product made of one or more applications.
class SystemStatus {
  const SystemStatus({
    required this.id,
    required this.name,
    required this.status,
    required this.applications,
    this.description,
  });

  factory SystemStatus.fromJson(Map<String, dynamic> json) => SystemStatus(
    id: _requiredString(json, 'id'),
    name: _requiredString(json, 'name'),
    description: _string(json['description']),
    status: Status.parse(json['status']),
    applications: _list(json, 'applications', ApplicationStatus.fromJson),
  );

  final String id;
  final String name;
  final String? description;
  final Status status;
  final List<ApplicationStatus> applications;
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

  /// The GitHub repository, or `null` for platform components.
  final String? repository;

  /// `null` when there is no container.
  final Deployment? deployment;

  /// `null` when there is no container.
  final ContainerInfo? container;

  /// `null` for `not_deployed` applications, which are not probed.
  final Probe? probe;
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
