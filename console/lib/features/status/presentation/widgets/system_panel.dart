import 'package:material_ui/material_ui.dart';

import '../../../../core/widgets/links.dart';
import '../../../../core/widgets/time_widgets.dart';
import '../../domain/status.dart';
import '../../domain/status_logic.dart';
import '../layout.dart';
import '../map/system_icons.dart';
import '../map/system_map.dart';
import 'application_detail.dart';
import 'status_visuals.dart';

/// Shows [system] in [environment]: a side panel from [Breakpoints.tablet] up,
/// so the map stays visible, and a bottom sheet below it.
///
/// It shows the state as it was when opened; it does not follow a refresh.
Future<void> showSystemPanel(
  BuildContext context, {
  required SystemStatus system,
  required SystemEnvironment environment,
  required HostStatus host,
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
        builder: (context, controller) => SystemPanel(
          system: system,
          environment: environment,
          host: host,
          scrollController: controller,
        ),
      ),
    );
  }

  return showGeneralDialog<void>(
    context: context,
    barrierDismissible: true,
    barrierLabel: 'Close system panel',
    // No dimming: the map is the context the panel is read against.
    barrierColor: Colors.transparent,
    transitionDuration: const Duration(milliseconds: 200),
    pageBuilder: (context, _, _) => Align(
      alignment: Alignment.centerRight,
      child: Material(
        elevation: 8,
        child: SizedBox(
          width: _sidePanelWidth,
          height: double.infinity,
          child: SafeArea(
            child: SystemPanel(
              system: system,
              environment: environment,
              host: host,
              onClose: () => Navigator.of(context).pop(),
            ),
          ),
        ),
      ),
    ),
    transitionBuilder: (context, animation, _, child) => SlideTransition(
      position: Tween<Offset>(
        begin: const Offset(1, 0),
        end: Offset.zero,
      ).animate(CurvedAnimation(parent: animation, curve: Curves.easeOut)),
      child: child,
    ),
  );
}

const double _sidePanelWidth = 420;

class SystemPanel extends StatelessWidget {
  const SystemPanel({
    super.key,
    required this.system,
    required this.environment,
    required this.host,
    this.scrollController,
    this.onClose,
  });

  final SystemStatus system;
  final SystemEnvironment environment;

  /// The response the system comes from, for the environment's name and the
  /// names of the systems it integrates with.
  final HostStatus host;
  final ScrollController? scrollController;
  final VoidCallback? onClose;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final scheme = theme.colorScheme;
    final muted = theme.textTheme.bodySmall?.copyWith(
      color: scheme.onSurfaceVariant,
    );
    final environmentName = host.environmentName(environment.environment);
    final applications = sortApplications(environment.applications);

    return ListView(
      key: const ValueKey<String>('system-panel'),
      controller: scrollController,
      padding: const EdgeInsets.fromLTRB(20, 8, 20, 24),
      children: <Widget>[
        Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            CircleAvatar(
              backgroundColor: scheme.secondaryContainer,
              foregroundColor: scheme.onSecondaryContainer,
              child: Icon(systemIcon(system.icon)),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: <Widget>[
                  Semantics(
                    header: true,
                    child: Text(system.name, style: theme.textTheme.titleLarge),
                  ),
                  Text(environmentName, style: muted),
                ],
              ),
            ),
            if (onClose != null)
              IconButton(
                tooltip: 'Close',
                onPressed: onClose,
                icon: const Icon(Icons.close),
              ),
          ],
        ),
        if (system.description != null) ...[
          const SizedBox(height: 8),
          Text(system.description!, style: theme.textTheme.bodyMedium),
        ],
        const SizedBox(height: 8),
        Wrap(
          spacing: 10,
          runSpacing: 6,
          crossAxisAlignment: WrapCrossAlignment.center,
          children: <Widget>[
            StatusChip(environment.status),
            if (environment.onDemand)
              Tooltip(
                message:
                    'Started only when used; stopped is its normal state '
                    'when idle.',
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
          ],
        ),
        const _Heading('Applications'),
        if (applications.isEmpty)
          Text('No applications in $environmentName.', style: muted),
        for (final (index, application) in applications.indexed) ...[
          if (index > 0) const Divider(height: 1),
          ApplicationRow(
            system: system,
            environmentId: environment.environment,
            environmentName: environmentName,
            application: application,
          ),
        ],
        // One section per application that has a detailed health endpoint.
        for (final application in applications)
          if (application.details != null)
            _ReportSection(
              application: application,
              integrations: <IntegrationStatus>[
                for (final integration in environment.integrations)
                  if (integration.from == application.id) integration,
              ],
              host: host,
              environmentId: environment.environment,
            ),
      ],
    );
  }
}

