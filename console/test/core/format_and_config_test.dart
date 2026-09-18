import 'package:flutter_test/flutter_test.dart';
import 'package:yggdrasil_console/core/config/app_config.dart';
import 'package:yggdrasil_console/core/format/time_format.dart';
import 'package:yggdrasil_console/features/environments/domain/environment.dart';

void main() {
  group('formatAgo', () {
    final now = DateTime.utc(2026, 9, 18, 18, 0, 0);

    test('formats seconds, minutes, hours and days', () {
      expect(
        formatAgo(now.subtract(const Duration(seconds: 12)), now),
        '12 s ago',
      );
      expect(
        formatAgo(now.subtract(const Duration(minutes: 3)), now),
        '3 min ago',
      );
      expect(formatAgo(now.subtract(const Duration(hours: 5)), now), '5 h ago');
      expect(formatAgo(now.subtract(const Duration(days: 3)), now), '3 d ago');
    });

    test('a future moment reads as just now', () {
      expect(formatAgo(now.add(const Duration(seconds: 5)), now), 'just now');
    });
  });

  test('formatAbsolute shows local time with its offset', () {
    final text = formatAbsolute(DateTime.utc(2026, 9, 18, 18, 4, 11));

    expect(
      text,
      matches(RegExp(r'^2026-09-1\d \d\d:\d\d:11 \(UTC[+-]\d\d:\d\d\)$')),
    );
  });

  test('formatLatency', () {
    expect(formatLatency(12), '12 ms');
    expect(formatLatency(2380), '2.4 s');
  });

  group('AppConfig.parseEnvironments', () {
    test('reads name and url, normalising the url', () {
      final environments = AppConfig.parseEnvironments(
        '[{"name":"production","url":"https://yggdrasil.example.com/"},'
        '{"name":"homologation","url":"https://yggdrasil.hml.example.com"}]',
      );

      expect(environments.map((e) => e.name), <String>[
        'production',
        'homologation',
      ]);
      expect(environments.first.url, 'https://yggdrasil.example.com');
      expect(environments.first.id, 'url:https://yggdrasil.example.com');
    });

    test('is empty when undefined or unreadable', () {
      expect(AppConfig.parseEnvironments(''), isEmpty);
      expect(AppConfig.parseEnvironments('not json'), isEmpty);
      expect(AppConfig.parseEnvironments('{"name":"x"}'), isEmpty);
    });

    test('skips invalid and duplicate entries', () {
      final environments = AppConfig.parseEnvironments(
        '[{"name":"a","url":"ftp://x"},{"name":"","url":"https://a.com"},'
        '{"url":"https://b.com"},{"name":"c","url":"https://c.com"},'
        '{"name":"c2","url":"https://c.com/"},42]',
      );

      expect(environments.map((e) => e.name), <String>['c']);
    });
  });

  group('validateBaseUrl', () {
    test('accepts http(s) URLs and the demo', () {
      expect(validateBaseUrl('https://yggdrasil.example.com'), isNull);
      expect(validateBaseUrl('http://localhost:8090/'), isNull);
      expect(validateBaseUrl(Environment.demoUrl), isNull);
    });

    test('rejects anything else', () {
      expect(validateBaseUrl(''), isNotNull);
      expect(validateBaseUrl('yggdrasil.example.com'), isNotNull);
      expect(validateBaseUrl('ftp://example.com'), isNotNull);
      expect(validateBaseUrl('https://example.com/?a=1'), isNotNull);
    });
  });
}
