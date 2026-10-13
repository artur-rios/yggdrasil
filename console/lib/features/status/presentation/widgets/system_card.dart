import 'package:flutter/material.dart';

import '../../../../core/format/time_format.dart';
import '../../../../core/widgets/links.dart';
import '../../../../core/widgets/time_widgets.dart';
import '../../domain/status.dart';
import '../../domain/status_logic.dart';
import 'application_detail.dart';
import 'status_visuals.dart';

/// One system: its status, name, description and one chip per environment;
/// expanded, one section per environment with a row per application.
class SystemCard extends StatelessWidget {
  const SystemCard({
    super.key,
    required this.host,
    required this.system,
    required this.expanded,
    required this.onToggle,
    required this.now,
  });

  /// The response the system comes from, for the environments' names.
  final HostStatus host;
  final SystemStatus system;
  final bool expanded;
  final VoidCallback onToggle;
  final DateTime now;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);

    return Card(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: <Widget>[
          Semantics(
            button: true,
            expanded: expanded,
            child: InkWell(
              key: ValueKey<String>('system-${system.id}'),
              onTap: onToggle,
              child: Padding(
                padding: const EdgeInsets.fromLTRB(16, 14, 8, 14),
                child: Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: <Widget>[
                    Padding(
                      padding: const EdgeInsets.only(top: 2),
                      child: StatusIcon(system.status, size: 24),
                    ),
                    const SizedBox(width: 12),
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: <Widget>[
                          Text(
                            system.name,
                            style: theme.textTheme.titleMedium,
                            overflow: TextOverflow.ellipsis,
                          ),
                          if (system.description != null)
                            Text(
                              system.description!,
                              style: theme.textTheme.bodyMedium?.copyWith(
                                color: theme.colorScheme.onSurfaceVariant,
                              ),
                            ),
                          const SizedBox(height: 8),
                          Wrap(
                            spacing: 6,
                            runSpacing: 6,
                            children: <Widget>[
                              for (final environment in system.environments)
                                EnvironmentStatusChip(
                                  key: ValueKey<String>(
                                    'chip-${system.id}-'
                                    '${environment.environment}',
                                  ),
                                  name: host.environmentName(
                                    environment.environment,
                                  ),
                                  status: environment.status,
                                  onDemand:
                                      host
                                          .environment(environment.environment)
                                          ?.onDemand ??
                                      false,
                                  detail: environmentSummary(environment),
                                ),
                            ],
                          ),
                        ],
                      ),
                    ),
                    AnimatedRotation(
                      turns: expanded ? 0.5 : 0,
                      duration: const Duration(milliseconds: 200),
                      child: Icon(
                        Icons.expand_more,
                        semanticLabel: expanded
                            ? 'Collapse ${system.name}'
                            : 'Expand ${system.name}',
                      ),
                    ),
                  ],
                ),
              ),
            ),
          ),
          AnimatedSize(
            duration: const Duration(milliseconds: 200),
            alignment: Alignment.topCenter,
            child: expanded
                ? Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: <Widget>[
                      for (final environment in system.environments)
                        EnvironmentSection(
                          key: ValueKey<String>(
                            'section-${system.id}-${environment.environment}',
                          ),
                          system: system,
                          environment: environment,
                          name: host.environmentName(environment.environment),
                          onDemand:
                              host
                                  .environment(environment.environment)
                                  ?.onDemand ??
                              false,
                          now: now,
                        ),
                    ],
                  )
                : const SizedBox(width: double.infinity),
          ),
        ],
      ),
    );
  }
}

/// A system in one environment, inside an expanded card: the environment's
/// name, status and summary, then a row per application.
class EnvironmentSection extends StatelessWidget {
  const EnvironmentSection({
    super.key,
    required this.system,
    required this.environment,
    required this.name,
    required this.onDemand,
    required this.now,
  });

  final SystemStatus system;
  final SystemEnvironment environment;

  /// The environment's display name.
  final String name;
  final bool onDemand;
  final DateTime now;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final scheme = theme.colorScheme;
    final muted = theme.textTheme.bodySmall?.copyWith(
      color: scheme.onSurfaceVariant,
    );
    final applications = sortApplications(environment.applications);

    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: <Widget>[
        const Divider(height: 1),
        ColoredBox(
          color: scheme.surfaceContainerLow,
          child: Padding(
            padding: const EdgeInsets.fromLTRB(16, 10, 16, 10),
            child: Wrap(
              spacing: 10,
              runSpacing: 4,
              crossAxisAlignment: WrapCrossAlignment.center,
              children: <Widget>[
                Semantics(
                  header: true,
                  child: Text(name, style: theme.textTheme.titleSmall),
                ),
                StatusChip(environment.status),
                if (onDemand)
                  Tooltip(
                    message:
                        'Started only when used; stopped is its normal '
                        'state when idle.',
                    child: Row(
                      mainAxisSize: MainAxisSize.min,
                      children: <Widget>[
                        Icon(
                          Icons.power_settings_new,
                          size: 14,
                          color: scheme.outline,
                        ),
                        const SizedBox(width: 3),
                        Text('on demand', style: muted),
                      ],
                    ),
                  ),
                Text(environmentSummary(environment), style: muted),
              ],
            ),
          ),
        ),
        if (applications.isEmpty)
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 10, 16, 12),
            child: Text('No applications in $name.', style: muted),
          ),
        for (final (index, application) in applications.indexed) ...[
          if (index > 0) const Divider(height: 1, indent: 16),
          ApplicationRow(
            system: system,
            environmentId: environment.environment,
            environmentName: name,
            application: application,
            now: now,
          ),
        ],
      ],
    );
  }
}

