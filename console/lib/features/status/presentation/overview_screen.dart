import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/widgets/time_widgets.dart';
import '../../environments/domain/environment.dart';
import '../../environments/presentation/environment_dialogs.dart';
import '../../environments/presentation/environments_controller.dart';
import '../../environments/presentation/settings_screen.dart';
import '../data/status_source.dart';
import '../domain/status.dart';
import '../domain/status_logic.dart';
import 'layout.dart';
import 'status_controller.dart';
import 'widgets/status_visuals.dart';
import 'widgets/system_card.dart';

/// The host switcher, the host's status and every system on it.
///
/// Polls every `AppConfig.refreshInterval` while the app is visible; the timer
/// stops when the app is backgrounded or the browser tab hidden, and while the
/// settings screen covers this one.
class OverviewScreen extends ConsumerStatefulWidget {
  const OverviewScreen({super.key});

  @override
  ConsumerState<OverviewScreen> createState() => _OverviewScreenState();
}

class _OverviewScreenState extends ConsumerState<OverviewScreen> {
  Timer? _poll;

  /// The one-shot retry after a "starting" answer (503 with Retry-After).
  Timer? _retry;
  late final AppLifecycleListener _lifecycle;
  bool _visible = true;
  bool _covered = false;
  bool _askingForToken = false;

  @override
  void initState() {
    super.initState();
    _lifecycle = AppLifecycleListener(onStateChange: _onLifecycle);
    _startPolling();
  }

  @override
  void dispose() {
    _poll?.cancel();
    _retry?.cancel();
    _lifecycle.dispose();
    super.dispose();
  }

  void _onLifecycle(AppLifecycleState state) {
    final visible =
        state == AppLifecycleState.resumed ||
        state == AppLifecycleState.inactive;

    if (visible == _visible) {
      return;
    }

    _visible = visible;

    if (visible) {
      // Whatever is on screen is at least as old as the time spent hidden.
      unawaited(_refresh());
      _startPolling();
    } else {
      _poll?.cancel();
      _retry?.cancel();
    }
  }

  void _scheduleRetry(Duration after) {
    _retry?.cancel();

    if (!_visible || _covered) {
      return;
    }

    _retry = Timer(after, () => unawaited(_refresh()));
  }

  void _startPolling() {
    _poll?.cancel();

    if (!_visible || _covered) {
      return;
    }

    _poll = Timer.periodic(
      ref.read(appConfigProvider).refreshInterval,
      (_) => unawaited(_refresh()),
    );
  }

  Future<void> _refresh() =>
      ref.read(statusControllerProvider.notifier).refresh();

  Future<void> _openSettings() async {
    _covered = true;
    _poll?.cancel();
    _retry?.cancel();
    await Navigator.of(context)
        .push(MaterialPageRoute<void>(builder: (_) => const SettingsScreen()));

    if (!mounted) {
      return;
    }

    _covered = false;
    unawaited(_refresh());
    _startPolling();
  }

  Future<void> _askForToken(
    Environment environment, {
    bool rejected = false,
  }) async {
    if (_askingForToken) {
      return;
    }

    _askingForToken = true;

    try {
      final token = await showTokenDialog(
        context,
        environment,
        rejected: rejected,
      );

      if (token != null) {
        await ref
            .read(environmentsControllerProvider.notifier)
            .setToken(environment.id, token);
        await _refresh();
      }
    } finally {
      _askingForToken = false;
    }
  }

