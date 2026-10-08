import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;

import '../../environments/domain/environment.dart';
import '../domain/status.dart';

/// Why a status request failed, which is what decides how the console reacts.
enum StatusFailureKind {
  /// `401`: the token is missing or wrong. The console asks for it.
  unauthorized,

  /// The request never got an HTTP answer: offline, DNS, CORS, timeout.
  network,

  /// Any other non-2xx answer.
  server,

  /// A 2xx answer that is not the contract's JSON.
  invalidResponse,

  /// `503` with `Retry-After`: the status API has not finished its first
  /// refresh yet. Not an error; the console retries after [retryAfter].
  starting,
}

class StatusException implements Exception {
  const StatusException(
    this.kind, {
    this.statusCode,
    this.detail,
    this.retryAfter,
  });

  final StatusFailureKind kind;
  final int? statusCode;
  final String? detail;

  /// How long to wait before asking again, for [StatusFailureKind.starting].
  final Duration? retryAfter;

  /// Whether this is the API starting up rather than something wrong: the
  /// data on screen is not stale because of it.
  bool get isStarting => kind == StatusFailureKind.starting;

  /// One line for the user.
  String get message => switch (kind) {
    StatusFailureKind.unauthorized =>
      'The status API needs a valid token for this host.',
    StatusFailureKind.network => 'The status API could not be reached.',
    StatusFailureKind.server =>
      'The status API answered with HTTP ${statusCode ?? '?'}.',
    StatusFailureKind.invalidResponse =>
      'The status API sent a response this console cannot read.',
    StatusFailureKind.starting =>
      'The status API is starting and has no results yet.',
  };

  @override
  String toString() => 'StatusException($kind, $statusCode, $detail)';
}

/// Where a host's status comes from. ([Environment] is the saved
/// connection: a host's status API URL and token.)
abstract interface class StatusSource {
  /// Throws [StatusException] on failure.
  Future<HostStatus> fetch(Environment environment, String? token);
}

/// `GET <baseUrl>/api/status` with the bearer token.
class HttpStatusSource implements StatusSource {
  HttpStatusSource(this._client, {this.timeout = const Duration(seconds: 10)});

  final http.Client _client;
  final Duration timeout;

  @override
  Future<HostStatus> fetch(Environment environment, String? token) async {
    final uri = Uri.parse('${environment.baseUrl}/api/status');
    final http.Response response;

    try {
      response = await _client
          .get(
            uri,
            headers: <String, String>{
              'Accept': 'application/json',
              if (token != null && token.isNotEmpty)
                'Authorization': 'Bearer $token',
            },
          )
          .timeout(timeout);
    } on TimeoutException {
      throw const StatusException(StatusFailureKind.network, detail: 'timeout');
    } on Exception catch (error) {
      // http.ClientException on every platform (the browser reports offline,
      // DNS and CORS failures alike as one), SocketException on the VM.
      throw StatusException(StatusFailureKind.network, detail: '$error');
    }

    if (response.statusCode == 401) {
      throw const StatusException(
        StatusFailureKind.unauthorized,
        statusCode: 401,
      );
    }

    // The contract's "not ready yet" answer. A 503 without Retry-After (e.g.
    // Traefik with no healthy backend) is an ordinary server failure.
    final retryAfter = response.headers['retry-after'];

    if (response.statusCode == 503 && retryAfter != null) {
      throw StatusException(
        StatusFailureKind.starting,
        statusCode: 503,
        retryAfter: parseRetryAfter(retryAfter),
      );
    }

    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw StatusException(
        StatusFailureKind.server,
        statusCode: response.statusCode,
      );
    }

    return parseStatusBody(utf8.decode(response.bodyBytes));
  }
}

/// A `Retry-After` in seconds, kept between 1 s and 1 min; anything else
/// (an HTTP date, garbage) means the contract's 5 s.
Duration parseRetryAfter(String value) {
  final seconds = int.tryParse(value.trim());

  if (seconds == null) {
    return const Duration(seconds: 5);
  }

  return Duration(seconds: seconds.clamp(1, 60));
}

/// Parses a `GET /api/status` body, throwing
/// [StatusFailureKind.invalidResponse] for anything that is not the contract.
HostStatus parseStatusBody(String body) {
  try {
    final decoded = jsonDecode(body);

    if (decoded is! Map<String, dynamic>) {
      throw const FormatException('Expected a JSON object');
    }

    return HostStatus.fromJson(decoded);
  } on FormatException catch (error) {
    throw StatusException(
      StatusFailureKind.invalidResponse,
      detail: error.message,
    );
  }
}

/// The offline demo: the bundled fixture, with every timestamp moved so that
/// `generatedAt` is now and the relative times read naturally.
class DemoStatusSource implements StatusSource {
  DemoStatusSource(this._loadFixture, {DateTime Function()? now})
    : _now = now ?? DateTime.now;

  final Future<String> Function() _loadFixture;
  final DateTime Function() _now;

  @override
  Future<HostStatus> fetch(Environment environment, String? token) async {
    final json = jsonDecode(await _loadFixture()) as Map<String, dynamic>;
    final generatedAt = DateTime.parse(json['generatedAt']! as String);
    final shift = _now().toUtc().difference(generatedAt);

    return HostStatus.fromJson(
      _shiftDates(json, shift) as Map<String, dynamic>,
    );
  }

  static Object? _shiftDates(Object? value, Duration shift) {
    if (value is Map<String, dynamic>) {
      return <String, dynamic>{
        for (final entry in value.entries)
          entry.key: entry.key.endsWith('At') && entry.value is String
              ? DateTime.parse(entry.value as String)
                    .add(shift)
                    .toUtc()
                    .toIso8601String()
              : _shiftDates(entry.value, shift),
      };
    }

    if (value is List<dynamic>) {
      return <Object?>[
        for (final element in value) _shiftDates(element, shift),
      ];
    }

    return value;
  }
}

/// Sends the demo host to [demo] and every other one to [http].
class RoutingStatusSource implements StatusSource {
  const RoutingStatusSource({required this.http, required this.demo});

  final StatusSource http;
  final StatusSource demo;

  @override
  Future<HostStatus> fetch(Environment environment, String? token) =>
      environment.isDemo
      ? demo.fetch(environment, token)
      : http.fetch(environment, token);
}
