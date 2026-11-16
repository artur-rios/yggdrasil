import 'package:flutter_test/flutter_test.dart';
import 'package:yggdrasil_console/features/environments/domain/environment.dart';

void main() {
  const environment = Environment(
    id: 'production',
    name: 'production',
    url: 'https://yggdrasil.example.com/',
  );

  group('Environment', () {
    test('given an environment, when written to JSON and read back, then it '
        'is equal to the original', () {
      final placeholder = environment.copyWith(nameFromResponse: true);

      expect(Environment.fromJson(environment.toJson()), environment);
      expect(Environment.fromJson(placeholder.toJson()), placeholder);
    });

    test('given JSON without nameFromResponse, when read, then the name is '
        "the user's", () {
      final read = Environment.fromJson(<String, dynamic>{
        'id': 'a',
        'name': 'a',
        'url': 'https://a.example.com',
      });

      expect(read.nameFromResponse, isFalse);
    });

    test(
      'given JSON without an id, name or url, when read, then it throws',
      () {
        for (final key in <String>['id', 'name', 'url']) {
          final json = environment.toJson()..remove(key);

          expect(
            () => Environment.fromJson(json),
            throwsA(anything),
            reason: key,
          );
        }
      },
    );

    test('given a url with trailing slashes, when its base url is read, then '
        'the slashes are gone', () {
      expect(environment.baseUrl, 'https://yggdrasil.example.com');
    });

    test(
      'given the demo, when inspected, then it is the demo and only it is',
      () {
        final demo = Environment.demo();

        expect(demo.id, Environment.demoId);
        expect(demo.url, Environment.demoUrl);
        expect(demo.isDemo, isTrue);
        expect(environment.isDemo, isFalse);
      },
    );

    test('given copyWith with no values, when copied, then the copy is equal '
        'and keeps the id', () {
      expect(environment.copyWith(), environment);
      expect(environment.copyWith().hashCode, environment.hashCode);
    });

    test('given copyWith with new values, when copied, then only those change '
        'and the id stays', () {
      final copy = environment.copyWith(
        name: 'prod',
        url: 'https://new.example.com',
        nameFromResponse: true,
      );

      expect(copy.id, environment.id);
      expect(copy.name, 'prod');
      expect(copy.url, 'https://new.example.com');
      expect(copy.nameFromResponse, isTrue);
    });

    test('given environments differing in any field, when compared, then they '
        'are not equal', () {
      expect(
        const Environment(id: 'other', name: 'production', url: 'x'),
        isNot(environment),
      );
      expect(environment.copyWith(name: 'other'), isNot(environment));
      expect(environment.copyWith(url: 'other'), isNot(environment));
      expect(environment.copyWith(nameFromResponse: true), isNot(environment));
    });
  });

  group('normalizeBaseUrl', () {
    test('given blanks and several trailing slashes, when normalised, then '
        'both are removed', () {
      expect(
        normalizeBaseUrl('  https://yggdrasil.example.com///  '),
        'https://yggdrasil.example.com',
      );
    });

    test('given a url with a path, when normalised, then only the trailing '
        'slash goes', () {
      expect(
        normalizeBaseUrl('https://example.com/yggdrasil/'),
        'https://example.com/yggdrasil',
      );
    });

    test('given only slashes, when normalised, then it is empty', () {
      expect(normalizeBaseUrl('///'), isEmpty);
    });
  });

  group('validateBaseUrl', () {
    test('given http(s) URLs and the demo, when validated, then they are '
        'accepted', () {
      expect(validateBaseUrl('https://yggdrasil.example.com'), isNull);
      expect(validateBaseUrl('http://localhost:8090/'), isNull);
      expect(validateBaseUrl('  https://example.com/path/  '), isNull);
      expect(validateBaseUrl(Environment.demoUrl), isNull);
      expect(validateBaseUrl('demo:/'), isNull);
    });

    test('given anything that is not an http(s) URL with a host, when '
        'validated, then it asks for one', () {
      for (final url in <String>[
        '',
        'yggdrasil.example.com',
        'ftp://example.com',
        'https://',
        'http://:8080',
      ]) {
        expect(validateBaseUrl(url), contains('http(s) URL'), reason: url);
      }
    });

    test('given a URL with a query or a fragment, when validated, then it is '
        'rejected for that', () {
      expect(validateBaseUrl('https://example.com/?a=1'), contains('query'));
      expect(validateBaseUrl('https://example.com/#top'), contains('fragment'));
    });
  });
}