  @override
  Widget build(BuildContext context) {
    // A 401 takes the user straight to entering that host's token.
    ref.listen<StatusViewState>(statusControllerProvider, (previous, next) {
      final environment = next.environment;

      if (next.needsToken &&
          !(previous?.needsToken ?? false) &&
          environment != null) {
        unawaited(_askForToken(environment, rejected: true));
      }

      // The status API has not finished its first refresh: ask again when
      // it says to, rather than waiting for the next poll.
      final failure = next.failure;

      if (failure != null &&
          failure.isStarting &&
          !identical(previous?.failure, failure)) {
        _scheduleRetry(failure.retryAfter ?? const Duration(seconds: 5));
      }
    });

    final environments = ref.watch(environmentsControllerProvider);
    final status = ref.watch(statusControllerProvider);

    return Scaffold(
      appBar: AppBar(
        title: const Row(
          mainAxisSize: MainAxisSize.min,
          children: <Widget>[
            Icon(Icons.park),
            SizedBox(width: 8),
            Flexible(child: Text('Yggdrasil', overflow: TextOverflow.ellipsis)),
          ],
        ),
        actions: <Widget>[
          if (status.environment != null)
            IconButton(
              key: const ValueKey<String>('refresh'),
              tooltip: 'Refresh',
              onPressed: status.loading ? null : _refresh,
              icon: const Icon(Icons.refresh),
            ),
          IconButton(
            key: const ValueKey<String>('settings'),
            tooltip: 'Hosts',
            onPressed: _openSettings,
            icon: const Icon(Icons.settings),
          ),
        ],
        bottom: PreferredSize(
          preferredSize: const Size.fromHeight(2),
          child: status.loading
              ? const LinearProgressIndicator(minHeight: 2)
              : const SizedBox(height: 2),
        ),
      ),
      body: switch (environments) {
        AsyncData<EnvironmentsState>(:final value)
            when value.environments.isEmpty =>
          _NoEnvironments(onOpenSettings: _openSettings),
        AsyncData<EnvironmentsState>(:final value) => RefreshIndicator(
          onRefresh: _refresh,
          child: _OverviewBody(
            environments: value,
            status: status,
            onRetry: _refresh,
            onEnterToken: _askForToken,
          ),
        ),
        AsyncError<EnvironmentsState>(:final error) => Center(
          child: Padding(
            padding: const EdgeInsets.all(24),
            child: Text('Could not load the hosts: $error'),
          ),
        ),
        _ => const Center(child: CircularProgressIndicator()),
      },
    );
  }
}

class _OverviewBody extends ConsumerWidget {
  const _OverviewBody({
    required this.environments,
    required this.status,
    required this.onRetry,
    required this.onEnterToken,
  });

  final EnvironmentsState environments;
  final StatusViewState status;
  final Future<void> Function() onRetry;
  final Future<void> Function(Environment environment) onEnterToken;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final environment = status.environment ?? environments.selected;
    final snapshot = status.snapshot;
    final failure = status.failure;
    final problemsOnly = ref.watch(problemsOnlyProvider);
    final expanded = ref.watch(expandedSystemsProvider);

    return LayoutBuilder(
      builder: (context, constraints) {
        final width = constraints.maxWidth;
        final gutter = width < Breakpoints.tablet ? 8.0 : 16.0;
        final columns = systemColumnsFor(width);

        return CustomScrollView(
          physics: const AlwaysScrollableScrollPhysics(),
          slivers: <Widget>[
            SliverPadding(
              padding: EdgeInsets.fromLTRB(gutter, gutter, gutter, 0),
              sliver: SliverToBoxAdapter(
                child: _Constrained(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: <Widget>[
                      if (environments.environments.length > 1)
                        Padding(
                          padding: const EdgeInsets.only(bottom: 12),
                          child: _EnvironmentSwitcher(
                            environments: environments,
                            selectedId: environment?.id,
                          ),
                        ),
                      if (failure != null && failure.isStarting)
                        Padding(
                          padding: const EdgeInsets.only(bottom: 12),
                          child: _StartingBanner(failure: failure),
                        )
                      else if (failure != null && environment != null)
                        Padding(
                          padding: const EdgeInsets.only(bottom: 12),
                          child: _FailureBanner(
                            failure: failure,
                            snapshot: snapshot?.status,
                            onRetry: onRetry,
                            onEnterToken: () => onEnterToken(environment),
                          ),
                        ),
                      if (snapshot != null)
                        _Stale(
                          stale: status.isStale,
                          child: _StatusBanner(status: snapshot.status),
                        ),
                    ],
                  ),
                ),
              ),
            ),
            if (snapshot == null)
              SliverFillRemaining(
                hasScrollBody: false,
                child: Center(
                  child: failure == null || failure.isStarting
                      ? const CircularProgressIndicator()
                      : const SizedBox.shrink(),
                ),
              )
            else ...<Widget>[
              SliverPadding(
                padding: EdgeInsets.fromLTRB(gutter, 12, gutter, 8),
                sliver: SliverToBoxAdapter(
                  child: _Constrained(
                    child: Align(
                      alignment: Alignment.centerLeft,
                      child: FilterChip(
                        key: const ValueKey<String>('problems-only'),
                        label: Text(
                          'Problems only '
                          '(${snapshot.status.systems.where(hasProblem).length})',
                        ),
                        selected: problemsOnly,
                        onSelected: (value) =>
                            ref.read(problemsOnlyProvider.notifier).state =
                                value,
                      ),
                    ),
                  ),
                ),
              ),
              SliverPadding(
                padding: EdgeInsets.fromLTRB(gutter, 0, gutter, gutter + 16),
                sliver: SliverToBoxAdapter(
                  child: _Constrained(
                    child: _Stale(
                      stale: status.isStale,
                      child: _SystemGrid(
                        host: snapshot.status,
                        systems: visibleSystems(
                          snapshot.status.systems,
                          problemsOnly: problemsOnly,
                        ),
                        columns: columns,
                        spacing: gutter < 16 ? 8 : 12,
                        expanded: expanded,
                        onToggle: (id) {
                          final notifier = ref.read(
                            expandedSystemsProvider.notifier,
                          );
                          notifier.state = notifier.state.contains(id)
                              ? (Set<String>.of(notifier.state)..remove(id))
                              : <String>{...notifier.state, id};
                        },
                        emptyText: problemsOnly
                            ? 'No problems: every system is up.'
                            : 'This host has no systems.',
                      ),
                    ),
                  ),
                ),
              ),
            ],
          ],
        );
      },
    );
  }
}

