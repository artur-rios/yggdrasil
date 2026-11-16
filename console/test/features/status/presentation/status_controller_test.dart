import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_riverpod/misc.dart' show Override;
import 'package:flutter_test/flutter_test.dart';
import 'package:yggdrasil_console/features/environments/data/environment_store.dart';
import 'package:yggdrasil_console/features/environments/domain/environment.dart';
import 'package:yggdrasil_console/features/environments/presentation/environments_controller.dart';
import 'package:yggdrasil_console/features/status/data/refresh_settings_store.dart';
import 'package:yggdrasil_console/features/status/data/status_repository.dart';
import 'package:yggdrasil_console/features/status/data/status_source.dart';
import 'package:yggdrasil_console/features/status/domain/status.dart';
import 'package:yggdrasil_console/features/status/presentation/refresh_settings.dart';
import 'package:yggdrasil_console/features/status/presentation/status_controller.dart';

import '../../../helpers.dart';

HostStatus answerFrom(String host) => HostStatus(
  host: host,
  generatedAt: null,
  status: Status.up,
  environments: const <HostEnvironment>[],
  systems: const <SystemStatus>[],
);

const movedUrl = 'https://yggdrasil.new.example.com';

/// A store whose remembered environment is held until [answer] completes.
class SlowRefreshSettingsStore extends InMemoryRefreshSettingsStore {
  final Completer<void> answer = Completer<void>();

  @override
  Future<String?> readEnvironmentFor(String hostId) async {
    await answer.future;

    return super.readEnvironmentFor(hostId);
  }
}

/// A container over [source] with [environments] (production by default),
/// loaded once.
Future<ProviderContainer> loadedContainer(
  FakeStatusSource source, {
  List<Environment> environments = const <Environment>[production],
  Map<String, String>? tokens,
  RefreshSettingsStore? refreshSettings,
}) async {
  final container = ProviderContainer(
    overrides: <Override>[
      environmentStoreProvider.overrideWithValue(
        InMemoryEnvironmentStore(environments: environments),
      ),
      tokenStoreProvider.overrideWithValue(InMemoryTokenStore(tokens)),
      refreshSettingsStoreProvider.overrideWithValue(
        refreshSettings ?? InMemoryRefreshSettingsStore(),
      ),
      statusRepositoryProvider.overrideWithValue(StatusRepository(source)),
    ],
  );
  addTearDown(container.dispose);
  container
    ..listen(statusControllerProvider, (_, _) {})
    ..listen(selectedHostEnvironmentProvider, (_, _) {});

  await container.read(environmentsControllerProvider.future);
  await pumpEventQueue();

  return container;
}

StatusViewState statusOf(ProviderContainer container) =>
    container.read(statusControllerProvider);

EnvironmentsController environmentsOf(ProviderContainer container) =>
    container.read(environmentsControllerProvider.notifier);

