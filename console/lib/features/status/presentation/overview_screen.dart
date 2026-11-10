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
import 'map/system_map.dart';
import 'refresh_settings.dart';
import 'status_controller.dart';
import 'widgets/environment_selector.dart';
import 'widgets/refresh_controls.dart';
import 'widgets/status_visuals.dart';
import 'widgets/system_panel.dart';

/// The host switcher, the host's status and every system on it.
///
/// Loads once on start, on request and, when the user turned auto-refresh on,
/// every chosen interval; that timer stops when the app is backgrounded or the
/// browser tab hidden, and while the settings screen covers this one.
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
      // Whatever is on screen is at least as old as the time spent hidden;
      // without auto-refresh the user asks when they want it. A pending
      // "starting" retry was cancelled on the way out, so re-arm it.
      if (_autoRefresh.enabled || _isStarting) {
        unawaited(_refresh());
      }

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

  bool get _isStarting => ref.read(statusControllerProvider).isStarting;

  RefreshSettings get _autoRefresh => ref.read(refreshSettingsProvider);

  void _startPolling() {
    _poll?.cancel();

    if (!_visible || _covered || !_autoRefresh.enabled) {
      return;
    }

    _poll = Timer.periodic(_autoRefresh.interval, (_) => unawaited(_refresh()));
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

    if (_autoRefresh.enabled || _isStarting) {
      unawaited(_refresh());
    }

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
    // A new choice restarts the timer, or stops it.
    ref.listen<RefreshSettings>(
      refreshSettingsProvider,
      (_, _) => _startPolling(),
    );

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
    final autoRefresh = ref.watch(refreshSettingsProvider);
    final rememberedEnvironment = ref.watch(selectedHostEnvironmentProvider);
    final hostStatus = snapshot?.status;
    final hostEnvironment = hostStatus == null
        ? null
        : resolveHostEnvironment(hostStatus, rememberedEnvironment);

    return LayoutBuilder(
      builder: (context, constraints) {
        final width = constraints.maxWidth;
        final gutter = width < Breakpoints.tablet ? 8.0 : 16.0;

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
                      if (environment != null)
                        Padding(
                          padding: const EdgeInsets.only(bottom: 12),
                          child: RefreshControls(
                            updatedAt: hostStatus?.generatedAt,
                            loading: status.loading,
                            settings: autoRefresh,
                            onRefresh: onRetry,
                            onSettingsChanged: (settings) => ref
                                .read(refreshSettingsProvider.notifier)
                                .update(settings),
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
                      if (hostStatus != null)
                        Padding(
                          padding: const EdgeInsets.only(top: 12),
                          child: hostEnvironment == null
                              ? const Text('This host reports no environments.')
                              : _EnvironmentBar(
                                  host: hostStatus,
                                  selected: hostEnvironment,
                                  onSelected: (id) => ref
                                      .read(
                                        selectedHostEnvironmentProvider
                                            .notifier,
                                      )
                                      .select(id),
                                ),
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
            else if (hostEnvironment != null)
              SliverPadding(
                padding: EdgeInsets.fromLTRB(gutter, 16, gutter, gutter + 16),
                sliver: SliverToBoxAdapter(
                  child: _Constrained(
                    child: _Stale(
                      stale: status.isStale,
                      child: SystemMap(
                        host: snapshot.status,
                        environmentId: hostEnvironment.id,
                        onOpenSystem: (id) => _openSystem(
                          context,
                          snapshot.status,
                          hostEnvironment.id,
                          id,
                        ),
                      ),
                    ),
                  ),
                ),
              )
            else if (snapshot.status.systems.isEmpty)
              const SliverPadding(
                padding: EdgeInsets.symmetric(vertical: 32),
                sliver: SliverToBoxAdapter(
                  child: Center(
                    child: Text(
                      'This host has no systems.',
                      textAlign: TextAlign.center,
                    ),
                  ),
                ),
              ),
          ],
        );
      },
    );
  }
}

/// Opens the panel of system [id] as it is in [environmentId] right now.
void _openSystem(
  BuildContext context,
  HostStatus host,
  String environmentId,
  String id,
) {
  for (final system in host.systems) {
    if (system.id != id) {
      continue;
    }

    for (final environment in system.environments) {
      if (environment.environment == environmentId) {
        unawaited(
          showSystemPanel(
            context,
            system: system,
            environment: environment,
            host: host,
          ),
        );

        return;
      }
    }
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

/// Picks the saved host: small, as it changes far less often than the
/// environment selector under it.
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
    final theme = Theme.of(context);
    final list = environments.environments;
    final style = theme.textTheme.bodySmall;
    final longest = list
        .map((environment) => environment.name.length)
        .reduce((a, b) => a > b ? a : b);

    return Align(
      alignment: Alignment.centerLeft,
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: <Widget>[
          Text(
            'Host',
            style: style?.copyWith(color: theme.colorScheme.onSurfaceVariant),
          ),
          const SizedBox(width: 8),
          Flexible(
            // Roughly what the longest name needs: the arrow plus ~7 px per
            // character. Sized so a short list stays small and a long name
            // ellipsizes instead of overflowing a narrow screen.
            child: ConstrainedBox(
              constraints: BoxConstraints(maxWidth: 40 + longest * 7.0),
              child: DropdownButton<String>(
                key: const ValueKey<String>('environment-switcher'),
                // The host on screen can be one just removed from the list.
                value: list.any((environment) => environment.id == selectedId)
                    ? selectedId
                    : null,
                isDense: true,
                isExpanded: true,
                underline: const SizedBox.shrink(),
                style: style?.copyWith(color: theme.colorScheme.onSurface),
                items: <DropdownMenuItem<String>>[
                  for (final environment in list)
                    DropdownMenuItem<String>(
                      value: environment.id,
                      child: Text(
                        environment.name,
                        overflow: TextOverflow.ellipsis,
                      ),
                    ),
                ],
                onChanged: (id) {
                  if (id != null) {
                    controller.select(id);
                  }
                },
              ),
            ),
          ),
        ],
      ),
    );
  }
}

/// The host: its overall status and a summary.
class _StatusBanner extends StatelessWidget {
  const _StatusBanner({required this.status});

  final HostStatus status;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final visual = StatusVisual.of(status.status, theme.brightness);

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

/// The environment selector and, beside it, how many integrations are failing
/// there.
class _EnvironmentBar extends StatelessWidget {
  const _EnvironmentBar({
    required this.host,
    required this.selected,
    required this.onSelected,
  });

  final HostStatus host;
  final HostEnvironment selected;
  final ValueChanged<String> onSelected;

  @override
  Widget build(BuildContext context) {
    final summary = environmentSummaryLine(host, selected.id);

    return Wrap(
      spacing: 16,
      runSpacing: 8,
      crossAxisAlignment: WrapCrossAlignment.center,
      children: <Widget>[
        EnvironmentSelector(
          environments: host.environments,
          selectedId: selected.id,
          onSelected: onSelected,
        ),
        if (summary.isNotEmpty)
          Text(summary, style: Theme.of(context).textTheme.bodyMedium),
      ],
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