/// Centres content and caps its width on very wide screens.
class _Constrained extends StatelessWidget {
  const _Constrained({required this.child});

  final Widget child;

  @override
  Widget build(BuildContext context) => Center(
    child: ConstrainedBox(
      constraints: const BoxConstraints(maxWidth: Breakpoints.maxContentWidth),
      child: child,
    ),
  );
}

/// Greys out data that the last refresh could not confirm.
class _Stale extends StatelessWidget {
  const _Stale({required this.stale, required this.child});

  final bool stale;
  final Widget child;

  static const List<double> _greyscale = <double>[
    0.2126, 0.7152, 0.0722, 0, 0, //
    0.2126, 0.7152, 0.0722, 0, 0, //
    0.2126, 0.7152, 0.0722, 0, 0, //
    0, 0, 0, 1, 0, //
  ];

  @override
  Widget build(BuildContext context) => stale
      ? Opacity(
          opacity: 0.6,
          child: ColorFiltered(
            colorFilter: const ColorFilter.matrix(_greyscale),
            child: child,
          ),
        )
      : child;
}

class _EnvironmentSwitcher extends ConsumerWidget {
  const _EnvironmentSwitcher({
    required this.environments,
    required this.selectedId,
  });

  final EnvironmentsState environments;
  final String? selectedId;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final controller = ref.read(environmentsControllerProvider.notifier);
    final list = environments.environments;

    return LayoutBuilder(
      builder: (context, constraints) {
        final longest = list
            .map((environment) => environment.name.length)
            .reduce((a, b) => a > b ? a : b);
        // Roughly what a segment needs: padding plus ~9 px per character.
        final segmentWidth = 40 + longest * 9.0;
        final fits =
            list.length <= 4 &&
            segmentWidth * list.length <= constraints.maxWidth;

        if (fits) {
          return Align(
            alignment: Alignment.centerLeft,
            child: SegmentedButton<String>(
              key: const ValueKey<String>('environment-switcher'),
              showSelectedIcon: false,
              segments: <ButtonSegment<String>>[
                for (final environment in list)
                  ButtonSegment<String>(
                    value: environment.id,
                    label: Text(environment.name),
                  ),
              ],
              selected: <String>{?selectedId},
              onSelectionChanged: (selection) =>
                  controller.select(selection.first),
            ),
          );
        }

        return DropdownButtonFormField<String>(
          key: const ValueKey<String>('environment-switcher'),
          initialValue: selectedId,
          isExpanded: true,
          decoration: const InputDecoration(labelText: 'Host', isDense: true),
          items: <DropdownMenuItem<String>>[
            for (final environment in list)
              DropdownMenuItem<String>(
                value: environment.id,
                child: Text(environment.name, overflow: TextOverflow.ellipsis),
              ),
          ],
          onChanged: (id) {
            if (id != null) {
              controller.select(id);
            }
          },
        );
      },
    );
  }
}

