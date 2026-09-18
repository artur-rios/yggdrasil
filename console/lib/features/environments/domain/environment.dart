/// An environment the console can show: a name and the base URL of that
/// host's status API. The token is not part of it; it lives in secure storage
/// under [id] (see `TokenStore`).
class Environment {
  const Environment({
    required this.id,
    required this.name,
    required this.url,
    this.nameFromResponse = false,
  });

  factory Environment.fromJson(Map<String, dynamic> json) => Environment(
    id: json['id']! as String,
    name: json['name']! as String,
    url: json['url']! as String,
    nameFromResponse: json['nameFromResponse'] as bool? ?? false,
  );

  /// The built-in offline demo, which reads the bundled fixture instead of a
  /// status API.
  factory Environment.demo() =>
      const Environment(id: demoId, name: 'demo', url: demoUrl);

  static const String demoId = 'demo';
  static const String demoUrl = 'demo:';

  /// Stable across renames and URL edits, so the token stays attached.
  final String id;

  final String name;

  /// The status API's base URL, e.g. `https://yggdrasil.example.com`; the
  /// console requests `<url>/api/status`.
  final String url;

  /// Whether [name] is a placeholder to replace with the `environment` field
  /// of the first successful response.
  final bool nameFromResponse;

  bool get isDemo => url == demoUrl;

  /// [url] without trailing slashes, so paths concatenate predictably.
  String get baseUrl => normalizeBaseUrl(url);

  Environment copyWith({String? name, String? url, bool? nameFromResponse}) =>
      Environment(
        id: id,
        name: name ?? this.name,
        url: url ?? this.url,
        nameFromResponse: nameFromResponse ?? this.nameFromResponse,
      );

  Map<String, dynamic> toJson() => <String, dynamic>{
    'id': id,
    'name': name,
    'url': url,
    'nameFromResponse': nameFromResponse,
  };

  @override
  bool operator ==(Object other) =>
      other is Environment &&
      other.id == id &&
      other.name == name &&
      other.url == url &&
      other.nameFromResponse == nameFromResponse;

  @override
  int get hashCode => Object.hash(id, name, url, nameFromResponse);

  @override
  String toString() => 'Environment($id, $name, $url)';
}

String normalizeBaseUrl(String url) {
  var trimmed = url.trim();

  while (trimmed.endsWith('/')) {
    trimmed = trimmed.substring(0, trimmed.length - 1);
  }

  return trimmed;
}

/// Why [url] is not a usable status API base URL, or `null` when it is.
String? validateBaseUrl(String url) {
  final trimmed = normalizeBaseUrl(url);

  if (trimmed == Environment.demoUrl) {
    return null;
  }

  final uri = Uri.tryParse(trimmed);

  if (uri == null ||
      !(uri.scheme == 'https' || uri.scheme == 'http') ||
      uri.host.isEmpty) {
    return 'Enter an http(s) URL, e.g. https://yggdrasil.example.com';
  }

  if (uri.hasQuery || uri.hasFragment) {
    return 'The base URL takes no query or fragment';
  }

  return null;
}
