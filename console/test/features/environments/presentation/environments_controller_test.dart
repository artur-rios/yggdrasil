import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_riverpod/misc.dart' show Override;
import 'package:flutter_test/flutter_test.dart';
import 'package:yggdrasil_console/core/config/app_config.dart';
import 'package:yggdrasil_console/features/environments/data/environment_store.dart';
import 'package:yggdrasil_console/features/environments/domain/environment.dart';
import 'package:yggdrasil_console/features/environments/presentation/environments_controller.dart';

import '../../../helpers.dart';

void main() {
  late InMemoryEnvironmentStore store;
  late InMemoryTokenStore tokens;

  ProviderContainer container({
    AppConfig config = const AppConfig(),
    String? webOrigin,
  }) {
    final container = ProviderContainer(
      overrides: <Override>[
        appConfigProvider.overrideWithValue(config),
        environmentStoreProvider.overrideWithValue(store),
        tokenStoreProvider.overrideWithValue(tokens),
        webOriginProvider.overrideWithValue(webOrigin),
      ],
    );
    addTearDown(container.dispose);

    return container;
  }

  /// A container whose environments are loaded, and its controller.
  Future<(ProviderContainer, EnvironmentsController)> loaded() async {
    final c = container();
    await c.read(environmentsControllerProvider.future);

    return (c, c.read(environmentsControllerProvider.notifier));
  }

  EnvironmentsState stateOf(ProviderContainer c) =>
      c.read(environmentsControllerProvider).requireValue;

  setUp(() {
    store = InMemoryEnvironmentStore();
    tokens = InMemoryTokenStore();
  });

  group('defaults when nothing is saved', () {
    test('given no compiled-in environments off the web, when loaded, then '
        'there are none and none is selected', () async {
      final state = await container().read(
        environmentsControllerProvider.future,
      );

      expect(state.environments, isEmpty);
      expect(state.selected, isNull);
      expect(state.selectedId, isNull);
    });

    test('given a page origin on the web, when loaded, then it is the host, '
        'to be named by the response', () async {
      final state = await container(webOrigin: 'https://yggdrasil.example.com')
          .read(environmentsControllerProvider.future);

      final environment = state.environments.single;
      expect(environment.url, 'https://yggdrasil.example.com');
      expect(environment.name, 'yggdrasil.example.com');
      expect(environment.nameFromResponse, isTrue);
      expect(state.selected, environment);
    });

    test('given compiled-in environments and another page origin, when '
        'loaded, then the origin comes first and the compiled-in ones '
        'after it', () async {
      final config = AppConfig(
        environments: AppConfig.parseEnvironments(
          '[{"name":"production","url":"https://yggdrasil.example.com"},'
          '{"name":"homologation","url":"https://yggdrasil.hml.example.com"}]',
        ),
      );

      final onOtherHost = await container(
        config: config,
        webOrigin: 'http://localhost:8090',
      ).read(environmentsControllerProvider.future);

      expect(onOtherHost.environments.map((e) => e.name), <String>[
        'localhost',
        'production',
        'homologation',
      ]);
    });

    test('given the page origin is compiled in, when loaded, then it is not '
        'repeated', () async {
      final config = AppConfig(
        environments: AppConfig.parseEnvironments(
          '[{"name":"production","url":"https://yggdrasil.example.com"}]',
        ),
      );

      final state = await container(
        config: config,
        webOrigin: 'https://yggdrasil.example.com/',
      ).read(environmentsControllerProvider.future);

      expect(state.environments.single.name, 'production');
      expect(state.environments.single.nameFromResponse, isFalse);
    });

    test('given a page origin that is not an http(s) URL, when loaded, then '
        'it is ignored', () {
      expect(
        EnvironmentsController.defaultEnvironments(
          const AppConfig(),
          'file://',
        ),
        isEmpty,
      );
    });

    test(
      'given a saved list, when loaded, then it wins over the defaults',
      () async {
        store.environments = <Environment>[homologation];

        final state = await container(
          webOrigin: 'https://yggdrasil.example.com',
        ).read(environmentsControllerProvider.future);

        expect(state.environments, <Environment>[homologation]);
      },
    );

    test('given an empty saved list, when loaded, then it wins over the '
        'defaults too', () async {
      store.environments = <Environment>[];

      final state = await container(webOrigin: 'https://yggdrasil.example.com')
          .read(environmentsControllerProvider.future);

      expect(state.environments, isEmpty);
    });
  });

  group('loading', () {
    test('given a saved selection and tokens, when loaded, then the '
        'selection and which environments have a token are restored', () async {
      store
        ..environments = <Environment>[production, homologation]
        ..selectedId = 'homologation';
      tokens.tokens['production'] = 'secret';

      final state = await container().read(
        environmentsControllerProvider.future,
      );

      expect(state.selected, homologation);
      expect(state.withToken, <String>{'production'});
    });

    test('given a saved selection that is gone, when loaded, then the first '
        'environment is selected', () async {
      store
        ..environments = <Environment>[production, homologation]
        ..selectedId = 'staging';

      final state = await container().read(
        environmentsControllerProvider.future,
      );

      expect(state.selectedId, 'production');
    });

    test('given an empty saved token, when loaded, then the environment '
        'counts as having none', () async {
      store.environments = <Environment>[production];
      tokens.tokens['production'] = '';

      final state = await container().read(
        environmentsControllerProvider.future,
      );

      expect(state.withToken, isEmpty);
    });
  });

  group('EnvironmentsState', () {
    test('given a selected id not in the list, when the selection is read, '
        'then it is the first environment', () {
      const state = EnvironmentsState(
        environments: <Environment>[production, homologation],
        selectedId: 'staging',
        withToken: <String>{},
      );

      expect(state.selected, production);
      expect(state.byId('staging'), isNull);
      expect(state.byId('homologation'), homologation);
    });

    test('given no environments, when the selection is read, then there is '
        'none', () {
      const state = EnvironmentsState(
        environments: <Environment>[],
        selectedId: null,
        withToken: <String>{},
      );

      expect(state.selected, isNull);
    });
  });

  group('select', () {
    test(
      'given a known id, when selected, then it is shown and saved',
      () async {
        store.environments = <Environment>[production, homologation];
        final (c, controller) = await loaded();

        await controller.select('homologation');

        expect(stateOf(c).selected, homologation);
        expect(store.selectedId, 'homologation');
      },
    );

    test('given an unknown id, when selected, then nothing changes', () async {
      store
        ..environments = <Environment>[production]
        ..selectedId = 'production';
      final (c, controller) = await loaded();

      await controller.select('staging');

      expect(stateOf(c).selectedId, 'production');
      expect(store.selectedId, 'production');
    });
  });

  group('save', () {
    test('given a new environment with a token, when saved, then it is '
        'added, selected and persisted, and the trimmed token goes to the '
        'token store', () async {
      final (c, controller) = await loaded();

      await controller.save(production, token: ' secret ');

      final state = stateOf(c);
      expect(state.environments, <Environment>[production]);
      expect(state.selected, production);
      expect(state.withToken, <String>{'production'});
      expect(store.environments, <Environment>[production]);
      expect(store.selectedId, 'production');
      expect(tokens.tokens['production'], 'secret');
    });

    test('given an existing environment, when saved, then it is replaced in '
        'place and the selection stays', () async {
      store
        ..environments = <Environment>[production, homologation]
        ..selectedId = 'production';
      final (c, controller) = await loaded();

      await controller.save(homologation.copyWith(name: 'hml'));

      final state = stateOf(c);
      expect(state.environments.map((e) => e.name), <String>[
        'production',
        'hml',
      ]);
      expect(state.selectedId, 'production');
      expect(store.selectedId, 'production');
    });

    test('given a saved token, when saved with a null token, then it is '
        'kept, and with an empty one, then it is forgotten', () async {
      store.environments = <Environment>[production];
      tokens.tokens['production'] = 'secret';
      final (c, controller) = await loaded();

      await controller.save(production.copyWith(name: 'prod'));
      expect(tokens.tokens['production'], 'secret');
      expect(stateOf(c).withToken, <String>{'production'});

      await controller.save(production, token: '');
      expect(tokens.tokens.containsKey('production'), isFalse);
      expect(stateOf(c).withToken, isEmpty);
    });

    test('given a blank token, when saved, then it is forgotten like an empty '
        'one', () async {
      store.environments = <Environment>[production];
      tokens.tokens['production'] = 'secret';
      final (c, controller) = await loaded();

      await controller.save(production, token: '   ');

      expect(tokens.tokens, isEmpty);
      expect(stateOf(c).withToken, isEmpty);
    });
  });

  group('remove', () {
    test('given the selected environment, when removed, then it and its '
        'token are gone and the next one is selected', () async {
      store
        ..environments = <Environment>[production, homologation]
        ..selectedId = 'production';
      tokens.tokens['production'] = 'secret';
      final (c, controller) = await loaded();

      await controller.remove('production');

      final state = stateOf(c);
      expect(state.environments, <Environment>[homologation]);
      expect(state.selected, homologation);
      expect(state.withToken, isEmpty);
      expect(tokens.tokens, isEmpty);
      expect(store.environments, <Environment>[homologation]);
      expect(store.selectedId, 'homologation');
    });

    test('given an environment that is not selected, when removed, then the '
        'selection stays', () async {
      store
        ..environments = <Environment>[production, homologation]
        ..selectedId = 'production';
      final (c, controller) = await loaded();

      await controller.remove('homologation');

      expect(stateOf(c).selectedId, 'production');
      expect(store.environments, <Environment>[production]);
    });

    test(
      'given the last environment, when removed, then none is selected',
      () async {
        store
          ..environments = <Environment>[production]
          ..selectedId = 'production';
        final (c, controller) = await loaded();

        await controller.remove('production');

        final state = stateOf(c);
        expect(state.environments, isEmpty);
        expect(state.selectedId, isNull);
        expect(state.selected, isNull);
        expect(store.environments, isEmpty);
      },
    );
  });

  group('tokens', () {
    test(
      'given a token set, when read, then it is saved trimmed and marked',
      () async {
        store.environments = <Environment>[production];
        final (c, controller) = await loaded();

        await controller.setToken('production', ' secret ');

        expect(await controller.tokenFor('production'), 'secret');
        expect(stateOf(c).withToken, <String>{'production'});
      },
    );

    test(
      'given a saved token, when set to empty, then it is removed',
      () async {
        store.environments = <Environment>[production];
        tokens.tokens['production'] = 'secret';
        final (c, controller) = await loaded();

        await controller.setToken('production', '');

        expect(await controller.tokenFor('production'), isNull);
        expect(stateOf(c).withToken, isEmpty);
      },
    );
  });

  group('adoptResponseName', () {
    test('given a placeholder host, when named twice, then only the first '
        'name is adopted, and persisted', () async {
      final c = container(webOrigin: 'https://yggdrasil.example.com');
      final controller = c.read(environmentsControllerProvider.notifier);
      final id = (await c.read(environmentsControllerProvider.future))
          .environments
          .single
          .id;

      await controller.adoptResponseName(id, ' example.com ');
      await controller.adoptResponseName(id, 'something-else');

      final environment = stateOf(c).environments.single;
      expect(environment.name, 'example.com');
      expect(environment.nameFromResponse, isFalse);
      expect(store.environments!.single.name, 'example.com');
    });

    test('given a name the user chose, when a response names the host, then '
        'the name stays', () async {
      store.environments = <Environment>[homologation];
      final (c, controller) = await loaded();

      await controller.adoptResponseName('homologation', 'example.com');

      expect(stateOf(c).selected!.name, 'homologation');
      expect(store.environments, <Environment>[homologation]);
    });

    test('given a placeholder host, when the response name is blank, then '
        'it stays a placeholder', () async {
      final placeholder = production.copyWith(nameFromResponse: true);
      store.environments = <Environment>[placeholder];
      final (c, controller) = await loaded();

      await controller.adoptResponseName('production', '   ');

      expect(stateOf(c).selected, placeholder);
    });

    test('given an unknown id, when named, then nothing changes', () async {
      store.environments = <Environment>[production];
      final (c, controller) = await loaded();

      await controller.adoptResponseName('staging', 'example.com');

      expect(stateOf(c).environments, <Environment>[production]);
    });
  });

  group('addDemo', () {
    test('given the demo added twice with another host chosen between, when '
        'done, then the demo is there once and selected', () async {
      store.environments = <Environment>[production];
      final (c, controller) = await loaded();

      await controller.addDemo();
      await controller.select('production');
      await controller.addDemo();

      final state = stateOf(c);
      expect(state.environments.where((e) => e.isDemo), hasLength(1));
      expect(state.selected!.isDemo, isTrue);
    });
  });

  group('newEnvironmentId', () {
    test('given a new host, when an id is made, then it is prefixed and not '
        'derived from a URL', () {
      expect(newEnvironmentId(), matches(RegExp(r'^env-[0-9a-z]+$')));
    });
  });
}
