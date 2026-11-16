import 'package:flutter_test/flutter_test.dart';
import 'package:material_ui/material_ui.dart';
import 'package:yggdrasil_console/features/status/presentation/map/system_icons.dart';

void main() {
  group('systemIcon', () {
    test('given every catalog icon name, when looked up, then each has its '
        'own icon, none the neutral one', () {
      final icons = <IconData>{
        for (final name in <String>[
          'shield',
          'mail',
          'savings',
          'construction',
          'local_library',
          'music_note',
          'extension',
          'person',
          'business',
          'park',
          'cloud',
        ])
          systemIcon(name),
      };

      expect(icons, hasLength(11));
      expect(icons, isNot(contains(Icons.hub)));
      expect(systemIcon('shield'), Icons.shield);
    });

    test('given no name, an unknown one or a differently cased one, when '
        'looked up, then it is the neutral icon', () {
      expect(systemIcon(null), Icons.hub);
      expect(systemIcon('no_such_icon'), Icons.hub);
      expect(systemIcon(''), Icons.hub);
      expect(systemIcon('Shield'), Icons.hub);
    });
  });
}