void main() {
  group('a late answer from the URL an environment had before', () {
    // production's first request, to its old URL, is held until the test
    // lets it answer; requests to the new URL answer at once (or fail).
    late Completer<HostStatus> oldHost;
    late FakeStatusSource source;
    late ProviderContainer container;

    setUp(() async {
      oldHost = Completer<HostStatus>();
      source = FakeStatusSource(
        (environment, _) => environment.url == production.url
            ? oldHost.future
            : Future<HostStatus>.value(answerFrom('new')),
      );
      container = await loadedContainer(source);
      expect(source.calls.single.$1.url, production.url);

      // The URL is edited while that request is still out.
      await environmentsOf(container).save(production.copyWith(url: movedUrl));
      await pumpEventQueue();
    });

    test('given the URL edited during a request, when the old answer comes, '
        'then it is dropped', () async {
      expect(source.calls.last.$1.url, movedUrl);
      expect(statusOf(container).snapshot?.status.host, 'new');

      oldHost.complete(answerFrom('old'));
      await pumpEventQueue();

      final state = statusOf(container);
      expect(state.environment?.url, movedUrl);
      expect(state.snapshot?.status.host, 'new');
    });

    test('given the old answer came late, when the new URL then fails, then '
        "the stale data is the new URL's, never the old one's", () async {
      oldHost.complete(answerFrom('old'));
      await pumpEventQueue();

      source.respond = (_, _) =>
          throw const StatusException(StatusFailureKind.network);
      await container.read(statusControllerProvider.notifier).refresh();

      final state = statusOf(container);
      expect(state.failure?.kind, StatusFailureKind.network);
      expect(state.isStale, isTrue);
      expect(state.snapshot?.status.host, 'new');
    });
  });

  group('a load is the map, then the details', () {
    test('given a host, when loaded, then the map is asked first and the '
        'details after it', () async {
      final source = FakeStatusSource((_, _) async => answerFrom('map'))
        ..respondDetails = (_, _) async => answerFrom('details');
      final container = await loadedContainer(source);

      expect(source.calls, hasLength(1));
      expect(source.detailsCalls, hasLength(1));

      final state = statusOf(container);
      expect(state.snapshot?.status.host, 'details');
      expect(state.loading, isFalse);
      expect(state.failure, isNull);
    });

    test('given slow details, when loaded, then the map is drawn at once and '
        'loading holds until the details', () async {
      final details = Completer<HostStatus?>();
      final source = FakeStatusSource((_, _) async => answerFrom('map'))
        ..respondDetails = (_, _) => details.future;
      final container = await loadedContainer(source);

      var state = statusOf(container);
      expect(state.snapshot?.status.host, 'map');
      expect(state.loading, isTrue);

      details.complete(answerFrom('details'));
      await pumpEventQueue();

      state = statusOf(container);
      expect(state.snapshot?.status.host, 'details');
      expect(state.loading, isFalse);
    });

    test('given the map fails, when loaded, then the details are not asked '
        'for', () async {
      final source = FakeStatusSource(
        (_, _) => throw const StatusException(StatusFailureKind.network),
      );
      final container = await loadedContainer(source);

      expect(source.detailsCalls, isEmpty);

      final state = statusOf(container);
      expect(state.failure?.kind, StatusFailureKind.network);
      expect(state.loading, isFalse);
      expect(state.snapshot, isNull);
    });

    test(
      'given the details fail, when loaded, then the map is kept, stale',
      () async {
        final source = FakeStatusSource((_, _) async => answerFrom('map'))
          ..respondDetails = (_, _) =>
              throw const StatusException(StatusFailureKind.server);
        final container = await loadedContainer(source);

        final state = statusOf(container);
        expect(state.snapshot?.status.host, 'map');
        expect(state.failure?.kind, StatusFailureKind.server);
        expect(state.isStale, isTrue);
        expect(state.loading, isFalse);
      },
    );

    test('given an API without the details endpoint, when loaded, then the '
        'map is kept with no failure', () async {
      final source = FakeStatusSource((_, _) async => answerFrom('map'))
        ..respondDetails = (_, _) async => null;
      final container = await loadedContainer(source);

      final state = statusOf(container);
      expect(state.snapshot?.status.host, 'map');
      expect(state.failure, isNull);
      expect(state.loading, isFalse);
    });

    test('given the URL edited while the details are out, when the old '
        'details come, then they are discarded', () async {
      final oldDetails = Completer<HostStatus?>();
      final source = FakeStatusSource((_, _) async => answerFrom('map'))
        ..respondDetails = (environment, _) => environment.url == production.url
            ? oldDetails.future
            : Future<HostStatus?>.value(answerFrom('new-details'));
      final container = await loadedContainer(source);

      await environmentsOf(container).save(production.copyWith(url: movedUrl));
      await pumpEventQueue();
      expect(statusOf(container).snapshot?.status.host, 'new-details');

      oldDetails.complete(answerFrom('old-details'));
      await pumpEventQueue();

      final state = statusOf(container);
      expect(state.environment?.url, movedUrl);
      expect(state.snapshot?.status.host, 'new-details');
      expect(state.loading, isFalse);
    });

    test('given a host renamed, when the state rebuilds, then it does not '
        'load again', () async {
      final source = FakeStatusSource();
      final container = await loadedContainer(source);

      await environmentsOf(container)
          .save(production.copyWith(name: 'renamed'));
      await pumpEventQueue();

      expect(source.calls, hasLength(1));
      expect(source.detailsCalls, hasLength(1));
      expect(statusOf(container).environment?.name, 'renamed');
    });

    test(
      'given a saved token, when loaded, then both requests carry it',
      () async {
        final source = FakeStatusSource();
        await loadedContainer(
          source,
          tokens: <String, String>{'production': 'secret'},
        );

        expect(source.calls.single.$2, 'secret');
        expect(source.detailsCalls.single.$2, 'secret');
      },
    );

    test('given a placeholder host, when the map answers, then the host is '
        'named after the response', () async {
      final source = FakeStatusSource(
        (_, _) async => answerFrom('example.com'),
      );
      final container = await loadedContainer(
        source,
        environments: <Environment>[
          production.copyWith(nameFromResponse: true),
        ],
      );

      final selected = container
          .read(environmentsControllerProvider)
          .requireValue
          .selected!;
      expect(selected.name, 'example.com');
      expect(selected.nameFromResponse, isFalse);
      // Renaming is not a reason to load again.
      expect(source.calls, hasLength(1));
    });

    test('given a placeholder host, when the map fails, then it keeps its '
        'placeholder name', () async {
      final placeholder = production.copyWith(nameFromResponse: true);
      final container = await loadedContainer(
        FakeStatusSource(
          (_, _) => throw const StatusException(StatusFailureKind.network),
        ),
        environments: <Environment>[placeholder],
      );

      expect(
        container.read(environmentsControllerProvider).requireValue.selected,
        placeholder,
      );
    });
  });

  group('refresh', () {
    test('given a load in flight, when refreshed again, then no second '
        'request is made', () async {
      final map = Completer<HostStatus>();
      final source = FakeStatusSource((_, _) => map.future);
      final container = await loadedContainer(source);
      expect(statusOf(container).loading, isTrue);

      await container.read(statusControllerProvider.notifier).refresh();
      expect(source.calls, hasLength(1));

      map.complete(demoStatus());
      await pumpEventQueue();

      expect(source.calls, hasLength(1));
      expect(source.detailsCalls, hasLength(1));
      expect(statusOf(container).loading, isFalse);
    });

    test('given a loaded host, when refreshed, then it loads again', () async {
      final source = FakeStatusSource();
      final container = await loadedContainer(source);

      await container.read(statusControllerProvider.notifier).refresh();

      expect(source.calls, hasLength(2));
      expect(source.detailsCalls, hasLength(2));
    });

    test('given no host, when refreshed, then nothing is requested and the '
        'state is empty', () async {
      final source = FakeStatusSource();
      final container = await loadedContainer(
        source,
        environments: const <Environment>[],
      );

      await container.read(statusControllerProvider.notifier).refresh();

      final state = statusOf(container);
      expect(source.calls, isEmpty);
      expect(state.environment, isNull);
      expect(state.snapshot, isNull);
      expect(state.loading, isFalse);
    });

    test('given two hosts, when the other is selected, then it loads, and '
        'switching back shows what was known at once', () async {
      final source = FakeStatusSource(
        (environment, _) async => answerFrom(environment.id),
      );
      final container = await loadedContainer(
        source,
        environments: const <Environment>[production, homologation],
      );

      await environmentsOf(container).select('homologation');
      await pumpEventQueue();
      expect(source.calls.last.$1, homologation);
      expect(statusOf(container).snapshot?.status.host, 'homologation');

      await environmentsOf(container).select('production');

      expect(statusOf(container).environment, production);
      expect(statusOf(container).snapshot?.status.host, 'production');

      // The reload it started ends before the container is disposed.
      await pumpEventQueue();
      expect(statusOf(container).loading, isFalse);
      expect(source.calls.map((call) => call.$1.id), <String>[
        'production',
        'homologation',
        'production',
      ]);
    });
  });

  group('StatusViewState', () {
    const snapshotless = StatusViewState(environment: production);

    test('given no failure, when inspected, then it is neither stale, '
        'starting nor needing a token', () {
      expect(snapshotless.isStale, isFalse);
      expect(snapshotless.isStarting, isFalse);
      expect(snapshotless.needsToken, isFalse);
    });

    test('given a 401, when inspected, then it needs a token', () {
      const state = StatusViewState(
        failure: StatusException(StatusFailureKind.unauthorized),
      );

      expect(state.needsToken, isTrue);
      expect(state.isStarting, isFalse);
    });

    test('given the API starting over known data, when inspected, then it is '
        'starting and not stale', () {
      final state = StatusViewState(
        snapshot: StatusSnapshot(
          status: answerFrom('x'),
          receivedAt: DateTime.utc(2026),
        ),
        failure: const StatusException(StatusFailureKind.starting),
      );

      expect(state.isStarting, isTrue);
      expect(state.isStale, isFalse);
    });

    test(
      'given a failure and no data, when inspected, then it is not stale',
      () {
        const state = StatusViewState(
          failure: StatusException(StatusFailureKind.network),
        );

        expect(state.isStale, isFalse);
      },
    );
  });

  group('selectedHostEnvironmentProvider', () {
    test('given nothing remembered, when read, then it is null', () async {
      final container = await loadedContainer(FakeStatusSource());

      expect(container.read(selectedHostEnvironmentProvider), isNull);
    });

    test(
      'given a remembered environment, when read, then it is that one',
      () async {
        final store = InMemoryRefreshSettingsStore();
        await store.writeEnvironmentFor('production', 'development');

        final container = await loadedContainer(
          FakeStatusSource(),
          refreshSettings: store,
        );

        expect(container.read(selectedHostEnvironmentProvider), 'development');
      },
    );

    test('given a choice, when made, then it is remembered for that host and '
        'does not load', () async {
      final store = InMemoryRefreshSettingsStore();
      final source = FakeStatusSource();
      final container = await loadedContainer(source, refreshSettings: store);

      await container
          .read(selectedHostEnvironmentProvider.notifier)
          .select('homologation');

      expect(container.read(selectedHostEnvironmentProvider), 'homologation');
      expect(await store.readEnvironmentFor('production'), 'homologation');
      expect(source.calls, hasLength(1));
    });

    test('given a choice made before the memory is read, when the memory '
        'answers, then the choice wins', () async {
      final store = SlowRefreshSettingsStore();
      await store.writeEnvironmentFor('production', 'development');
      final container = await loadedContainer(
        FakeStatusSource(),
        refreshSettings: store,
      );

      await container
          .read(selectedHostEnvironmentProvider.notifier)
          .select('homologation');
      store.answer.complete();
      await pumpEventQueue();

      expect(container.read(selectedHostEnvironmentProvider), 'homologation');
    });

    test('given another host selected, when read, then it starts from that '
        "host's own memory", () async {
      final store = InMemoryRefreshSettingsStore();
      await store.writeEnvironmentFor('homologation', 'production');
      final container = await loadedContainer(
        FakeStatusSource(),
        environments: const <Environment>[production, homologation],
        refreshSettings: store,
      );

      await container
          .read(selectedHostEnvironmentProvider.notifier)
          .select('development');
      await environmentsOf(container).select('homologation');
      await pumpEventQueue();

      expect(container.read(selectedHostEnvironmentProvider), 'production');
    });

    test('given no host, when an environment is chosen, then it is shown but '
        'not remembered', () async {
      final store = InMemoryRefreshSettingsStore();
      final container = await loadedContainer(
        FakeStatusSource(),
        environments: const <Environment>[],
        refreshSettings: store,
      );

      await container
          .read(selectedHostEnvironmentProvider.notifier)
          .select('production');

      expect(container.read(selectedHostEnvironmentProvider), 'production');
      expect(await store.readEnvironmentFor('production'), isNull);
    });
  });

  test('given no override, when the repository is read, then it says it '
      'must be overridden', () {
    final container = ProviderContainer();
    addTearDown(container.dispose);

    expect(() => container.read(statusRepositoryProvider), throwsA(anything));
  });
}
