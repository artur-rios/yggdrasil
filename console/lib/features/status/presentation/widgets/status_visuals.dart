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
