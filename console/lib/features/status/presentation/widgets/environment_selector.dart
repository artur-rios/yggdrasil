import 'package:material_ui/material_ui.dart';

import '../../domain/status.dart';
import 'status_visuals.dart';

/// Picks which environment of the host (development, production...) the
/// screen is about. Not the saved-host switcher: that one picks the host.
///
/// Each choice carries a small mark in its status's shape, so a problem in an
/// environment that is not selected still shows. A segmented control while
/// the choices fit, else a dropdown, as the host switcher does.
class EnvironmentSelector extends StatelessWidget {
  const EnvironmentSelector({
    super.key,
    required this.environments,
    required this.selectedId,
    required this.onSelected,
  });

  final List<HostEnvironment> environments;
  final String? selectedId;
  final ValueChanged<String> onSelected;

  @override
  Widget build(BuildContext context) {
    final brightness = Theme.of(context).brightness;

    // The mark names the status in words for screen readers; colour is never
    // the only signal.
    Widget mark(HostEnvironment environment) {
      final visual = StatusVisual.of(environment.status, brightness);

      return Icon(
        visual.icon,
        size: 18,
        color: visual.foreground,
        semanticLabel: visual.label,
      );
    }

    return LayoutBuilder(
      builder: (context, constraints) {
        final longest = environments
            .map((environment) => environment.name.length)
            .reduce((a, b) => a > b ? a : b);
        // Roughly what a segment needs: padding, the mark and ~9 px per
        // character.
        final segmentWidth = 64 + longest * 9.0;
        final fits =
            environments.length <= 4 &&
            segmentWidth * environments.length <= constraints.maxWidth;

        if (fits) {
          return Align(
            alignment: Alignment.centerLeft,
            child: SegmentedButton<String>(
              key: const ValueKey<String>('environment-selector'),
              showSelectedIcon: false,
              segments: <ButtonSegment<String>>[
                for (final environment in environments)
                  ButtonSegment<String>(
                    value: environment.id,
                    icon: mark(environment),
                    label: Text(environment.name),
                  ),
              ],
              selected: <String>{?selectedId},
              onSelectionChanged: (selection) => onSelected(selection.first),
            ),
          );
        }

        return DropdownButtonFormField<String>(
          key: const ValueKey<String>('environment-selector'),
          initialValue: selectedId,
          isExpanded: true,
          decoration: const InputDecoration(
            labelText: 'Environment',
            isDense: true,
          ),
          items: <DropdownMenuItem<String>>[
            for (final environment in environments)
              DropdownMenuItem<String>(
                value: environment.id,
                child: Row(
                  children: <Widget>[
                    mark(environment),
                    const SizedBox(width: 8),
                    Flexible(
                      child: Text(
                        environment.name,
                        overflow: TextOverflow.ellipsis,
                      ),
                    ),
                  ],
                ),
              ),
          ],
          onChanged: (id) {
            if (id != null) {
              onSelected(id);
            }
          },
        );
      },
    );
  }
}
