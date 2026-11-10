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

void main() {
  loadTests();

  // production's first request, to its old URL, is held until the test lets
  // it answer; requests to the new URL answer at once (or fail).
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
    container = ProviderContainer(
      overrides: <Override>[
        environmentStoreProvider.overrideWithValue(
          InMemoryEnvironmentStore(environments: <Environment>[production]),
        ),
        tokenStoreProvider.overrideWithValue(InMemoryTokenStore()),
        statusRepositoryProvider.overrideWithValue(StatusRepository(source)),
      ],
    );
    addTearDown(container.dispose);
    container.listen(statusControllerProvider, (_, _) {});

    await container.read(environmentsControllerProvider.future);
    await pumpEventQueue();
    expect(source.calls.single.$1.url, production.url);

    // The URL is edited while that request is still out.
    await container
        .read(environmentsControllerProvider.notifier)
        .save(production.copyWith(url: movedUrl));
    await pumpEventQueue();
  });

  test(
    'a late answer from the URL an environment had before is dropped',
    () async {
      expect(source.calls.last.$1.url, movedUrl);
      expect(
        container.read(statusControllerProvider).snapshot?.status.host,
        'new',
      );

      oldHost.complete(answerFrom('old'));
      await pumpEventQueue();

      final state = container.read(statusControllerProvider);
      expect(state.environment?.url, movedUrl);
      expect(state.snapshot?.status.host, 'new');
    },
  );

  test(
    'a late answer from the old URL is not kept as the new URL\'s last data',
    () async {
      oldHost.complete(answerFrom('old'));
      await pumpEventQueue();

      source.respond = (_, _) =>
          throw const StatusException(StatusFailureKind.network);
      await container.read(statusControllerProvider.notifier).refresh();

      // Stale data on a failure is the new host's, never the old one's.
      final state = container.read(statusControllerProvider);
      expect(state.failure?.kind, StatusFailureKind.network);
      expect(state.isStale, isTrue);
      expect(state.snapshot?.status.host, 'new');
    },
  );
}

/// A container over [source] with one host, production, already loaded once.
Future<ProviderContainer> loadedContainer(
  FakeStatusSource source, {
  RefreshSettingsStore? refreshSettings,
}) async {
  final container = ProviderContainer(
    overrides: <Override>[
      environmentStoreProvider.overrideWithValue(
        InMemoryEnvironmentStore(environments: <Environment>[production]),
      ),
      tokenStoreProvider.overrideWithValue(InMemoryTokenStore()),
      refreshSettingsStoreProvider.overrideWithValue(
        refreshSettings ?? InMemoryRefreshSettingsStore(),
      ),
      statusRepositoryProvider.overrideWithValue(StatusRepository(source)),
    ],
  );
  addTearDown(container.dispose);
  container.listen(statusControllerProvider, (_, _) {});

  await container.read(environmentsControllerProvider.future);
  await pumpEventQueue();

  return container;
}