class _Heading extends StatelessWidget {
  const _Heading(this.text);

  final String text;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);

    return Padding(
      padding: const EdgeInsets.only(top: 20, bottom: 6),
      child: Semantics(
        header: true,
        child: Text(
          text,
          style: theme.textTheme.titleSmall?.copyWith(
            color: theme.colorScheme.primary,
          ),
        ),
      ),
    );
  }
}

/// What one application's detailed health said.
class _ReportSection extends StatelessWidget {
  const _ReportSection({
    required this.application,
    required this.integrations,
    required this.host,
    required this.environmentId,
  });

  final ApplicationStatus application;

  /// The integrations the catalog declares for this application.
  final List<IntegrationStatus> integrations;
  final HostStatus host;
  final String environmentId;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final brightness = theme.brightness;
    final details = application.details!;
    final title = 'Reported by ${application.name}';
    final checkedAt = details.checkedAt;
    final headingStyle = theme.textTheme.titleSmall?.copyWith(
      color: theme.colorScheme.primary,
    );

    return Column(
      key: ValueKey<String>('report-$environmentId-${application.id}'),
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: <Widget>[
        Padding(
          padding: const EdgeInsets.only(top: 20, bottom: 6),
          child: Semantics(
            header: true,
            child: checkedAt == null
                ? Text(title, style: headingStyle)
                : RelativeTime(
                    checkedAt,
                    prefix: '$title · checked ',
                    style: headingStyle,
                  ),
          ),
        ),
        switch (details.state) {
          DetailsState.reported => _Services(
            details: details,
            integrations: integrations,
            host: host,
          ),
          DetailsState.unavailable => Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: <Widget>[
              _Note(
                'Could not get more details. The detailed health endpoint '
                'did not answer: ${details.reason ?? 'no reason given'}.',
              ),
              for (final integration in integrations)
                _Row(
                  mark: _Mark.notChecked(brightness),
                  name: integration.check,
                  target: _targetOf(integration, host),
                  isIntegration: true,
                ),
            ],
          ),
          DetailsState.unsupported => const _Note(
            'This version does not report detailed health',
          ),
          DetailsState.notChecked => const _Note('Not checked yet'),
        },
      ],
    );
  }
}

class _Services extends StatelessWidget {
  const _Services({
    required this.details,
    required this.integrations,
    required this.host,
  });

  final ApplicationDetails details;
  final List<IntegrationStatus> integrations;
  final HostStatus host;

  @override
  Widget build(BuildContext context) {
    final brightness = Theme.of(context).brightness;

    if (details.services.isEmpty) {
      return const _Note('The report lists nothing.');
    }

    return Column(
      children: <Widget>[
        for (final service in details.services)
          _serviceRow(service, brightness),
      ],
    );
  }

  Widget _serviceRow(ReportedService service, Brightness brightness) {
    final integration = _integrationNamed(integrations, service.name);

    if (integration == null) {
      return _Row(
        mark: _Mark.reported(service.status, brightness),
        name: service.name,
        reason: service.reason,
      );
    }

    // An integration's status is the catalog's reading, so a dependency left
    // stopped on purpose reads "Stopped" and not the "failing" the report
    // itself says.
    return _Row(
      mark: _Mark.integration(integration.status, brightness),
      name: service.name,
      reason: integration.status == IntegrationHealth.down
          ? integration.reason
          : null,
      target: _targetOf(integration, host),
      isIntegration: true,
    );
  }
}

IntegrationStatus? _integrationNamed(
  List<IntegrationStatus> integrations,
  String check,
) {
  for (final integration in integrations) {
    if (integration.check == check) {
      return integration;
    }
  }

  return null;
}

/// Who an integration points at: a system of the host by its name, or an
/// external service as written.
({String name, IconData icon}) _targetOf(
  IntegrationStatus integration,
  HostStatus host,
) {
  final external = integration.external;

  if (external != null) {
    return (name: external, icon: Icons.cloud);
  }

  for (final system in host.systems) {
    if (system.id == integration.system) {
      return (name: system.name, icon: systemIcon(system.icon));
    }
  }

  return (name: integration.system ?? integration.check, icon: Icons.hub);
}

