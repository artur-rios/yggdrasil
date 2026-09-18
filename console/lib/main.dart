import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:flutter/widgets.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:http/http.dart' as http;

import 'app/console_app.dart';
import 'core/config/app_config.dart';
import 'features/environments/data/environment_store.dart';
import 'features/environments/presentation/environments_controller.dart';
import 'features/status/data/status_repository.dart';
import 'features/status/data/status_source.dart';
import 'features/status/presentation/status_controller.dart';

void main() {
  WidgetsFlutterBinding.ensureInitialized();

  final config = AppConfig.fromEnvironment();

  runApp(
    ProviderScope(
      overrides: <Override>[
        appConfigProvider.overrideWithValue(config),
        environmentStoreProvider.overrideWithValue(
          const PreferencesEnvironmentStore(),
        ),
        tokenStoreProvider.overrideWithValue(
          const SecureTokenStore(FlutterSecureStorage()),
        ),
        webOriginProvider.overrideWithValue(kIsWeb ? Uri.base.origin : null),
        statusRepositoryProvider.overrideWithValue(
          StatusRepository(
            RoutingStatusSource(
              http: HttpStatusSource(http.Client()),
              demo: DemoStatusSource(
                () => rootBundle.loadString('assets/demo/status.json'),
              ),
            ),
          ),
        ),
      ],
      child: const ConsoleApp(),
    ),
  );
}
