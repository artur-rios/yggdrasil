import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_riverpod/misc.dart' show Override;
import 'package:flutter_test/flutter_test.dart';
import 'package:yggdrasil_console/features/environments/data/environment_store.dart';
import 'package:yggdrasil_console/features/environments/domain/environment.dart';
import 'package:yggdrasil_console/features/environments/presentation/environments_controller.dart';
import 'package:yggdrasil_console/features/status/data/status_repository.dart';
import 'package:yggdrasil_console/features/status/data/status_source.dart';
import 'package:yggdrasil_console/features/status/domain/status.dart';
import 'package:yggdrasil_console/features/status/presentation/status_controller.dart';

import '../../../helpers.dart';

EnvironmentStatus answerFrom(String host) => EnvironmentStatus(
  environment: host,
  generatedAt: null,
  status: Status.up,
  systems: const <SystemStatus>[],
);

void main() {
  const movedUrl = 'https://yggdrasil.new.example.com';

  // production's first request, to its old URL, is held until the test lets
  // it answer; requests to the new URL answer at once (or fail).
  late Completer<EnvironmentStatus> oldHost;
  late FakeStatusSource source;
  late ProviderContainer container;

  setUp(() async {
    oldHost = Completer<EnvironmentStatus>();
    source = FakeStatusSource(
      (environment, _) => environment.url == production.url
          ? oldHost.future
          : Future<EnvironmentStatus>.value(answerFrom('new')),
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
        container.read(statusControllerProvider).snapshot?.status.environment,
        'new',
      );

      oldHost.complete(answerFrom('old'));
      await pumpEventQueue();

      final state = container.read(statusControllerProvider);
      expect(state.environment?.url, movedUrl);
      expect(state.snapshot?.status.environment, 'new');
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
      expect(state.snapshot?.status.environment, 'new');
    },
  );
}
