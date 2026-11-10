/// The intervals auto-refresh offers; the first is the default.
const List<Duration> autoRefreshIntervals = <Duration>[
  Duration(minutes: 1),
  Duration(minutes: 5),
  Duration(minutes: 10),
  Duration(minutes: 15),
  Duration(minutes: 30),
];

/// Whether the overview repeats its load by itself, and how often.
class RefreshSettings {
  const RefreshSettings({
    this.enabled = false,
    this.interval = const Duration(minutes: 1),
  });

  final bool enabled;
  final Duration interval;

  RefreshSettings copyWith({bool? enabled, Duration? interval}) =>
      RefreshSettings(
        enabled: enabled ?? this.enabled,
        interval: interval ?? this.interval,
      );

  @override
  bool operator ==(Object other) =>
      other is RefreshSettings &&
      other.enabled == enabled &&
      other.interval == interval;

  @override
  int get hashCode => Object.hash(enabled, interval);
}
