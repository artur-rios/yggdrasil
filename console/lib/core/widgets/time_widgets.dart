import 'dart:async';

import 'package:flutter/material.dart';

import '../format/time_format.dart';

/// Rebuilds [builder] with the current time every [interval], so relative
/// times ("12 s ago") keep counting between refreshes.
class NowBuilder extends StatefulWidget {
  const NowBuilder({
    super.key,
    required this.builder,
    this.interval = const Duration(seconds: 1),
  });

  final Widget Function(BuildContext context, DateTime now) builder;
  final Duration interval;

  @override
  State<NowBuilder> createState() => _NowBuilderState();
}

class _NowBuilderState extends State<NowBuilder> {
  Timer? _timer;

  @override
  void initState() {
    super.initState();
    _timer = Timer.periodic(widget.interval, (_) {
      if (mounted) {
        setState(() {});
      }
    });
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => widget.builder(context, DateTime.now());
}

/// `prefix 3 h ago`, with the absolute time on hover (web) or long-press
/// (touch).
class RelativeTime extends StatelessWidget {
  const RelativeTime(
    this.moment, {
    super.key,
    this.prefix = '',
    this.style,
    this.now,
  });

  final DateTime moment;
  final String prefix;
  final TextStyle? style;

  /// The reference time; `DateTime.now()` when omitted.
  final DateTime? now;

  @override
  Widget build(BuildContext context) => Tooltip(
    message: formatAbsolute(moment),
    child: Text(
      '$prefix${formatAgo(moment, now ?? DateTime.now())}',
      style: style,
    ),
  );
}