void loadTests() {
  group('a load is the map, then the details', () {
    test('asks for the map first and the details after it', () async {
      final source = FakeStatusSource((_, _) async => answerFrom('map'))
        ..respondDetails = (_, _) async => answerFrom('details');
      final container = await loadedContainer(source);

      expect(source.calls, hasLength(1));
      expect(source.detailsCalls, hasLength(1));

      final state = container.read(statusControllerProvider);
      expect(state.snapshot?.status.host, 'details');
      expect(state.loading, isFalse);
      expect(state.failure, isNull);
    });

    test('draws the map at once and keeps loading until the details', () async {
      final details = Completer<HostStatus?>();
      final source = FakeStatusSource((_, _) async => answerFrom('map'))
        ..respondDetails = (_, _) => details.future;
      final container = await loadedContainer(source);

      var state = container.read(statusControllerProvider);
      expect(state.snapshot?.status.host, 'map');
      expect(state.loading, isTrue);

      details.complete(answerFrom('details'));
      await pumpEventQueue();

      state = container.read(statusControllerProvider);
      expect(state.snapshot?.status.host, 'details');
      expect(state.loading, isFalse);
    });

    test('does not ask for details when the map fails', () async {
      final source = FakeStatusSource(
        (_, _) => throw const StatusException(StatusFailureKind.network),
      );
      final container = await loadedContainer(source);

      expect(source.detailsCalls, isEmpty);

      final state = container.read(statusControllerProvider);
      expect(state.failure?.kind, StatusFailureKind.network);
      expect(state.loading, isFalse);
    });

    test('a failed details refresh keeps the map', () async {
      final source = FakeStatusSource((_, _) async => answerFrom('map'))
        ..respondDetails = (_, _) =>
            throw const StatusException(StatusFailureKind.server);
      final container = await loadedContainer(source);

      final state = container.read(statusControllerProvider);
      expect(state.snapshot?.status.host, 'map');
      expect(state.failure?.kind, StatusFailureKind.server);
      expect(state.isStale, isTrue);
      expect(state.loading, isFalse);
    });

    test('an API without the details endpoint keeps the map', () async {
      final source = FakeStatusSource((_, _) async => answerFrom('map'))
        ..respondDetails = (_, _) async => null;
      final container = await loadedContainer(source);

      final state = container.read(statusControllerProvider);
      expect(state.snapshot?.status.host, 'map');
      expect(state.failure, isNull);
      expect(state.loading, isFalse);
    });

    test('a details answer for the previous host is discarded', () async {
      final oldDetails = Completer<HostStatus?>();
      final source = FakeStatusSource((_, _) async => answerFrom('map'))
        ..respondDetails = (environment, _) => environment.url == production.url
            ? oldDetails.future
            : Future<HostStatus?>.value(answerFrom('new-details'));
      final container = await loadedContainer(source);

      await container
          .read(environmentsControllerProvider.notifier)
          .save(production.copyWith(url: movedUrl));
      await pumpEventQueue();
      expect(
        container.read(statusControllerProvider).snapshot?.status.host,
        'new-details',
      );

      oldDetails.complete(answerFrom('old-details'));
      await pumpEventQueue();

      final state = container.read(statusControllerProvider);
      expect(state.environment?.url, movedUrl);
      expect(state.snapshot?.status.host, 'new-details');
      expect(state.loading, isFalse);
    });

    test('renaming a host does not load again', () async {
      final source = FakeStatusSource();
      final container = await loadedContainer(source);

      await container
          .read(environmentsControllerProvider.notifier)
          .save(production.copyWith(name: 'renamed'));
      await pumpEventQueue();

      expect(source.calls, hasLength(1));
      expect(source.detailsCalls, hasLength(1));
    });
  });

  group('refresh settings', () {
    test('are off, every minute, until chosen', () async {
      final container = await loadedContainer(FakeStatusSource());

      expect(container.read(refreshSettingsProvider), const RefreshSettings());
      expect(const RefreshSettings().enabled, isFalse);
      expect(const RefreshSettings().interval, const Duration(minutes: 1));
      expect(autoRefreshIntervals, const <Duration>[
        Duration(minutes: 1),
        Duration(minutes: 5),
        Duration(minutes: 10),
        Duration(minutes: 15),
        Duration(minutes: 30),
      ]);
    });

    test('survive a restart', () async {
      final store = InMemoryRefreshSettingsStore();
      final first = await loadedContainer(
        FakeStatusSource(),
        refreshSettings: store,
      );

      await first
          .read(refreshSettingsProvider.notifier)
          .update(
            const RefreshSettings(
              enabled: true,
              interval: Duration(minutes: 15),
            ),
          );

      final second = await loadedContainer(
        FakeStatusSource(),
        refreshSettings: store,
      );

      second.listen(refreshSettingsProvider, (_, _) {});
      await pumpEventQueue();

      expect(
        second.read(refreshSettingsProvider),
        const RefreshSettings(enabled: true, interval: Duration(minutes: 15)),
      );
    });
  });
}
