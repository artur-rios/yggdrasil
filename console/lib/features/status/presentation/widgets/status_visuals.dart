import 'package:flutter/material.dart';

import '../../domain/status.dart';
import '../../domain/status_logic.dart';

/// How a status looks. Every status has its own icon shape and a label, so
/// none of them depends on colour alone.
class StatusVisual {
  const StatusVisual({
    required this.icon,
    required this.label,
    required this.foreground,
    required this.container,
  });

  factory StatusVisual.of(Status status, Brightness brightness) {
    final dark = brightness == Brightness.dark;

    return switch (status) {
      Status.up => StatusVisual(
        icon: Icons.check_circle,
        label: statusLabel(status),
        foreground: dark ? const Color(0xFF81C995) : const Color(0xFF1B6B2A),
        container: dark ? const Color(0xFF1C3A24) : const Color(0xFFDCF2DF),
      ),
      Status.degraded => StatusVisual(
        icon: Icons.warning_rounded,
        label: statusLabel(status),
        foreground: dark ? const Color(0xFFF2C14E) : const Color(0xFF7A4F00),
        container: dark ? const Color(0xFF45360E) : const Color(0xFFFCE8B2),
      ),
      Status.down => StatusVisual(
        icon: Icons.cancel,
        label: statusLabel(status),
        foreground: dark ? const Color(0xFFFFB4AB) : const Color(0xFFB3261E),
        container: dark ? const Color(0xFF5C1A16) : const Color(0xFFFFDAD6),
      ),
      // Neutral like not deployed, but its own shape and a cooler tint: an
      // on-demand environment that is off, not something missing.
      Status.stopped => StatusVisual(
        icon: Icons.pause_circle_outline,
        label: statusLabel(status),
        foreground: dark ? const Color(0xFFB4C8D6) : const Color(0xFF41566A),
        container: dark ? const Color(0xFF2B353D) : const Color(0xFFE2EAF0),
      ),
      Status.notDeployed => StatusVisual(
        icon: Icons.remove_circle_outline,
        label: statusLabel(status),
        foreground: dark ? const Color(0xFFC4C7C5) : const Color(0xFF5E6360),
        container: dark ? const Color(0xFF333735) : const Color(0xFFE7EAE8),
      ),
      Status.unknown => StatusVisual(
        icon: Icons.help,
        label: statusLabel(status),
        foreground: dark ? const Color(0xFFCDBEFF) : const Color(0xFF5B4B8A),
        container: dark ? const Color(0xFF3A3155) : const Color(0xFFEAE2FB),
      ),
    };
  }

  final IconData icon;
  final String label;
  final Color foreground;
  final Color container;
}

class StatusIcon extends StatelessWidget {
  const StatusIcon(this.status, {super.key, this.size = 20});

  final Status status;
  final double size;

  @override
  Widget build(BuildContext context) {
    final visual = StatusVisual.of(status, Theme.of(context).brightness);

    return Icon(
      visual.icon,
      size: size,
      color: visual.foreground,
      semanticLabel: visual.label,
    );
  }
}

/// Icon and label on the status's own tint.
class StatusChip extends StatelessWidget {
  const StatusChip(this.status, {super.key});

  final Status status;

  @override
  Widget build(BuildContext context) {
    final visual = StatusVisual.of(status, Theme.of(context).brightness);

    return Semantics(
      label: 'Status: ${visual.label}',
      excludeSemantics: true,
      child: DecoratedBox(
        decoration: BoxDecoration(
          color: visual.container,
          borderRadius: BorderRadius.circular(8),
        ),
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: <Widget>[
              Icon(visual.icon, size: 16, color: visual.foreground),
              const SizedBox(width: 4),
              Text(
                visual.label,
                style: Theme.of(context).textTheme.labelMedium?.copyWith(
                  color: visual.foreground,
                  fontWeight: FontWeight.w600,
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

/// An environment's name and status on the status's own tint, e.g.
/// `✓ Production · Up`; with [onDemand], the tooltip says so.
class EnvironmentStatusChip extends StatelessWidget {
  const EnvironmentStatusChip({
    super.key,
    required this.name,
    required this.status,
    this.onDemand = false,
    this.detail,
  });

  final String name;
  final Status status;
  final bool onDemand;

  /// More for the tooltip, e.g. the applications' summary.
  final String? detail;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final visual = StatusVisual.of(status, theme.brightness);
    final style = theme.textTheme.labelMedium?.copyWith(
      color: visual.foreground,
    );
    final tooltip = <String>[
      '$name: ${visual.label}${onDemand ? ' (on demand)' : ''}',
      ?detail,
    ].join('\n');

    return Tooltip(
      message: tooltip,
      excludeFromSemantics: true,
      child: Semantics(
        label: '$name: ${visual.label}',
        excludeSemantics: true,
        child: DecoratedBox(
          decoration: BoxDecoration(
            color: visual.container,
            borderRadius: BorderRadius.circular(8),
            border: Border.all(
              color: visual.foreground.withValues(alpha: 0.35),
            ),
          ),
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: <Widget>[
                Icon(visual.icon, size: 16, color: visual.foreground),
                const SizedBox(width: 4),
                Flexible(
                  child: Text.rich(
                    TextSpan(
                      children: <InlineSpan>[
                        TextSpan(text: name),
                        TextSpan(
                          text: ' · ${visual.label}',
                          style: const TextStyle(fontWeight: FontWeight.w600),
                        ),
                      ],
                    ),
                    style: style,
                    overflow: TextOverflow.ellipsis,
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

IconData kindIcon(ApplicationKind kind) => switch (kind) {
  ApplicationKind.api => Icons.api,
  ApplicationKind.web => Icons.web,
  ApplicationKind.worker => Icons.engineering,
  ApplicationKind.platform => Icons.dns,
  ApplicationKind.other => Icons.widgets_outlined,
};

String kindLabel(ApplicationKind kind, [String? wire]) => switch (kind) {
  ApplicationKind.api => 'API',
  ApplicationKind.web => 'Web',
  ApplicationKind.worker => 'Worker',
  ApplicationKind.platform => 'Platform',
  ApplicationKind.other => wire ?? 'Other',
};
