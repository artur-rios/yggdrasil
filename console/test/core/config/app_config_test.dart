import 'package:flutter_test/flutter_test.dart';
import 'package:yggdrasil_console/core/config/app_config.dart';

void main() {
  group('AppConfig.parseEnvironments', () {
    test('given a list of name and url, when parsed, then each is read with '
        'its url normalised and an id derived from it', () {
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
      expect(environments.first.nameFromResponse, isFalse);
    });

    test('given an undefined, unreadable or non-list value, when parsed, then '
        'there are no environments', () {
      expect(AppConfig.parseEnvironments(''), isEmpty);
      expect(AppConfig.parseEnvironments('   '), isEmpty);
      expect(AppConfig.parseEnvironments('not json'), isEmpty);
      expect(AppConfig.parseEnvironments('{"name":"x"}'), isEmpty);
    });

    test('given invalid and duplicate entries, when parsed, then they are '
        'skipped', () {
      final environments = AppConfig.parseEnvironments(
        '[{"name":"a","url":"ftp://x"},{"name":"","url":"https://a.com"},'
        '{"url":"https://b.com"},{"name":"c","url":"https://c.com"},'
        '{"name":"c2","url":"https://c.com/"},42]',
      );

      expect(environments.map((e) => e.name), <String>['c']);
    });

    test('given a name with blanks around it, when parsed, then the name is '
        'trimmed', () {
      final environments = AppConfig.parseEnvironments(
        '[{"name":"  vps  ","url":"https://yggdrasil.example.com"},'
        '{"name":"   ","url":"https://other.example.com"}]',
      );

      expect(environments.single.name, 'vps');
    });

    test('given a name or url that is not a string, when parsed, then the '
        'entry is skipped', () {
      final environments = AppConfig.parseEnvironments(
        '[{"name":1,"url":"https://a.example.com"},'
        '{"name":"b","url":true},'
        '{"name":"c","url":"https://c.example.com"}]',
      );

      expect(environments.map((e) => e.name), <String>['c']);
    });
  });

  group('AppConfig', () {
    test('given no environments, when constructed, then it offers none', () {
      expect(const AppConfig().environments, isEmpty);
    });

    test('given no YGGDRASIL_ENVIRONMENTS define, when read from the '
        'environment, then it offers none', () {
      expect(AppConfig.fromEnvironment().environments, isEmpty);
    });
  });
}
