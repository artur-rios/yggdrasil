import 'dart:math' as math;

import 'package:flutter/material.dart';

import '../../domain/status.dart';
import '../../domain/status_logic.dart';
import '../layout.dart';
import '../widgets/status_visuals.dart';
import 'map_layout.dart';
import 'system_icons.dart';

/// Tall enough for the circle, the name, the "on demand" line and, between
/// two rows, a pill that clears them all.
const double _cellHeight = 160;

const double _maxCellWidth = 200;

/// Below this cell width (a phone), the circle shrinks so a pill still fits
/// between two circles.
const double _compactCellWidth = 120;

const double _circleDiameter = 64;
const double _compactCircleDiameter = 48;

/// Above the circle in its cell.
const double _circleInset = 8;

/// Between a circle and the end of a line.
const double _lineGap = 2;

/// How close to a line a tap has to be to count.
const double _lineTapDistance = 12;

const double _pillSlotHeight = 40;

/// A pill's widest on a phone: "Not configured" takes two lines there.
const double _compactPillWidth = 80;

/// How far above a level line a pill rides on a phone.
const double _compactPillLift = 14;

/// Pills sharing a line stack this far apart.
const double _pillStep = 22;

/// From the top-left of a cell to the centre of its circle.
Offset _circleOffset(double cellWidth, double diameter) =>
    Offset(cellWidth / 2, _circleInset + diameter / 2);

/// How an integration's line and pill look. Each health has its own glyph and
/// word, and the two grey ones their own dash, so none depends on colour.
class IntegrationVisual {
  const IntegrationVisual({
    required this.label,
    required this.icon,
    required this.foreground,
    required this.container,
    this.dash,
  });

  factory IntegrationVisual.of(
    IntegrationHealth health,
    Brightness brightness,
  ) {
    IntegrationVisual tinted(
      Status tint,
      String label,
      IconData icon, [
      List<double>? dash,
    ]) {
      final visual = StatusVisual.of(tint, brightness);

      return IntegrationVisual(
        label: label,
        icon: icon,
        foreground: visual.foreground,
        container: visual.container,
        dash: dash,
      );
    }

    return switch (health) {
      IntegrationHealth.up => tinted(Status.up, 'OK', Icons.check),
      IntegrationHealth.down => tinted(Status.down, 'Failing', Icons.close),
      IntegrationHealth.unknown => tinted(
        Status.unknown,
        'Unknown',
        Icons.question_mark,
      ),
      // Grey like "not deployed": neither is a problem.
      IntegrationHealth.stopped => tinted(
        Status.notDeployed,
        'Stopped',
        Icons.pause,
        const <double>[8, 5],
      ),
      IntegrationHealth.notConfigured => tinted(
        Status.notDeployed,
        'Not configured',
        Icons.remove,
        const <double>[3, 4],
      ),
    };
  }

  /// What the pill says.
  final String label;

  final IconData icon;
  final Color foreground;
  final Color container;

  /// Painted and unpainted lengths of a dashed line; `null` for a solid one.
  final List<double>? dash;
}

/// One system per icon on an even grid, with an arrow from caller to target
/// for each integration, for one environment of the host.
///
/// Where everything sits comes from the host as a whole, so a system keeps its
/// place when the environment changes; one not deployed there leaves its cell
/// empty.
class SystemMap extends StatelessWidget {
  const SystemMap({
    super.key,
    required this.host,
    required this.environmentId,
    required this.onOpenSystem,
  });

  final HostStatus host;

  /// The host environment on screen, as in [HostEnvironment.id].
  final String environmentId;

  /// Called with the id of the system tapped, or of the caller of the line
  /// tapped.
  final ValueChanged<String> onOpenSystem;

  @override
  Widget build(BuildContext context) => LayoutBuilder(
    builder: (context, constraints) {
      final drawing = _MapDrawing.of(
        host,
        environmentId,
        constraints.maxWidth,
        Theme.of(context).brightness,
      );

      if (drawing.systems.isEmpty) {
        return Padding(
          padding: const EdgeInsets.symmetric(vertical: 32),
          child: Center(
            child: Text(
              host.systems.isEmpty
                  ? 'This host has no systems.'
                  : 'Nothing is deployed in '
                        '${host.environmentName(environmentId)}.',
              textAlign: TextAlign.center,
            ),
          ),
        );
      }

      return Center(
        child: _MapCanvas(drawing: drawing, onOpenSystem: onOpenSystem),
      );
    },
  );
}