/// The host: its overall status, a summary, and each environment's status.
class _StatusBanner extends StatelessWidget {
  const _StatusBanner({required this.status});

  final HostStatus status;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final visual = StatusVisual.of(status.status, theme.brightness);
    final generatedAt = status.generatedAt;

    return Semantics(
      container: true,
      label: '${status.host} is ${visual.label}',
      child: DecoratedBox(
        decoration: BoxDecoration(
          color: visual.container,
          borderRadius: BorderRadius.circular(12),
        ),
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Row(
            children: <Widget>[
              Icon(visual.icon, color: visual.foreground, size: 40),
              const SizedBox(width: 16),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: <Widget>[
                    Text(
                      status.host,
                      key: const ValueKey<String>('host-name'),
                      style: theme.textTheme.labelLarge?.copyWith(
                        color: visual.foreground,
                      ),
                      overflow: TextOverflow.ellipsis,
                    ),
                    Text(
                      visual.label,
                      style: theme.textTheme.headlineSmall?.copyWith(
                        color: visual.foreground,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                    const SizedBox(height: 4),
                    Text(
                      hostSummary(status),
                      style: theme.textTheme.bodyMedium?.copyWith(
                        color: visual.foreground,
                      ),
                    ),
                    if (status.environments.isNotEmpty) ...[
                      const SizedBox(height: 8),
                      Wrap(
                        key: const ValueKey<String>('host-environments'),
                        spacing: 6,
                        runSpacing: 6,
                        children: <Widget>[
                          for (final environment in status.environments)
                            EnvironmentStatusChip(
                              name: environment.name,
                              status: environment.status,
                              onDemand: environment.onDemand,
                            ),
                        ],
                      ),
                      const SizedBox(height: 4),
                    ],
                    if (generatedAt != null)
                      NowBuilder(
                        builder: (context, now) => RelativeTime(
                          generatedAt,
                          prefix: 'updated ',
                          now: now,
                          style: theme.textTheme.bodySmall?.copyWith(
                            color: visual.foreground,
                          ),
                        ),
                      ),
                  ],
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

/// Not an error: the status API is up but has no results yet.
class _StartingBanner extends StatelessWidget {
  const _StartingBanner({required this.failure});

  final StatusException failure;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final scheme = theme.colorScheme;
    final seconds =
        (failure.retryAfter ?? const Duration(seconds: 5)).inSeconds;

    return Card(
      key: const ValueKey<String>('starting-banner'),
      color: scheme.secondaryContainer,
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Row(
          children: <Widget>[
            Icon(Icons.hourglass_top, color: scheme.onSecondaryContainer),
            const SizedBox(width: 12),
            Expanded(
              child: Text(
                'The status API is starting and has no results yet. '
                'Trying again in $seconds s.',
                style: theme.textTheme.bodyLarge?.copyWith(
                  color: scheme.onSecondaryContainer,
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _FailureBanner extends StatelessWidget {
  const _FailureBanner({
    required this.failure,
    required this.snapshot,
    required this.onRetry,
    required this.onEnterToken,
  });

  final StatusException failure;
  final HostStatus? snapshot;
  final Future<void> Function() onRetry;
  final VoidCallback onEnterToken;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final scheme = theme.colorScheme;
    final generatedAt = snapshot?.generatedAt;
    final unauthorized = failure.kind == StatusFailureKind.unauthorized;

    return Card(
      key: const ValueKey<String>('failure-banner'),
      color: scheme.errorContainer,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(16, 12, 8, 4),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: <Widget>[
                Icon(
                  unauthorized
                      ? Icons.lock_outline
                      : (failure.kind == StatusFailureKind.network
                            ? Icons.cloud_off
                            : Icons.error_outline),
                  color: scheme.onErrorContainer,
                ),
                const SizedBox(width: 12),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: <Widget>[
                      Text(
                        failure.message,
                        style: theme.textTheme.bodyLarge?.copyWith(
                          color: scheme.onErrorContainer,
                        ),
                      ),
                      if (snapshot != null)
                        NowBuilder(
                          builder: (context, now) => Text(
                            generatedAt == null
                                ? 'Showing the last data received.'
                                : 'Showing the last data received, generated '
                                      '${_ago(generatedAt, now)}.',
                            style: theme.textTheme.bodyMedium?.copyWith(
                              color: scheme.onErrorContainer,
                            ),
                          ),
                        ),
                    ],
                  ),
                ),
              ],
            ),
            Align(
              alignment: Alignment.centerRight,
              child: Wrap(
                spacing: 4,
                children: <Widget>[
                  if (unauthorized)
                    TextButton(
                      onPressed: onEnterToken,
                      child: const Text('Enter token'),
                    ),
                  TextButton(onPressed: onRetry, child: const Text('Retry')),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }

  static String _ago(DateTime moment, DateTime now) {
    final seconds = now.difference(moment).inSeconds;

    if (seconds < 60) {
      return '${seconds < 0 ? 0 : seconds} s ago';
    }

    if (seconds < 3600) {
      return '${seconds ~/ 60} min ago';
    }

    return '${seconds ~/ 3600} h ago';
  }
}

/// Cards laid out in [columns] columns, filled row by row so the worst
/// systems stay at the top whatever the width.
class _SystemGrid extends StatelessWidget {
  const _SystemGrid({
    required this.host,
    required this.systems,
    required this.columns,
    required this.spacing,
    required this.expanded,
    required this.onToggle,
    required this.emptyText,
  });

  final HostStatus host;
  final List<SystemStatus> systems;
  final int columns;
  final double spacing;
  final Set<String> expanded;
  final void Function(String id) onToggle;
  final String emptyText;

  @override
  Widget build(BuildContext context) {
    if (systems.isEmpty) {
      return Padding(
        padding: const EdgeInsets.symmetric(vertical: 32),
        child: Center(child: Text(emptyText, textAlign: TextAlign.center)),
      );
    }

    return NowBuilder(
      interval: const Duration(seconds: 15),
      builder: (context, now) {
        Widget card(SystemStatus system) => SystemCard(
          key: ValueKey<String>('card-${system.id}'),
          host: host,
          system: system,
          expanded: expanded.contains(system.id),
          onToggle: () => onToggle(system.id),
          now: now,
        );

        if (columns == 1) {
          return Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: <Widget>[
              for (final (index, system) in systems.indexed) ...[
                if (index > 0) SizedBox(height: spacing),
                card(system),
              ],
            ],
          );
        }

        // Row-major: system i goes to column i % columns. Each column is its
        // own stack, so expanding a card does not stretch its neighbours.
        return Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            for (var column = 0; column < columns; column++) ...[
              if (column > 0) SizedBox(width: spacing),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: <Widget>[
                    for (final (index, system) in systems.indexed)
                      if (index % columns == column) ...[
                        if (index >= columns) SizedBox(height: spacing),
                        card(system),
                      ],
                  ],
                ),
              ),
            ],
          ],
        );
      },
    );
  }
}

class _NoEnvironments extends ConsumerWidget {
  const _NoEnvironments({required this.onOpenSettings});

  final VoidCallback onOpenSettings;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final theme = Theme.of(context);

    return Center(
      child: SingleChildScrollView(
        padding: const EdgeInsets.all(24),
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 420),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: <Widget>[
              Icon(
                Icons.park_outlined,
                size: 64,
                color: theme.colorScheme.primary,
              ),
              const SizedBox(height: 16),
              Text('No hosts yet', style: theme.textTheme.titleLarge),
              const SizedBox(height: 8),
              const Text(
                'Add the base URL of a yggdrasil host and its status token, '
                'or look around with the offline demo.',
                textAlign: TextAlign.center,
              ),
              const SizedBox(height: 24),
              Wrap(
                spacing: 12,
                runSpacing: 12,
                alignment: WrapAlignment.center,
                children: <Widget>[
                  FilledButton.icon(
                    onPressed: () =>
                        SettingsScreen.addEnvironment(context, ref),
                    icon: const Icon(Icons.add),
                    label: const Text('Add host'),
                  ),
                  OutlinedButton.icon(
                    key: const ValueKey<String>('open-demo'),
                    onPressed: () => ref
                        .read(environmentsControllerProvider.notifier)
                        .addDemo(),
                    icon: const Icon(Icons.science_outlined),
                    label: const Text('Open the demo'),
                  ),
                ],
              ),
              const SizedBox(height: 8),
              TextButton(
                onPressed: onOpenSettings,
                child: const Text('Manage hosts'),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
