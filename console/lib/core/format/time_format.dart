/// Relative and absolute time, without pulling in `intl` for two formats.
library;

/// `12 s ago`, `3 min ago`, `5 h ago`, `2 d ago`.
///
/// A moment in the future (clock skew between the API host and this device)
/// reads as `just now` rather than a negative age.
String formatAgo(DateTime moment, DateTime now) {
  final age = now.difference(moment);

  if (age.inSeconds < 1) {
    return 'just now';
  }

  if (age.inSeconds < 60) {
    return '${age.inSeconds} s ago';
  }

  if (age.inMinutes < 60) {
    return '${age.inMinutes} min ago';
  }

  if (age.inHours < 48) {
    return '${age.inHours} h ago';
  }

  return '${age.inDays} d ago';
}

/// `2026-09-18 15:04:11 (UTC-03:00)` in the device's time zone, so the value
/// on screen matches the clock on the wall and still says which zone it is.
String formatAbsolute(DateTime moment) {
  final local = moment.toLocal();
  final offset = local.timeZoneOffset;
  final sign = offset.isNegative ? '-' : '+';
  final minutes = offset.inMinutes.abs();

  return '${local.year}-${_two(local.month)}-${_two(local.day)} '
      '${_two(local.hour)}:${_two(local.minute)}:${_two(local.second)} '
      '(UTC$sign${_two(minutes ~/ 60)}:${_two(minutes % 60)})';
}

/// `12 ms` or `2.4 s`.
String formatLatency(int milliseconds) => milliseconds < 1000
    ? '$milliseconds ms'
    : '${(milliseconds / 1000).toStringAsFixed(1)} s';

String _two(int value) => value.toString().padLeft(2, '0');
