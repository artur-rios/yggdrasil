import 'package:flutter_test/flutter_test.dart';
import 'package:yggdrasil_console/features/status/domain/refresh_settings.dart';

void main() {
  group('RefreshSettings', () {
    test('given no values, when constructed, then auto-refresh is off every '
        'minute, the first interval offered', () {
      const settings = RefreshSettings();

      expect(settings.enabled, isFalse);
      expect(settings.interval, const Duration(minutes: 1));
      expect(settings.interval, autoRefreshIntervals.first);
    });

    test('given the intervals offered, when listed, then they are 1, 5, 10, '
        '15 and 30 minutes', () {
      expect(autoRefreshIntervals, const <Duration>[
        Duration(minutes: 1),
        Duration(minutes: 5),
        Duration(minutes: 10),
        Duration(minutes: 15),
        Duration(minutes: 30),
      ]);
    });

    test('given copyWith, when one field is given, then only it changes', () {
      const settings = RefreshSettings();

      expect(
        settings.copyWith(enabled: true),
        const RefreshSettings(enabled: true),
      );
      expect(
        settings.copyWith(interval: const Duration(minutes: 5)),
        const RefreshSettings(interval: Duration(minutes: 5)),
      );
      expect(settings.copyWith(), settings);
    });

    test('given settings with the same values, when compared, then they are '
        'equal with the same hash, and differ otherwise', () {
      const a = RefreshSettings(enabled: true, interval: Duration(minutes: 5));
      // Built at run time, so not the same const instance.
      final b = const RefreshSettings().copyWith(
        enabled: true,
        interval: const Duration(minutes: 5),
      );

      expect(a, b);
      expect(a.hashCode, b.hashCode);
      expect(a, isNot(const RefreshSettings(enabled: true)));
      expect(a, isNot(const RefreshSettings(interval: Duration(minutes: 5))));
    });
  });
}
