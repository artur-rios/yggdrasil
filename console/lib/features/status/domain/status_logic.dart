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

/// The systems shown for the "problems only" filter: those whose own status,
/// or any application's, needs attention.
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

bool hasProblem(SystemStatus system) =>
    system.status.isProblem ||
    system.applications.any((application) => application.status.isProblem);

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

/// The one-line summary of a system, e.g. `2 applications · 1 degraded`.
///
/// Every status other than `up` is counted; when all are up it says so.
String systemSummary(SystemStatus system) {
  final total = system.applications.length;
  final parts = <String>[
    '$total ${total == 1 ? 'application' : 'applications'}',
  ];

  if (total == 0) {
    return parts.single;
  }

  final counts = countByStatus(system.applications);

  if (counts.length == 1 && counts.containsKey(Status.up)) {
    parts.add(total == 1 ? 'up' : 'all up');
  } else {
    for (final entry in counts.entries) {
      if (entry.key != Status.up) {
        parts.add('${entry.value} ${statusLabel(entry.key).toLowerCase()}');
      }
    }
  }

  return parts.join(' · ');
}

/// The one-line summary of an environment, e.g.
/// `4 systems · 13 applications · 1 down · 2 degraded`.
String environmentSummary(EnvironmentStatus environment) {
  final applications = <ApplicationStatus>[
    for (final system in environment.systems) ...system.applications,
  ];
  final systems = environment.systems.length;
  final parts = <String>[
    '$systems ${systems == 1 ? 'system' : 'systems'}',
    '${applications.length} '
        '${applications.length == 1 ? 'application' : 'applications'}',
  ];

  for (final entry in countByStatus(applications).entries) {
    if (entry.key != Status.up) {
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
  Status.notDeployed => 'Not deployed',
  Status.unknown => 'Unknown',
};
