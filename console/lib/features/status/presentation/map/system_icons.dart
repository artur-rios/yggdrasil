import 'package:flutter/material.dart';

/// The icon of a catalog `icon` name; [Icons.hub] for a name this build does
/// not know, or none.
///
/// A fixed mapping, not a lookup by name: Flutter strips the icons a build
/// does not reference.
IconData systemIcon(String? name) => switch (name) {
  'shield' => Icons.shield,
  'mail' => Icons.mail,
  'savings' => Icons.savings,
  'construction' => Icons.construction,
  'local_library' => Icons.local_library,
  'music_note' => Icons.music_note,
  'extension' => Icons.extension,
  'person' => Icons.person,
  'business' => Icons.business,
  'park' => Icons.park,
  'cloud' => Icons.cloud,
  _ => Icons.hub,
};
