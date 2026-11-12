import 'package:material_ui/material_ui.dart';

import '../../../../core/widgets/time_widgets.dart';
import '../../domain/refresh_settings.dart';

/// "Updated 3 min ago", Refresh now, and the auto-refresh switch with its
/// interval. The time stays in view because, with no polling, the screen can
/// be old without any failure to show.
class RefreshControls extends StatelessWidget {
  const RefreshControls({
    super.key,
    required this.updatedAt,
    required this.loading,
    required this.settings,
    required this.onRefresh,
    required this.onSettingsChanged,
  });

  final DateTime? updatedAt;

  /// A load is in flight, so Refresh now waits.
  final bool loading;
  final RefreshSettings settings;
  final VoidCallback onRefresh;
  final ValueChanged<RefreshSettings> onSettingsChanged;

  /// `Every 1 minute`, `Every 5 minutes`.
  static String intervalLabel(Duration interval) {
    final minutes = interval.inMinutes;

    return 'Every $minutes ${minutes == 1 ? 'minute' : 'minutes'}';
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final moment = updatedAt;

    return Wrap(
      spacing: 12,
      runSpacing: 4,
      crossAxisAlignment: WrapCrossAlignment.center,
      children: <Widget>[
        if (moment != null)
          NowBuilder(
            builder: (context, now) => RelativeTime(
              moment,
              prefix: 'Updated ',
              now: now,
              style: theme.textTheme.bodySmall,
            ),
          ),
        TextButton.icon(
          key: const ValueKey<String>('refresh'),
          onPressed: loading ? null : onRefresh,
          icon: const Icon(Icons.refresh),
          label: const Text('Refresh now'),
        ),
        Wrap(
          spacing: 4,
          crossAxisAlignment: WrapCrossAlignment.center,
          children: <Widget>[
            Text('Auto-refresh', style: theme.textTheme.bodyMedium),
            Switch(
              key: const ValueKey<String>('auto-refresh'),
              value: settings.enabled,
              onChanged: (enabled) =>
                  onSettingsChanged(settings.copyWith(enabled: enabled)),
            ),
            PopupMenuButton<Duration>(
              key: const ValueKey<String>('auto-refresh-interval'),
              tooltip: 'Auto-refresh interval',
              initialValue: settings.interval,
              onSelected: (interval) =>
                  onSettingsChanged(settings.copyWith(interval: interval)),
              itemBuilder: (context) => <PopupMenuEntry<Duration>>[
                for (final interval in autoRefreshIntervals)
                  PopupMenuItem<Duration>(
                    value: interval,
                    child: Text(intervalLabel(interval)),
                  ),
              ],
              child: Padding(
                padding: const EdgeInsets.symmetric(vertical: 8),
                child: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: <Widget>[
                    Text(
                      intervalLabel(settings.interval),
                      style: theme.textTheme.bodyMedium,
                    ),
                    const Icon(Icons.arrow_drop_down),
                  ],
                ),
              ),
            ),
          ],
        ),
      ],
    );
  }
}
