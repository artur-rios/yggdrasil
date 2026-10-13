import 'status.dart';

/// Systems ordered worst status first, then by name (case-insensitive), then
/// by id so the order is total and stable across refreshes.
List<SystemStatus> sortSystems(Iterable<SystemStatus> systems) =>
    systems.toList()..sort(compareByStatusThenName);

/// Applications in the same order as systems: worst first, then by name.
List<ApplicationStatus> sortApplications(
  Iterable<ApplicationStatus> applications,
) => applications.toList()
  ..sort((a, b) => _compare(a.status, a.name, a.id, b.status, b.name, b.id));

int compareByStatusThenName(SystemStatus a, SystemStatus b) =>
    _compare(a.status, a.name, a.id, b.status, b.name, b.id);

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

/// The systems shown for the "problems only" filter: those whose overall
/// status needs attention. A stopped on-demand environment is not a problem.
List<SystemStatus> visibleSystems(
  Iterable<SystemStatus> systems, {
  required bool problemsOnly,
}) {
  final sorted = sortSystems(systems);

  if (!problemsOnly) {
    return sorted;
  }

  return sorted.where(hasProblem).toList();
}

/// Whether the system's overall status (the rollup of its environments)
/// needs attention.
bool hasProblem(SystemStatus system) => system.status.isProblem;

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

/// The one-line summary of some applications, e.g. `2 applications · 1
/// degraded`.
///
/// Every status other than `up` is counted; when all are up, or all stopped,
/// it says so.
String applicationsSummary(Iterable<ApplicationStatus> applications) {
  final list = applications.toList();
  final total = list.length;
  final parts = <String>[
    '$total ${total == 1 ? 'application' : 'applications'}',
  ];

  if (total == 0) {
    return parts.single;
  }

  final counts = countByStatus(list);
  final only = counts.length == 1 ? counts.keys.single : null;

  if (only == Status.up || only == Status.stopped) {
    final label = statusLabel(only!).toLowerCase();
    parts.add(total == 1 ? label : 'all $label');
  } else {
    for (final entry in counts.entries) {
      if (entry.key != Status.up) {
        parts.add('${entry.value} ${statusLabel(entry.key).toLowerCase()}');
      }
    }
  }

  return parts.join(' · ');
}

/// The one-line summary of a system in one environment, e.g.
/// `2 applications · all up`.
String environmentSummary(SystemEnvironment environment) =>
    applicationsSummary(environment.applications);

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

/// The label shown next to a status icon.
String statusLabel(Status status) => switch (status) {
  Status.up => 'Up',
  Status.degraded => 'Degraded',
  Status.down => 'Down',
  Status.stopped => 'Stopped',
  Status.notDeployed => 'Not deployed',
  Status.unknown => 'Unknown',
};
