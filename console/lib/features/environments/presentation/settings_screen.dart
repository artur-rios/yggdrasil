import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../status/presentation/status_controller.dart';
import '../domain/environment.dart';
import 'environment_dialogs.dart';
import 'environments_controller.dart';

/// Add, edit and remove environments, and set their tokens.
class SettingsScreen extends ConsumerWidget {
  const SettingsScreen({super.key});

  static Future<void> addEnvironment(
    BuildContext context,
    WidgetRef ref,
  ) async {
    final edit = await showEnvironmentEditor(context);

    if (edit != null) {
      await ref
          .read(environmentsControllerProvider.notifier)
          .save(edit.environment, token: edit.token);
    }
  }

  Future<void> _edit(
    BuildContext context,
    WidgetRef ref,
    Environment environment,
    bool hasToken,
  ) async {
    final edit = await showEnvironmentEditor(
      context,
      existing: environment,
      hasToken: hasToken,
    );

    if (edit == null) {
      return;
    }

    if (edit.environment.baseUrl != environment.baseUrl) {
      // What was remembered came from another host.
      ref.read(statusRepositoryProvider).forget(environment.id);
    }

    await ref
        .read(environmentsControllerProvider.notifier)
        .save(edit.environment, token: edit.token);
  }

  Future<void> _remove(
    BuildContext context,
    WidgetRef ref,
    Environment environment,
  ) async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: Text('Remove ${environment.name}?'),
        content: const Text(
          'The environment and its saved token are removed from this device.',
        ),
        actions: <Widget>[
          TextButton(
            onPressed: () => Navigator.of(context).pop(false),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () => Navigator.of(context).pop(true),
            child: const Text('Remove'),
          ),
        ],
      ),
    );

    if (confirmed ?? false) {
      ref.read(statusRepositoryProvider).forget(environment.id);
      await ref
          .read(environmentsControllerProvider.notifier)
          .remove(environment.id);
    }
  }

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final environments = ref.watch(environmentsControllerProvider);

    return Scaffold(
      appBar: AppBar(title: const Text('Environments')),
      floatingActionButton: FloatingActionButton.extended(
        onPressed: () => addEnvironment(context, ref),
        icon: const Icon(Icons.add),
        label: const Text('Add'),
      ),
      body: switch (environments) {
        AsyncData<EnvironmentsState>(:final value) => Center(
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 720),
            child: ListView(
              padding: const EdgeInsets.only(bottom: 96),
              children: <Widget>[
                if (value.environments.isEmpty)
                  const ListTile(
                    title: Text('No environments'),
                    subtitle: Text(
                      'Add the base URL of a yggdrasil host, e.g. '
                      'https://yggdrasil.example.com.',
                    ),
                  ),
                for (final environment in value.environments)
                  _EnvironmentTile(
                    environment: environment,
                    hasToken: value.withToken.contains(environment.id),
                    onEdit: () => _edit(
                      context,
                      ref,
                      environment,
                      value.withToken.contains(environment.id),
                    ),
                    onRemove: () => _remove(context, ref, environment),
                  ),
                const Divider(),
                ListTile(
                  leading: const Icon(Icons.science_outlined),
                  title: const Text('Offline demo'),
                  subtitle: const Text(
                    'Sample data from the status API contract, no network.',
                  ),
                  onTap: () async {
                    await ref
                        .read(environmentsControllerProvider.notifier)
                        .addDemo();

                    if (context.mounted) {
                      Navigator.of(context).maybePop();
                    }
                  },
                ),
              ],
            ),
          ),
        ),
        AsyncError<EnvironmentsState>(:final error) => Center(
          child: Text('Could not load the environments: $error'),
        ),
        _ => const Center(child: CircularProgressIndicator()),
      },
    );
  }
}

class _EnvironmentTile extends StatelessWidget {
  const _EnvironmentTile({
    required this.environment,
    required this.hasToken,
    required this.onEdit,
    required this.onRemove,
  });

  final Environment environment;
  final bool hasToken;
  final VoidCallback onEdit;
  final VoidCallback onRemove;

  @override
  Widget build(BuildContext context) => ListTile(
    leading: Icon(environment.isDemo ? Icons.science_outlined : Icons.dns),
    title: Text(environment.name),
    subtitle: Text(
      environment.isDemo
          ? 'Offline demo'
          : '${environment.baseUrl}\n'
                '${hasToken ? 'Token saved' : 'No token'}',
    ),
    isThreeLine: !environment.isDemo,
    onTap: environment.isDemo ? null : onEdit,
    trailing: PopupMenuButton<String>(
      tooltip: 'Actions for ${environment.name}',
      onSelected: (action) => action == 'edit' ? onEdit() : onRemove(),
      itemBuilder: (context) => <PopupMenuEntry<String>>[
        if (!environment.isDemo)
          const PopupMenuItem<String>(value: 'edit', child: Text('Edit')),
        const PopupMenuItem<String>(value: 'remove', child: Text('Remove')),
      ],
    ),
  );
}