/// A system to draw, with its state in the selected environment.
class _DrawnSystem {
  const _DrawnSystem(this.system, this.environment, this.cell);

  final SystemStatus system;
  final SystemEnvironment environment;

  /// The top-left corner of its cell.
  final Offset cell;
}

class _DrawnExternal {
  const _DrawnExternal(this.name, this.cell);

  final String name;
  final Offset cell;
}

class _DrawnLine {
  const _DrawnLine({
    required this.callerId,
    required this.targetKey,
    required this.label,
    required this.visual,
    required this.start,
    required this.end,
    required this.pill,
  });

  final String callerId;

  /// The target's system id or external name.
  final String targetKey;

  /// What a screen reader says.
  final String label;

  final IntegrationVisual visual;
  final Offset start;
  final Offset end;

  /// The centre of its pill.
  final Offset pill;
}

/// What is on screen for one environment, in the map's own coordinates.
class _MapDrawing {
  const _MapDrawing({
    required this.size,
    required this.cellWidth,
    required this.pillWidth,
    required this.diameter,
    required this.systems,
    required this.externals,
    required this.lines,
  });

  factory _MapDrawing.of(
    HostStatus host,
    String environmentId,
    double width,
    Brightness brightness,
  ) {
    final columns = mapColumnsFor(width);
    final layout = computeMapLayout(host, columns);
    final cellWidth = math.min(width / columns, _maxCellWidth);
    final compact = cellWidth < _compactCellWidth;
    final diameter = compact ? _compactCircleDiameter : _circleDiameter;

    // A rollup of not_deployed means there is no container for any of its
    // applications here: as absent as a system the catalog leaves out.
    final deployed = <SystemNode, (SystemStatus, SystemEnvironment)>{
      for (final system in host.systems)
        for (final environment in system.environments)
          if (environment.environment == environmentId &&
              environment.status != Status.notDeployed)
            SystemNode(system.id): (system, environment),
    };

    final connections = <(SystemNode, MapNode, IntegrationStatus)>[
      for (final (_, environment) in deployed.values)
        for (final integration in environment.integrations)
          if (layout.endsOf(integration) case (final from, final to)?)
            if (deployed.containsKey(from) &&
                (to is ExternalNode || deployed.containsKey(to)))
              (from, to, integration),
    ];

    final shown = <MapNode>{
      ...deployed.keys,
      for (final (_, to, _) in connections) to,
    };
    final drawnRows = layout.drawnRows(shown);

    Offset cellOf(MapNode node) {
      final (row, column) = layout.cellOf(node);

      return Offset(column * cellWidth, drawnRows[row]! * _cellHeight);
    }

    Offset centreOf(MapNode node) =>
        cellOf(node) + _circleOffset(cellWidth, diameter);

    String nameOf(MapNode node) => switch (node) {
      SystemNode() => deployed[node]!.$1.name,
      ExternalNode(:final name) => name,
    };

    final lines = <_DrawnLine>[];

    for (final (from, to, integration) in connections) {
      final a = centreOf(from);
      final b = centreOf(to);
      final inset = (b - a) / (b - a).distance * (diameter / 2 + _lineGap);
      final visual = IntegrationVisual.of(integration.status, brightness);
      final reason = integration.reason;

      // Integrations between the same two nodes share one line. Stacking
      // their pills keeps a failure from hiding behind an OK.
      bool sameLine((SystemNode, MapNode, IntegrationStatus) other) =>
          (other.$1 == from && other.$2 == to) ||
          (other.$1 == to && other.$2 == from);
      final sharing = connections.where(sameLine).toList();

      // On a phone a pill is as wide as the gap between two circles: on a
      // level line it would hide the arrow, so it rides just above.
      final lift = compact && a.dy == b.dy ? _compactPillLift : 0.0;
      final stack =
          sharing.indexWhere((other) => identical(other.$3, integration)) -
          (sharing.length - 1) / 2;

      lines.add(
        _DrawnLine(
          callerId: from.id,
          targetKey: switch (to) {
            SystemNode(:final id) => id,
            ExternalNode(:final name) => name,
          },
          label:
              '${nameOf(from)} to ${nameOf(to)}: '
              '${visual.label.toLowerCase()}'
              '${reason == null ? '' : ', $reason'}',
          visual: visual,
          start: a + inset,
          end: b - inset,
          pill: (a + b) / 2 + Offset(0, stack * _pillStep - lift),
        ),
      );
    }

    return _MapDrawing(
      size: Size(columns * cellWidth, drawnRows.length * _cellHeight),
      cellWidth: cellWidth,
      pillWidth: compact ? _compactPillWidth : cellWidth,
      diameter: diameter,
      systems: <_DrawnSystem>[
        for (final MapEntry(key: node, value: (system, environment))
            in deployed.entries)
          _DrawnSystem(system, environment, cellOf(node)),
      ],
      externals: <_DrawnExternal>[
        for (final node in shown.whereType<ExternalNode>())
          _DrawnExternal(node.name, cellOf(node)),
      ],
      lines: lines,
    );
  }

