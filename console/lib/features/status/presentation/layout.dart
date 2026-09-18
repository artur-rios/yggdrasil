/// Width breakpoints in logical pixels, decided from the space a widget gets
/// (`LayoutBuilder`, `MediaQuery.sizeOf`), never from the device type.
abstract final class Breakpoints {
  /// Below this, a phone: one column, details in a bottom sheet.
  static const double tablet = 600;

  /// From this up, a desktop: three columns.
  static const double desktop = 1200;

  /// Content never grows wider than this on very wide screens.
  static const double maxContentWidth = 1680;
}

/// How many columns of system cards fit [width].
int systemColumnsFor(double width) {
  if (width >= Breakpoints.desktop) {
    return 3;
  }

  if (width >= Breakpoints.tablet) {
    return 2;
  }

  return 1;
}
