import 'dart:math' as math;
import 'dart:ui' show Offset;

import '../../domain/status.dart';

/// Something the map places in a cell.
sealed class MapNode {
  const MapNode();
}

/// A system of the host, by id.
final class SystemNode extends MapNode {
  const SystemNode(this.id);

  final String id;

  @override
  bool operator ==(Object other) => other is SystemNode && other.id == id;

  @override
  int get hashCode => Object.hash(SystemNode, id);

  @override
  String toString() => 'SystemNode($id)';
}

/// Something outside the catalog, by display name.
final class ExternalNode extends MapNode {
  const ExternalNode(this.name);

  final String name;

  @override
  bool operator ==(Object other) => other is ExternalNode && other.name == name;

  @override
  int get hashCode => Object.hash(ExternalNode, name);

  @override
  String toString() => 'ExternalNode($name)';
}

/// Where every node of a host sits, whichever environment is on screen.
class MapLayout {
  const MapLayout._(this._cells, this._owners, this._systems);

  final Map<MapNode, (int row, int column)> _cells;

  /// Application id to the id of the system it belongs to.
  final Map<String, String> _owners;

  final Set<String> _systems;

  /// Every placed node, in path order.
  Iterable<MapNode> get nodes => _cells.keys;

  int get rows => _cells.isEmpty
      ? 0
      : _cells.values.map((cell) => cell.$1).reduce(math.max) + 1;

  (int row, int column) cellOf(MapNode node) => _cells[node]!;

  /// The nodes [integration] joins, or `null` when it cannot be drawn.
  (SystemNode from, MapNode to)? endsOf(IntegrationStatus integration) =>
      _endsOf(integration, _owners, _systems);

  /// Where each row with one of [shown] in it is drawn: rows with none
  /// collapse, so an environment with few systems leaves no gaps.
  ///
  /// Asked of the layout by whoever draws it, so that the layout itself stays
  /// the same for every environment.
  Map<int, int> drawnRows(Iterable<MapNode> shown) {
    final rows = <int>{for (final node in shown) cellOf(node).$1}.toList()
      ..sort();

    return <int, int>{for (final (drawn, row) in rows.indexed) row: drawn};
  }
}

/// Places every system of [host] and everything their integrations reach, in
/// any environment, on a grid of [columns] columns.
///
/// Connected groups come first, each walked depth-first from where its calls
/// start, then the systems with no integration, all in catalog order. The
/// nodes then run along a boustrophedon path (row 0 left to right, row 1 right
/// to left and so on), so consecutive nodes are always in adjacent cells and a
/// chain of integrations gets short lines that cross nothing.
MapLayout computeMapLayout(HostStatus host, int columns) {
  final catalog = <SystemNode>[
    for (final system in host.systems) SystemNode(system.id),
  ];
  final systems = <String>{for (final node in catalog) node.id};
  final owners = <String, String>{
    for (final system in host.systems)
      for (final environment in system.environments)
        for (final application in environment.applications)
          application.id: system.id,
  };

  // The same integration in several environments is one edge. Sets keep the
  // order of first appearance, which is catalog order.
  final calls = <MapNode, Set<MapNode>>{};
  final callers = <MapNode, Set<MapNode>>{};

  for (final system in host.systems) {
    for (final environment in system.environments) {
      for (final integration in environment.integrations) {
        final ends = _endsOf(integration, owners, systems);

        if (ends != null) {
          final (from, to) = ends;
          (calls[from] ??= <MapNode>{}).add(to);
          (callers[to] ??= <MapNode>{}).add(from);
        }
      }
    }
  }

  Iterable<MapNode> neighbours(MapNode node) => <MapNode>[
    ...?calls[node],
    ...?callers[node],
  ];

  final path = <MapNode>{};

  void walk(MapNode node) {
    if (path.add(node)) {
      neighbours(node).forEach(walk);
    }
  }

  for (final node in catalog) {
    if (path.contains(node) || neighbours(node).isEmpty) {
      continue;
    }

    final group = <MapNode>{};

    void reach(MapNode member) {
      if (group.add(member)) {
        neighbours(member).forEach(reach);
      }
    }

    reach(node);

    // Start where the calls start, not at the first system of the catalog:
    // a chain walked from its middle would leave its head far from the next
    // link. Only a cycle has no such system.
    final groupCallers = catalog.where(
      (system) => group.contains(system) && calls.containsKey(system),
    );
    walk(
      groupCallers.firstWhere(
        (system) => !callers.containsKey(system),
        orElse: () => groupCallers.first,
      ),
    );
  }

  path.addAll(catalog);

  return MapLayout._(
    <MapNode, (int, int)>{
      for (final (index, node) in path.indexed)
        node: (
          index ~/ columns,
          (index ~/ columns).isEven
              ? index % columns
              : columns - 1 - index % columns,
        ),
    },
    owners,
    systems,
  );
}

/// The caller's system and the target of [integration]; `null` for a caller or
/// target the host does not have, and for a system calling itself, which has
/// no line to draw.
(SystemNode, MapNode)? _endsOf(
  IntegrationStatus integration,
  Map<String, String> owners,
  Set<String> systems,
) {
  final owner = owners[integration.from];
  final system = integration.system;
  final external = integration.external;

  if (owner == null || owner == system) {
    return null;
  }

  if (system != null) {
    return systems.contains(system)
        ? (SystemNode(owner), SystemNode(system))
        : null;
  }

  return external == null ? null : (SystemNode(owner), ExternalNode(external));
}

/// How far [p] is from the nearest point of the segment from [a] to [b].
double distanceToSegment(Offset p, Offset a, Offset b) {
  final along = b - a;
  final lengthSquared = along.distanceSquared;

  if (lengthSquared == 0) {
    return (p - a).distance;
  }

  final position =
      ((p.dx - a.dx) * along.dx + (p.dy - a.dy) * along.dy) / lengthSquared;

  return (p - (a + along * position.clamp(0.0, 1.0))).distance;
}