  final Size size;
  final double cellWidth;
  final double pillWidth;
  final double diameter;
  final List<_DrawnSystem> systems;
  final List<_DrawnExternal> externals;
  final List<_DrawnLine> lines;

  bool isOnExternal(Offset point) => externals.any(
    (external) =>
        (point - external.cell - _circleOffset(cellWidth, diameter)).distance <=
        diameter / 2,
  );

  /// The line a tap at [point] means: the nearest within reach.
  _DrawnLine? lineAt(Offset point) {
    _DrawnLine? nearest;
    var nearestDistance = _lineTapDistance;

    for (final line in lines) {
      final distance = distanceToSegment(point, line.start, line.end);

      if (distance <= nearestDistance) {
        nearest = line;
        nearestDistance = distance;
      }
    }

    return nearest;
  }
}

class _MapCanvas extends StatelessWidget {
  const _MapCanvas({required this.drawing, required this.onOpenSystem});

  final _MapDrawing drawing;
  final ValueChanged<String> onOpenSystem;

  void _onTapUp(TapUpDetails details) {
    final point = details.localPosition;

    if (drawing.isOnExternal(point)) {
      return;
    }

    final line = drawing.lineAt(point);

    if (line != null) {
      onOpenSystem(line.callerId);
    }
  }

  @override
  Widget build(BuildContext context) {
    final cellWidth = drawing.cellWidth;

    // Tiles sit in the detector's way, so a tap on a system goes to the
    // system first.
    return GestureDetector(
      behavior: HitTestBehavior.opaque,
      excludeFromSemantics: true,
      onTapUp: _onTapUp,
      child: SizedBox.fromSize(
        size: drawing.size,
        child: Stack(
          clipBehavior: Clip.none,
          children: <Widget>[
            Positioned.fill(
              child: CustomPaint(painter: _LinesPainter(drawing.lines)),
            ),
            for (final drawn in drawing.systems)
              Positioned(
                left: drawn.cell.dx,
                top: drawn.cell.dy + _circleInset,
                width: cellWidth,
                child: _SystemTile(
                  system: drawn.system,
                  environment: drawn.environment,
                  diameter: drawing.diameter,
                  onOpen: () => onOpenSystem(drawn.system.id),
                ),
              ),
            for (final drawn in drawing.externals)
              Positioned(
                left: drawn.cell.dx,
                top: drawn.cell.dy + _circleInset,
                width: cellWidth,
                child: _ExternalTile(
                  name: drawn.name,
                  diameter: drawing.diameter,
                ),
              ),
            // Above the tiles so a word is never cut by a circle, but they
            // take no taps of their own: the line beneath them does.
            for (final line in drawing.lines)
              Positioned(
                left: line.pill.dx - drawing.pillWidth / 2,
                top: line.pill.dy - _pillSlotHeight / 2,
                width: drawing.pillWidth,
                height: _pillSlotHeight,
                child: Semantics(
                  key: ValueKey<String>(
                    'map-line-${line.callerId}-${line.targetKey}',
                  ),
                  container: true,
                  label: line.label,
                  excludeSemantics: true,
                  child: IgnorePointer(
                    child: Center(child: _LinePill(visual: line.visual)),
                  ),
                ),
              ),
          ],
        ),
      ),
    );
  }
}