/// One application inside an expanded system.
class ApplicationRow extends StatelessWidget {
  const ApplicationRow({
    super.key,
    required this.system,
    required this.application,
    required this.now,
    this.environmentId,
    this.environmentName,
  });

  final SystemStatus system;
  final ApplicationStatus application;

  /// The environment the application is in, for its key and detail view.
  final String? environmentId;
  final String? environmentName;
  final DateTime now;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final small = theme.textTheme.bodySmall;
    final deployment = application.deployment;
    final container = application.container;
    final probe = application.probe;

    final version = <String>[
      if (deployment?.version != null) 'v${deployment!.version}',
      if (deployment?.commit != null) deployment!.commit!,
    ].join(' · ');

    final containerText = container == null
        ? 'no container'
        : <String>[
            container.state ?? 'unknown state',
            if (container.health != null) container.health!,
            if (container.restartCount != null)
              '${container.restartCount} '
                  '${container.restartCount == 1 ? 'restart' : 'restarts'}',
          ].join(' · ');

    final probeText = probe == null ? null : describeProbe(probe);

    return InkWell(
      key: ValueKey<String>(
        environmentId == null
            ? 'application-${application.id}'
            : 'application-$environmentId-${application.id}',
      ),
      onTap: () => showApplicationDetail(
        context,
        system: system,
        application: application,
        environmentName: environmentName,
      ),
      child: Padding(
        padding: const EdgeInsets.fromLTRB(16, 10, 8, 6),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            Row(
              children: <Widget>[
                Tooltip(
                  message: kindLabel(application.kind, application.kindWire),
                  child: Icon(kindIcon(application.kind), size: 20),
                ),
                const SizedBox(width: 10),
                Expanded(
                  child: Text(
                    application.name,
                    style: theme.textTheme.titleSmall,
                    overflow: TextOverflow.ellipsis,
                  ),
                ),
                const SizedBox(width: 8),
                StatusChip(application.status),
                const Icon(Icons.chevron_right, size: 20),
              ],
            ),
            const SizedBox(height: 6),
            Padding(
              padding: const EdgeInsets.only(left: 30),
              child: Wrap(
                spacing: 14,
                runSpacing: 4,
                crossAxisAlignment: WrapCrossAlignment.center,
                children: <Widget>[
                  if (version.isNotEmpty)
                    _Fact(
                      icon: Icons.sell_outlined,
                      child: Text(version, style: small),
                    ),
                  if (deployment?.deployedAt != null)
                    _Fact(
                      icon: Icons.rocket_launch_outlined,
                      child: RelativeTime(
                        deployment!.deployedAt!,
                        prefix: 'deployed ',
                        style: small,
                        now: now,
                      ),
                    ),
                  if (probeText != null)
                    _Fact(
                      icon: Icons.speed,
                      tooltip: probe!.statusCode == null
                          ? 'Health probe'
                          : 'Health probe: HTTP ${probe.statusCode}',
                      child: Text(probeText, style: small),
                    ),
                  _Fact(
                    icon: Icons.inventory_2_outlined,
                    tooltip: 'Container',
                    child: Text(containerText, style: small),
                  ),
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
            ),
          ],
        ),
      ),
    );
  }
}

/// The probe in a few words: `12 ms`, `HTTP 503 · 8 ms`, `connection refused`.
///
/// `error` is set only when no HTTP answer came back; otherwise the status
/// code tells what the answer was, and a 2xx is not worth repeating.
String describeProbe(Probe probe) {
  final code = probe.statusCode;

  return <String>[
    if (code != null && (code < 200 || code >= 300)) 'HTTP $code',
    if (probe.latencyMs != null) formatLatency(probe.latencyMs!),
    if (probe.error != null) probe.error!,
    if (code == null && probe.latencyMs == null && probe.error == null)
      probe.healthy == true ? 'healthy' : 'unhealthy',
  ].join(' · ');
}

class _Fact extends StatelessWidget {
  const _Fact({required this.icon, required this.child, this.tooltip});

  final IconData icon;
  final Widget child;
  final String? tooltip;

  @override
  Widget build(BuildContext context) {
    final content = Row(
      mainAxisSize: MainAxisSize.min,
      children: <Widget>[
        Icon(icon, size: 16, color: Theme.of(context).colorScheme.outline),
        const SizedBox(width: 4),
        Flexible(child: child),
      ],
    );

    return ConstrainedBox(
      // A long probe error must wrap inside the card, not overflow it.
      constraints: const BoxConstraints(maxWidth: 400),
      child: tooltip == null
          ? content
          : Tooltip(message: tooltip, child: content),
    );
  }
}
