import 'package:flutter/material.dart';

import '../../../../core/format/time_format.dart';
import '../../../../core/widgets/links.dart';
import '../../domain/status.dart';
import '../layout.dart';
import 'status_visuals.dart';

/// Shows every field of [application]: a bottom sheet on phones, a dialog on
/// anything wider.
Future<void> showApplicationDetail(
  BuildContext context, {
  required SystemStatus system,
  required ApplicationStatus application,
  String? environmentName,
}) {
  final width = MediaQuery.sizeOf(context).width;

  if (width < Breakpoints.tablet) {
    return showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      useSafeArea: true,
      showDragHandle: true,
      builder: (context) => DraggableScrollableSheet(
        expand: false,
        initialChildSize: 0.75,
        minChildSize: 0.4,
        maxChildSize: 1,
        builder: (context, controller) => ApplicationDetail(
          system: system,
          application: application,
          environmentName: environmentName,
          scrollController: controller,
        ),
      ),
    );
  }

  return showDialog<void>(
    context: context,
    builder: (context) => Dialog(
      clipBehavior: Clip.antiAlias,
      child: ConstrainedBox(
        constraints: BoxConstraints(
          maxWidth: 600,
          maxHeight: MediaQuery.sizeOf(context).height * 0.85,
        ),
        child: ApplicationDetail(
          system: system,
          application: application,
          environmentName: environmentName,
          onClose: () => Navigator.of(context).pop(),
        ),
      ),
    ),
  );
}

class ApplicationDetail extends StatelessWidget {
  const ApplicationDetail({
    super.key,
    required this.system,
    required this.application,
    this.environmentName,
    this.scrollController,
    this.onClose,
  });

  final SystemStatus system;
  final ApplicationStatus application;

  /// The environment the application is in, when known.
  final String? environmentName;
  final ScrollController? scrollController;
  final VoidCallback? onClose;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final deployment = application.deployment;
    final container = application.container;
    final probe = application.probe;

    return ListView(
      controller: scrollController,
      shrinkWrap: scrollController == null,
      padding: const EdgeInsets.fromLTRB(20, 8, 20, 24),
      children: <Widget>[
        Row(
          children: <Widget>[
            Icon(kindIcon(application.kind), size: 28),
            const SizedBox(width: 12),
            Expanded(
              child: Text(application.name, style: theme.textTheme.titleLarge),
            ),
            if (onClose != null)
              IconButton(
                tooltip: 'Close',
                onPressed: onClose,
                icon: const Icon(Icons.close),
              ),
          ],
        ),
        const SizedBox(height: 8),
        Wrap(
          spacing: 8,
          runSpacing: 8,
          crossAxisAlignment: WrapCrossAlignment.center,
          children: <Widget>[
            StatusChip(application.status),
            Text(
              <String>[
                system.name,
                ?environmentName,
                application.id,
              ].join(' · '),
            ),
          ],
        ),
        if (application.url != null || application.repository != null) ...[
          const SizedBox(height: 8),
          Wrap(
            spacing: 4,
            children: <Widget>[
              if (application.url != null)
                LinkButton(
                  icon: Icons.open_in_new,
                  label: 'Open',
                  url: application.url!,
                ),
              if (application.repository != null)
                LinkButton(
                  icon: Icons.code,
                  label: 'Repository',
                  url: application.repository!,
                ),
            ],
          ),
        ],
        _Section(
          title: 'Application',
          rows: <(String, String?)>[
            ('Id', application.id),
            if (environmentName != null) ('Environment', environmentName),
            ('Kind', kindLabel(application.kind, application.kindWire)),
            (
              'Status',
              StatusVisual.of(application.status, theme.brightness).label,
            ),
            ('URL', application.url),
            ('Repository', application.repository),
          ],
        ),
        _Section(
          title: 'Deployment',
          emptyText: deployment == null ? 'No container on this host.' : null,
          rows: <(String, String?)>[
            if (deployment != null) ...[
              ('Version', deployment.version),
              ('Commit', deployment.commit),
              (
                'Deployed',
                _moment(deployment.deployedAt) ?? deployment.deployedAtText,
              ),
              ('Image', deployment.image),
            ],
          ],
        ),
        _Section(
          title: 'Container',
          emptyText: container == null ? 'No container on this host.' : null,
          rows: <(String, String?)>[
            if (container != null) ...[
              ('State', container.state),
              ('Health', container.health ?? 'no health check'),
              ('Started', _moment(container.startedAt)),
              ('Restarts', container.restartCount?.toString()),
            ],
          ],
        ),
        _Section(
          title: 'Probe',
          emptyText: probe == null
              ? switch (application.status) {
                  Status.notDeployed =>
                    'Not probed: not deployed on this host.',
                  Status.stopped =>
                    'Not probed: stopped (an on-demand environment).',
                  _ => 'No probe result.',
                }
              : null,
          rows: <(String, String?)>[
            if (probe != null) ...[
              (
                'Healthy',
                probe.healthy == null ? null : (probe.healthy! ? 'yes' : 'no'),
              ),
              ('HTTP status', probe.statusCode?.toString()),
              (
                'Latency',
                probe.latencyMs == null
                    ? null
                    : formatLatency(probe.latencyMs!),
              ),
              ('Checked', _moment(probe.checkedAt)),
              ('Error', probe.error),
            ],
          ],
        ),
      ],
    );
  }

  static String? _moment(DateTime? moment) => moment == null
      ? null
      : '${formatAgo(moment, DateTime.now())} · ${formatAbsolute(moment)}';
}

class _Section extends StatelessWidget {
  const _Section({required this.title, required this.rows, this.emptyText});

  final String title;
  final List<(String, String?)> rows;
  final String? emptyText;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);

    return Padding(
      padding: const EdgeInsets.only(top: 20),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: <Widget>[
          Text(
            title,
            style: theme.textTheme.titleSmall?.copyWith(
              color: theme.colorScheme.primary,
            ),
          ),
          const SizedBox(height: 6),
          if (emptyText != null)
            Text(emptyText!, style: theme.textTheme.bodyMedium),
          for (final (label, value) in rows)
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 3),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: <Widget>[
                  SizedBox(
                    width: 104,
                    child: Text(
                      label,
                      style: theme.textTheme.bodyMedium?.copyWith(
                        color: theme.colorScheme.onSurfaceVariant,
                      ),
                    ),
                  ),
                  Expanded(
                    child: SelectableText(
                      value ?? '—',
                      style: theme.textTheme.bodyMedium,
                    ),
                  ),
                ],
              ),
            ),
        ],
      ),
    );
  }
}