class _SystemTile extends StatelessWidget {
  const _SystemTile({
    required this.system,
    required this.environment,
    required this.diameter,
    required this.onOpen,
  });

  final SystemStatus system;
  final SystemEnvironment environment;
  final double diameter;
  final VoidCallback onOpen;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final visual = StatusVisual.of(environment.status, theme.brightness);
    final onDemand = environment.onDemand;
    final label = <String>[
      system.name,
      statusLabel(environment.status).toLowerCase(),
      if (onDemand) 'on demand',
    ].join(', ');

    return Semantics(
      container: true,
      button: true,
      label: label,
      onTap: onOpen,
      excludeSemantics: true,
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: <Widget>[
          SizedBox.square(
            dimension: diameter,
            child: Stack(
              clipBehavior: Clip.none,
              children: <Widget>[
                Positioned.fill(
                  child: Material(
                    color: visual.container,
                    shape: const CircleBorder(),
                    child: InkWell(
                      key: ValueKey<String>('map-system-${system.id}'),
                      customBorder: const CircleBorder(),
                      onTap: onOpen,
                      child: Icon(
                        systemIcon(system.icon),
                        size: diameter / 2,
                        color: visual.foreground,
                      ),
                    ),
                  ),
                ),
                Positioned(
                  right: -2,
                  bottom: -2,
                  child: IgnorePointer(
                    child: DecoratedBox(
                      decoration: BoxDecoration(
                        shape: BoxShape.circle,
                        color: theme.scaffoldBackgroundColor,
                      ),
                      child: Padding(
                        padding: const EdgeInsets.all(1),
                        child: StatusIcon(environment.status),
                      ),
                    ),
                  ),
                ),
              ],
            ),
          ),
          // Only the words are tappable, not the width of the cell, or a line
          // running past would stop taking taps.
          GestureDetector(
            onTap: onOpen,
            child: _Caption(
              name: system.name,
              detail: onDemand ? const _OnDemandLabel() : null,
            ),
          ),
        ],
      ),
    );
  }
}

class _ExternalTile extends StatelessWidget {
  const _ExternalTile({required this.name, required this.diameter});

  final String name;
  final double diameter;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final color = theme.colorScheme.onSurfaceVariant;

    return Semantics(
      container: true,
      label: '$name, external',
      excludeSemantics: true,
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: <Widget>[
          SizedBox.square(
            key: ValueKey<String>('map-external-$name'),
            dimension: diameter,
            child: CustomPaint(
              painter: _DashedCirclePainter(theme.colorScheme.outline),
              child: Icon(
                systemIcon('cloud'),
                size: diameter / 2,
                color: color,
              ),
            ),
          ),
          _Caption(
            name: name,
            detail: Text(
              'external',
              style: theme.textTheme.bodySmall?.copyWith(color: color),
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
            ),
          ),
        ],
      ),
    );
  }
}

/// The name under a circle and, under the name, [detail].
///
/// On the page's own background: a line going down from the circle above
/// passes behind it.
class _Caption extends StatelessWidget {
  const _Caption({required this.name, this.detail});

  final String name;
  final Widget? detail;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);

    return Padding(
      padding: const EdgeInsets.only(top: 4),
      child: DecoratedBox(
        decoration: BoxDecoration(color: theme.scaffoldBackgroundColor),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: <Widget>[
            Text(
              name,
              style: theme.textTheme.labelLarge,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              textAlign: TextAlign.center,
            ),
            ?detail,
          ],
        ),
      ),
    );
  }
}

