import 'package:flutter_test/flutter_test.dart';
import 'package:yggdrasil_console/core/format/time_format.dart';

void main() {
  group('formatAgo', () {
    final now = DateTime.utc(2026, 9, 18, 18, 0, 0);

    String ago(Duration age) => formatAgo(now.subtract(age), now);

    test('given ages of seconds, minutes, hours and days, when formatted, '
        'then each reads in its own unit', () {
      expect(ago(const Duration(seconds: 12)), '12 s ago');
      expect(ago(const Duration(minutes: 3)), '3 min ago');
      expect(ago(const Duration(hours: 5)), '5 h ago');
      expect(ago(const Duration(days: 3)), '3 d ago');
    });

    test('given a moment in the future, when formatted, then it reads as just '
        'now', () {
      expect(formatAgo(now.add(const Duration(seconds: 5)), now), 'just now');
    });

    test('given an age under a second, when formatted, then it reads as just '
        'now', () {
      expect(ago(Duration.zero), 'just now');
      expect(ago(const Duration(milliseconds: 999)), 'just now');
    });

    test('given ages on each unit boundary, when formatted, then the larger '
        'unit starts there', () {
      expect(ago(const Duration(seconds: 59)), '59 s ago');
      expect(ago(const Duration(seconds: 60)), '1 min ago');
      expect(ago(const Duration(minutes: 59)), '59 min ago');
      expect(ago(const Duration(minutes: 60)), '1 h ago');
      // Hours run up to two days, so "1 d" never shows.
      expect(ago(const Duration(hours: 47)), '47 h ago');
      expect(ago(const Duration(hours: 48)), '2 d ago');
    });
  });

  group('formatAbsolute', () {
    test('given a UTC moment, when formatted, then it shows local time with '
        'its offset', () {
      final text = formatAbsolute(DateTime.utc(2026, 9, 18, 18, 4, 11));

      expect(
        text,
        matches(RegExp(r'^2026-09-1\d \d\d:\d\d:11 \(UTC[+-]\d\d:\d\d\)$')),
      );
    });

    test('given single-digit fields, when formatted, then each is padded to '
        'two digits', () {
      final moment = DateTime(2026, 1, 2, 3, 4, 5);

      expect(formatAbsolute(moment), startsWith('2026-01-02 03:04:05 (UTC'));
    });
  });

  group('formatLatency', () {
    test('given a latency under a second, when formatted, then it reads in '
        'milliseconds', () {
      expect(formatLatency(0), '0 ms');
      expect(formatLatency(12), '12 ms');
      expect(formatLatency(999), '999 ms');
    });

    test('given a latency of a second or more, when formatted, then it reads '
        'in seconds with one decimal', () {
      expect(formatLatency(1000), '1.0 s');
      expect(formatLatency(2380), '2.4 s');
    });
  });
}