/// A status word with its own glyph and tint.
class _Mark {
  const _Mark(this.label, this.icon, this.foreground, this.container);

  factory _Mark.integration(IntegrationHealth health, Brightness brightness) {
    final visual = IntegrationVisual.of(health, brightness);

    return _Mark(
      visual.label,
      visual.icon,
      visual.foreground,
      visual.container,
    );
  }

  factory _Mark.notChecked(Brightness brightness) {
    final visual = IntegrationVisual.of(IntegrationHealth.unknown, brightness);

    return _Mark(
      'Not checked',
      visual.icon,
      visual.foreground,
      visual.container,
    );
  }

  /// The report's own word, with the look of the nearest status.
  factory _Mark.reported(String word, Brightness brightness) {
    final status = switch (word) {
      'Healthy' => Status.up,
      'Degraded' => Status.degraded,
      'Unhealthy' => Status.down,
      'NotConfigured' => Status.notDeployed,
      _ => Status.unknown,
    };
    final visual = StatusVisual.of(status, brightness);

    return _Mark(
      switch (word) {
        'NotConfigured' => 'Not configured',
        '' => 'Unknown',
        _ => word,
      },
      visual.icon,
      visual.foreground,
      visual.container,
    );
  }

  final String label;
  final IconData icon;
  final Color foreground;
  final Color container;
}

class _Row extends StatelessWidget {
  const _Row({
    required this.mark,
    required this.name,
    this.reason,
    this.target,
    this.isIntegration = false,
  });

  final _Mark mark;
  final String name;
  final String? reason;
  final ({String name, IconData icon})? target;
  final bool isIntegration;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final scheme = theme.colorScheme;
    final muted = theme.textTheme.bodySmall?.copyWith(
      color: scheme.onSurfaceVariant,
    );

    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 6),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          DecoratedBox(
            decoration: BoxDecoration(
              color: mark.container,
              borderRadius: BorderRadius.circular(8),
            ),
            child: Padding(
              padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
              child: Row(
                mainAxisSize: MainAxisSize.min,
                children: <Widget>[
                  Icon(mark.icon, size: 16, color: mark.foreground),
                  const SizedBox(width: 4),
                  Text(
                    mark.label,
                    style: theme.textTheme.labelMedium?.copyWith(
                      color: mark.foreground,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                ],
              ),
            ),
          ),
          const SizedBox(width: 10),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: <Widget>[
                Wrap(
                  spacing: 8,
                  crossAxisAlignment: WrapCrossAlignment.center,
                  children: <Widget>[
                    Text(name, style: theme.textTheme.bodyMedium),
                    if (isIntegration)
                      DecoratedBox(
                        decoration: BoxDecoration(
                          border: Border.all(color: scheme.outlineVariant),
                          borderRadius: BorderRadius.circular(4),
                        ),
                        child: Padding(
                          padding: const EdgeInsets.symmetric(
                            horizontal: 4,
                            vertical: 1,
                          ),
                          child: Text('integration', style: muted),
                        ),
                      ),
                  ],
                ),
                if (target != null)
                  Padding(
                    padding: const EdgeInsets.only(top: 2),
                    child: Row(
                      mainAxisSize: MainAxisSize.min,
                      children: <Widget>[
                        Icon(target!.icon, size: 14, color: scheme.outline),
                        const SizedBox(width: 4),
                        Flexible(child: Text(target!.name, style: muted)),
                      ],
                    ),
                  ),
                if (reason != null) Text(reason!, style: muted),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _Note extends StatelessWidget {
  const _Note(this.text);

  final String text;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;

    return DecoratedBox(
      decoration: BoxDecoration(
        color: scheme.surfaceContainerHighest,
        borderRadius: BorderRadius.circular(8),
      ),
      child: Padding(padding: const EdgeInsets.all(12), child: Text(text)),
    );
  }
}

/// One application inside the system panel.
class ApplicationRow extends StatelessWidget {
  const ApplicationRow({
    super.key,
    required this.system,
    required this.application,
    required this.environmentId,
    this.environmentName,
  });

  final SystemStatus system;
  final ApplicationStatus application;

  /// The environment the application is in, for its key and detail view.
  final String environmentId;
  final String? environmentName;

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
      key: ValueKey<String>('application-$environmentId-${application.id}'),
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
      // A long probe error must wrap inside the panel, not overflow it.
      constraints: const BoxConstraints(maxWidth: 400),
      child: tooltip == null
          ? content
          : Tooltip(message: tooltip, child: content),
    );
  }
}
