import 'status.dart';

/// Applications in the same order as systems: worst first, then by name.
List<ApplicationStatus> sortApplications(
  Iterable<ApplicationStatus> applications,
) => applications.toList()
  ..sort((a, b) => _compare(a.status, a.name, a.id, b.status, b.name, b.id));

int _compare(
  Status statusA,
  String nameA,
  String idA,
  Status statusB,
  String nameB,
  String idB,
) {
  final bySeverity = statusB.severity.compareTo(statusA.severity);

  if (bySeverity != 0) {
    return bySeverity;
  }

  final byName = nameA.toLowerCase().compareTo(nameB.toLowerCase());

  return byName != 0 ? byName : idA.compareTo(idB);
}

/// How many applications of each status, in severity order, zeros omitted.
Map<Status, int> countByStatus(Iterable<ApplicationStatus> applications) {
  final counts = <Status, int>{};

  for (final application in applications) {
    counts[application.status] = (counts[application.status] ?? 0) + 1;
  }

  final ordered = counts.keys.toList()
    ..sort((a, b) => b.severity.compareTo(a.severity));

  return <Status, int>{for (final status in ordered) status: counts[status]!};
}

/// The one-line summary of a host, e.g.
/// `3 systems · 3 environments · 1 down · 2 degraded`.
///
/// Only problems are counted: stopped and not deployed applications are
/// normal on a host with on-demand environments. A platform component appears
/// in every environment's report with the same result; it is counted once.
String hostSummary(HostStatus host) {
  final systems = host.systems.length;
  final environments = host.environments.length;
  final parts = <String>[
    '$systems ${systems == 1 ? 'system' : 'systems'}',
    '$environments ${environments == 1 ? 'environment' : 'environments'}',
  ];
  final seen = <String>{};
  final applications = <ApplicationStatus>[
    for (final system in host.systems)
      for (final environment in system.environments)
        for (final application in environment.applications)
          if (seen.add(
            application.kind == ApplicationKind.platform
                ? application.id
                : '${environment.environment}/${application.id}',
          ))
            application,
  ];

  for (final entry in countByStatus(applications).entries) {
    if (entry.key.isProblem) {
      parts.add('${entry.value} ${statusLabel(entry.key).toLowerCase()}');
    }
  }

  return parts.join(' · ');
}

/// The environment of [host] to show: the [remembered] one if the host still
/// has it, else the last in catalog order (production, normally). `null` when
/// the host reports no environments.
HostEnvironment? resolveHostEnvironment(HostStatus host, String? remembered) {
  for (final environment in host.environments) {
    if (environment.id == remembered) {
      return environment;
    }
  }

  return host.environments.isEmpty ? null : host.environments.last;
}

/// The one-line summary beside the environment selector, e.g. `1 integration
/// failing`; empty when none is failing.
///
/// Only `down` counts: stopped and not-configured integrations are normal,
/// and an unknown one is not known to be failing.
String environmentSummaryLine(HostStatus host, String environmentId) {
  final failing = <IntegrationStatus>[
    for (final system in host.systems)
      for (final environment in system.environments)
        if (environment.environment == environmentId)
          for (final integration in environment.integrations)
            if (integration.status == IntegrationHealth.down) integration,
  ].length;

  return switch (failing) {
    0 => '',
    1 => '1 integration failing',
    _ => '$failing integrations failing',
  };
}

/// The label shown next to a status icon.
String statusLabel(Status status) => switch (status) {
  Status.up => 'Up',
  Status.degraded => 'Degraded',
  Status.down => 'Down',
  Status.stopped => 'Stopped',
  Status.notDeployed => 'Not deployed',
  Status.unknown => 'Unknown',
};
