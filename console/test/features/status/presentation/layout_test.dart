import 'package:flutter_test/flutter_test.dart';
import 'package:yggdrasil_console/features/status/presentation/layout.dart';

void main() {
  group('mapColumnsFor', () {
    test('given a phone, a tablet and a computer width, when the map columns '
        'are chosen, then they are 3, 4 and 5', () {
      expect(mapColumnsFor(320), 3);
      expect(mapColumnsFor(599), 3);
      expect(mapColumnsFor(600), 4);
      expect(mapColumnsFor(1199), 4);
      expect(mapColumnsFor(1200), 5);
      expect(mapColumnsFor(4000), 5);
    });

    test('given a width right on a breakpoint, when the map columns are '
        'chosen, then the wider layout applies', () {
      expect(mapColumnsFor(Breakpoints.tablet), 4);
      expect(mapColumnsFor(Breakpoints.desktop), 5);
      expect(mapColumnsFor(Breakpoints.tablet - 0.5), 3);
    });

    test('given no width at all, when the map columns are chosen, then it is '
        'the phone layout', () {
      expect(mapColumnsFor(0), 3);
    });
  });
}
