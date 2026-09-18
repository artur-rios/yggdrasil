import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:yggdrasil_console/core/config/app_config.dart';
import 'package:yggdrasil_console/features/environments/data/environment_store.dart';
import 'package:yggdrasil_console/features/environments/domain/environment.dart';
import 'package:yggdrasil_console/features/environments/presentation/environments_controller.dart';

import '../../helpers.dart';

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

  setUp(() {
    store = InMemoryEnvironmentStore();
    tokens = InMemoryTokenStore();
  });

  group('defaults when nothing is saved', () {
    test('no environments off the web without compiled-in ones', () async {
      final state = await container().read(
        environmentsControllerProvider.future,
      );

      expect(state.environments, isEmpty);
      expect(state.selected, isNull);
    });

    test('the page origin on the web, to be named by the response', () async {
      final state = await container(webOrigin: 'https://yggdrasil.example.com')
          .read(environmentsControllerProvider.future);

      final environment = state.environments.single;
      expect(environment.url, 'https://yggdrasil.example.com');
      expect(environment.name, 'yggdrasil.example.com');
      expect(environment.nameFromResponse, isTrue);
      expect(state.selected, environment);
    });

    test('the compiled-in environments, after the page origin', () async {
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

    test('the page origin is not repeated when compiled in', () async {
      final config = AppConfig(
        environments: AppConfig.parseEnvironments(
          '[{"name":"production","url":"https://yggdrasil.example.com"}]',
        ),
      );

      final state = await container(
        config: config,
        webOrigin: 'https://yggdrasil.example.com',
      ).read(environmentsControllerProvider.future);

      expect(state.environments.single.name, 'production');
      expect(state.environments.single.nameFromResponse, isFalse);
    });

    test('a saved list wins over the defaults', () async {
      store.environments = <Environment>[homologation];

      final state = await container(webOrigin: 'https://yggdrasil.example.com')
          .read(environmentsControllerProvider.future);

      expect(state.environments, <Environment>[homologation]);
    });
  });

  test('restores the selection and which environments have a token', () async {
    store
      ..environments = <Environment>[production, homologation]
      ..selectedId = 'homologation';
    tokens.tokens['production'] = 'secret';

    final state = await container().read(environmentsControllerProvider.future);

    expect(state.selected, homologation);
    expect(state.withToken, <String>{'production'});
  });

  test(
    'save adds, selects and persists; tokens go to the token store',
    () async {
      final c = container();
      final controller = c.read(environmentsControllerProvider.notifier);
      await c.read(environmentsControllerProvider.future);

      await controller.save(production, token: ' secret ');

      final state = c.read(environmentsControllerProvider).requireValue;
      expect(state.environments, <Environment>[production]);
      expect(state.selected, production);
      expect(state.withToken, <String>{'production'});
      expect(store.environments, <Environment>[production]);
      expect(store.selectedId, 'production');
      expect(tokens.tokens['production'], 'secret');
    },
  );

  test('save with an empty token forgets it; null keeps it', () async {
    store.environments = <Environment>[production];
    tokens.tokens['production'] = 'secret';
    final c = container();
    final controller = c.read(environmentsControllerProvider.notifier);
    await c.read(environmentsControllerProvider.future);

    await controller.save(production.copyWith(name: 'prod'));
    expect(tokens.tokens['production'], 'secret');

    await controller.save(production, token: '');
    expect(tokens.tokens.containsKey('production'), isFalse);
    expect(
      c.read(environmentsControllerProvider).requireValue.withToken,
      isEmpty,
    );
  });

  test('remove deletes the environment and its token', () async {
    store
      ..environments = <Environment>[production, homologation]
      ..selectedId = 'production';
    tokens.tokens['production'] = 'secret';
    final c = container();
    final controller = c.read(environmentsControllerProvider.notifier);
    await c.read(environmentsControllerProvider.future);

    await controller.remove('production');

    final state = c.read(environmentsControllerProvider).requireValue;
    expect(state.environments, <Environment>[homologation]);
    expect(state.selected, homologation);
    expect(tokens.tokens, isEmpty);
    expect(store.environments, <Environment>[homologation]);
  });

  test('adoptResponseName renames a placeholder once, and persists', () async {
    final c = container(webOrigin: 'https://yggdrasil.example.com');
    final controller = c.read(environmentsControllerProvider.notifier);
    final id = (await c.read(environmentsControllerProvider.future))
        .environments
        .single
        .id;

    await controller.adoptResponseName(id, 'production');
    await controller.adoptResponseName(id, 'something-else');

    final environment = c
        .read(environmentsControllerProvider)
        .requireValue
        .environments
        .single;
    expect(environment.name, 'production');
    expect(environment.nameFromResponse, isFalse);
    expect(store.environments!.single.name, 'production');
  });

  test('adoptResponseName leaves a name the user chose', () async {
    store.environments = <Environment>[homologation];
    final c = container();
    final controller = c.read(environmentsControllerProvider.notifier);
    await c.read(environmentsControllerProvider.future);

    await controller.adoptResponseName('homologation', 'production');

    expect(
      c.read(environmentsControllerProvider).requireValue.selected!.name,
      'homologation',
    );
  });

  test('addDemo adds the demo once and selects it', () async {
    store.environments = <Environment>[production];
    final c = container();
    final controller = c.read(environmentsControllerProvider.notifier);
    await c.read(environmentsControllerProvider.future);

    await controller.addDemo();
    await controller.select('production');
    await controller.addDemo();

    final state = c.read(environmentsControllerProvider).requireValue;
    expect(state.environments.where((e) => e.isDemo), hasLength(1));
    expect(state.selected!.isDemo, isTrue);
  });
}