class _OnDemandLabel extends StatelessWidget {
  const _OnDemandLabel();

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);

    return Row(
      mainAxisSize: MainAxisSize.min,
      children: <Widget>[
        Icon(
          Icons.power_settings_new,
          size: 12,
          color: theme.colorScheme.outline,
        ),
        const SizedBox(width: 3),
        Flexible(
          child: Text(
            'on demand',
            style: theme.textTheme.bodySmall?.copyWith(
              color: theme.colorScheme.onSurfaceVariant,
            ),
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
          ),
        ),
      ],
    );
  }
}

class _LinePill extends StatelessWidget {
  const _LinePill({required this.visual});

  final IntegrationVisual visual;

  @override
  Widget build(BuildContext context) => DecoratedBox(
    decoration: BoxDecoration(
      color: visual.container,
      borderRadius: BorderRadius.circular(8),
      border: Border.all(color: visual.foreground.withValues(alpha: 0.35)),
    ),
    child: Padding(
      padding: const EdgeInsets.symmetric(horizontal: 5, vertical: 1),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: <Widget>[
          Icon(visual.icon, size: 11, color: visual.foreground),
          const SizedBox(width: 2),
          // Two lines at most: "Not configured" is wider than a phone's gap
          // between two circles.
          Flexible(
            child: Text(
              visual.label,
              style: TextStyle(
                fontSize: 10,
                height: 1.1,
                fontWeight: FontWeight.w600,
                color: visual.foreground,
              ),
              maxLines: 2,
              overflow: TextOverflow.ellipsis,
              textAlign: TextAlign.center,
            ),
          ),
        ],
      ),
    ),
  );
}

/// Arrows from caller to target, solid or dashed as each line's visual says.
class _LinesPainter extends CustomPainter {
  const _LinesPainter(this.lines);

  static const double _headLength = 9;
  static const double _headHalfWidth = 5;

  final List<_DrawnLine> lines;

  @override
  void paint(Canvas canvas, Size size) {
    for (final line in lines) {
      final along = line.end - line.start;
      final direction = along / along.distance;
      final across = Offset(-direction.dy, direction.dx) * _headHalfWidth;
      final headBase = line.end - direction * _headLength;
      final color = line.visual.foreground;
      final stroke = Paint()
        ..color = color
        ..strokeWidth = 2
        ..style = PaintingStyle.stroke;

      _stroke(canvas, line.start, headBase, line.visual.dash, stroke);
      canvas.drawPath(
        Path()
          ..moveTo(line.end.dx, line.end.dy)
          ..lineTo(headBase.dx + across.dx, headBase.dy + across.dy)
          ..lineTo(headBase.dx - across.dx, headBase.dy - across.dy)
          ..close(),
        Paint()..color = color,
      );
    }
  }

  void _stroke(
    Canvas canvas,
    Offset from,
    Offset to,
    List<double>? dash,
    Paint paint,
  ) {
    if (dash == null) {
      canvas.drawLine(from, to, paint);

      return;
    }

    final length = (to - from).distance;
    final direction = (to - from) / length;

    for (var at = 0.0; at < length; at += dash[0] + dash[1]) {
      canvas.drawLine(
        from + direction * at,
        from + direction * math.min(at + dash[0], length),
        paint,
      );
    }
  }

  @override
  bool shouldRepaint(_LinesPainter oldDelegate) => oldDelegate.lines != lines;
}

/// The outline of an external: dashed, as nothing here checks its health.
class _DashedCirclePainter extends CustomPainter {
  const _DashedCirclePainter(this.color);

  static const int _dashes = 16;

  final Color color;

  @override
  void paint(Canvas canvas, Size size) {
    final rect = Offset.zero & size;
    final paint = Paint()
      ..color = color
      ..strokeWidth = 1.5
      ..style = PaintingStyle.stroke;
    const step = 2 * math.pi / _dashes;

    for (var i = 0; i < _dashes; i++) {
      canvas.drawArc(rect.deflate(1), i * step, step * 0.6, false, paint);
    }
  }

  @override
  bool shouldRepaint(_DashedCirclePainter oldDelegate) =>
      oldDelegate.color != color;
}
