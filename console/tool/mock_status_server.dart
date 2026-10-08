// A stand-in for the status API, for running the console without one.
//
//     dart run tool/mock_status_server.dart [--port 8090] [--token <token>]
//         [--fixture assets/demo/status.json] [--web build/web]
//         [--starting-seconds 0]
//
// Serves GET /api/status and GET /api/systems/{id} from the fixture, as
// docs/status-api.md defines them: bearer token (401 with an empty body and
// WWW-Authenticate otherwise), CORS for any origin, `generatedAt` moved to now
// on every request, and for the first --starting-seconds a 503 with
// Retry-After: 5, like a status API that has not finished its first refresh.
// With --web it also serves a `flutter build web` output with an SPA
// fallback, so the console and the API share one origin as behind Traefik.
//
// The default fixture is the v2 demo (one host, three environments); pass
// `--fixture test/fixtures/v1_example.json` to see how the console shows a
// host whose status API still answers with the v1 contract.
//
// Development only: nothing here is hardened.

import 'dart:convert';
import 'dart:io';
import 'dart:math';

Future<void> main(List<String> arguments) async {
  final options = _parse(arguments);
  final port = int.parse(options['port'] ?? '8090');
  final token = options['token'] ?? 'mock-status-token-0123456789abcdef';
  final fixture = File(options['fixture'] ?? 'assets/demo/status.json');
  final web = options['web'] == null ? null : Directory(options['web']!);
  final startingFor = Duration(
    seconds: int.parse(options['starting-seconds'] ?? '0'),
  );
  final started = DateTime.now();
  final random = Random();

  final server = await HttpServer.bind(InternetAddress.loopbackIPv4, port);
  stdout
    ..writeln('Mock status API on http://localhost:$port')
    ..writeln('  token:   $token')
    ..writeln('  fixture: ${fixture.path}')
    ..writeln('  web:     ${web?.path ?? '(not served)'}');

  await for (final request in server) {
    final response = request.response;
    final path = request.uri.path;
    final origin = request.headers.value('origin');

    if (origin != null) {
      response.headers
        ..set('Access-Control-Allow-Origin', origin)
        ..set('Access-Control-Allow-Headers', 'Authorization, Accept')
        ..set('Access-Control-Allow-Methods', 'GET, OPTIONS')
        ..set('Vary', 'Origin');
    }

    if (request.method == 'OPTIONS') {
      response.statusCode = HttpStatus.noContent;
      await response.close();
      continue;
    }

    stdout.writeln('${request.method} $path');

    if (path == '/healthz') {
      response.write('ok');
    } else if (path.startsWith('/api/')) {
      if (request.headers.value('authorization') != 'Bearer $token') {
        response
          ..statusCode = HttpStatus.unauthorized
          ..headers.set('WWW-Authenticate', 'Bearer');
      } else if (DateTime.now().difference(started) < startingFor) {
        response
          ..statusCode = HttpStatus.serviceUnavailable
          ..headers.set('Retry-After', '5');
      } else {
        final status = _fresh(fixture.readAsStringSync(), random);

        if (path == '/api/status') {
          _json(response, status);
        } else if (path.startsWith('/api/systems/')) {
          final id = path.substring('/api/systems/'.length);
          final systems = (status['systems'] as List<dynamic>)
              .cast<Map<String, dynamic>>()
              .where((system) => system['id'] == id);

          if (systems.isEmpty) {
            response.statusCode = HttpStatus.notFound;
          } else {
            _json(response, systems.single);
          }
        } else {
          response.statusCode = HttpStatus.notFound;
        }
      }
    } else if (web != null) {
      await _static(response, web, path);
    } else {
      response.statusCode = HttpStatus.notFound;
    }

    await response.close();
  }
}

/// The fixture with every timestamp moved so `generatedAt` is now, and a
/// little jitter on healthy latencies so each refresh visibly differs.
Map<String, dynamic> _fresh(String raw, Random random) {
  final json = jsonDecode(raw) as Map<String, dynamic>;
  final shift = DateTime.now().toUtc().difference(
    DateTime.parse(json['generatedAt'] as String),
  );

  Object? walk(Object? value) {
    if (value is Map<String, dynamic>) {
      return <String, dynamic>{
        for (final entry in value.entries)
          entry.key: switch (entry) {
            MapEntry(:final key, value: final String text)
                when key.endsWith('At') =>
              DateTime.parse(text).add(shift).toUtc().toIso8601String(),
            MapEntry(key: 'latencyMs', value: final int ms) when ms < 100 =>
              max(1, ms + random.nextInt(5) - 2),
            _ => walk(entry.value),
          },
      };
    }

    if (value is List<dynamic>) {
      return value.map(walk).toList();
    }

    return value;
  }

  return walk(json)! as Map<String, dynamic>;
}

void _json(HttpResponse response, Object body) {
  response.headers
    ..contentType = ContentType.json
    ..set('Cache-Control', 'no-store');
  response.write(jsonEncode(body));
}

const Map<String, String> _types = <String, String>{
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript',
  '.mjs': 'text/javascript',
  '.json': 'application/json',
  '.wasm': 'application/wasm',
  '.png': 'image/png',
  '.otf': 'font/otf',
  '.ttf': 'font/ttf',
  '.frag': 'application/octet-stream',
  '.bin': 'application/octet-stream',
};

Future<void> _static(HttpResponse response, Directory root, String path) async {
  final relative = path == '/' ? 'index.html' : path.substring(1);
  final segments = relative.split('/');

  if (segments.contains('..')) {
    response.statusCode = HttpStatus.forbidden;

    return;
  }

  var file = File('${root.path}/$relative');

  if (!file.existsSync()) {
    // SPA fallback, as nginx's try_files does in the image.
    file = File('${root.path}/index.html');
  }

  final dot = file.path.lastIndexOf('.');
  final type = _types[dot < 0 ? '' : file.path.substring(dot)];

  if (type != null) {
    response.headers.set('Content-Type', type);
  }

  response.headers.set('Cache-Control', 'no-cache');
  await response.addStream(file.openRead());
}

Map<String, String> _parse(List<String> arguments) {
  final options = <String, String>{};

  for (var i = 0; i < arguments.length; i++) {
    final argument = arguments[i];

    if (!argument.startsWith('--') || i + 1 >= arguments.length) {
      stderr.writeln('Unexpected argument: $argument');
      exit(64);
    }

    options[argument.substring(2)] = arguments[++i];
  }

  return options;
}
