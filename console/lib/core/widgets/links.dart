import 'package:flutter/material.dart';
import 'package:url_launcher/url_launcher.dart';

/// Opens [url] in the browser (a new tab on the web), telling the user when it
/// cannot be opened.
Future<void> openLink(BuildContext context, String url) async {
  final uri = Uri.tryParse(url);
  final messenger = ScaffoldMessenger.maybeOf(context);
  var opened = false;

  if (uri != null && (uri.scheme == 'https' || uri.scheme == 'http')) {
    try {
      opened = await launchUrl(
        uri,
        mode: LaunchMode.externalApplication,
        webOnlyWindowName: '_blank',
      );
    } on Object {
      opened = false;
    }
  }

  if (!opened) {
    messenger?.showSnackBar(SnackBar(content: Text('Could not open $url')));
  }
}

/// A compact text button that opens a link.
class LinkButton extends StatelessWidget {
  const LinkButton({
    super.key,
    required this.icon,
    required this.label,
    required this.url,
  });

  final IconData icon;
  final String label;
  final String url;

  @override
  Widget build(BuildContext context) => Tooltip(
    message: url,
    child: TextButton.icon(
      style: TextButton.styleFrom(
        visualDensity: VisualDensity.compact,
        padding: const EdgeInsets.symmetric(horizontal: 8),
        minimumSize: const Size(48, 36),
      ),
      onPressed: () => openLink(context, url),
      icon: Icon(icon, size: 18),
      label: Text(label),
    ),
  );
}
