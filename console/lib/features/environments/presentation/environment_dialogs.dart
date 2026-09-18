import 'package:flutter/material.dart';

import '../domain/environment.dart';
import 'environments_controller.dart';

/// Asks for [environment]'s token. Returns it, or `null` when cancelled.
Future<String?> showTokenDialog(
  BuildContext context,
  Environment environment, {
  bool rejected = false,
}) => showDialog<String>(
  context: context,
  builder: (context) =>
      _TokenDialog(environment: environment, rejected: rejected),
);

class _TokenDialog extends StatefulWidget {
  const _TokenDialog({required this.environment, required this.rejected});

  final Environment environment;
  final bool rejected;

  @override
  State<_TokenDialog> createState() => _TokenDialogState();
}

class _TokenDialogState extends State<_TokenDialog> {
  final TextEditingController _token = TextEditingController();
  bool _obscure = true;

  @override
  void dispose() {
    _token.dispose();
    super.dispose();
  }

  void _submit() {
    final value = _token.text.trim();

    if (value.isNotEmpty) {
      Navigator.of(context).pop(value);
    }
  }

  @override
  Widget build(BuildContext context) => AlertDialog(
    title: Text('Token for ${widget.environment.name}'),
    scrollable: true,
    content: Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.start,
      children: <Widget>[
        Text(
          widget.rejected
              ? 'The status API at ${widget.environment.baseUrl} rejected the '
                    'request (401). Enter the YGGDRASIL_STATUS_TOKEN of that '
                    'host.'
              : 'The YGGDRASIL_STATUS_TOKEN of '
                    '${widget.environment.baseUrl}.',
        ),
        const SizedBox(height: 16),
        TextField(
          key: const ValueKey<String>('token-field'),
          controller: _token,
          autofocus: true,
          obscureText: _obscure,
          autocorrect: false,
          enableSuggestions: false,
          decoration: InputDecoration(
            labelText: 'Token',
            suffixIcon: IconButton(
              tooltip: _obscure ? 'Show token' : 'Hide token',
              onPressed: () => setState(() => _obscure = !_obscure),
              icon: Icon(_obscure ? Icons.visibility : Icons.visibility_off),
            ),
          ),
          onSubmitted: (_) => _submit(),
        ),
      ],
    ),
    actions: <Widget>[
      TextButton(
        onPressed: () => Navigator.of(context).pop(),
        child: const Text('Cancel'),
      ),
      FilledButton(onPressed: _submit, child: const Text('Save')),
    ],
  );
}

/// The result of the environment editor.
class EnvironmentEdit {
  const EnvironmentEdit(this.environment, this.token);

  final Environment environment;

  /// `null` keeps the saved token; empty removes it.
  final String? token;
}

/// Adds an environment ([existing] `null`) or edits one.
Future<EnvironmentEdit?> showEnvironmentEditor(
  BuildContext context, {
  Environment? existing,
  bool hasToken = false,
}) => showDialog<EnvironmentEdit>(
  context: context,
  builder: (context) =>
      _EnvironmentEditor(existing: existing, hasToken: hasToken),
);

class _EnvironmentEditor extends StatefulWidget {
  const _EnvironmentEditor({required this.existing, required this.hasToken});

  final Environment? existing;
  final bool hasToken;

  @override
  State<_EnvironmentEditor> createState() => _EnvironmentEditorState();
}

class _EnvironmentEditorState extends State<_EnvironmentEditor> {
  final GlobalKey<FormState> _form = GlobalKey<FormState>();
  late final TextEditingController _name = TextEditingController(
    text: widget.existing?.name,
  );
  late final TextEditingController _url = TextEditingController(
    text: widget.existing?.url,
  );
  final TextEditingController _token = TextEditingController();
  bool _forgetToken = false;
  bool _obscure = true;

  @override
  void dispose() {
    _name.dispose();
    _url.dispose();
    _token.dispose();
    super.dispose();
  }

  void _submit() {
    if (!(_form.currentState?.validate() ?? false)) {
      return;
    }

    final existing = widget.existing;
    final name = _name.text.trim();
    final url = normalizeBaseUrl(_url.text);
    final environment = existing == null
        ? Environment(id: newEnvironmentId(), name: name, url: url)
        : existing.copyWith(
            name: name,
            url: url,
            // A name the user typed is theirs to keep.
            nameFromResponse:
                existing.nameFromResponse && name == existing.name,
          );
    final token = _token.text.trim();

    Navigator.of(context).pop(
      EnvironmentEdit(
        environment,
        token.isNotEmpty ? token : (_forgetToken ? '' : null),
      ),
    );
  }

  @override
  Widget build(BuildContext context) => AlertDialog(
    title: Text(
      widget.existing == null ? 'Add environment' : 'Edit environment',
    ),
    scrollable: true,
    content: SizedBox(
      width: 420,
      child: Form(
        key: _form,
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: <Widget>[
            TextFormField(
              key: const ValueKey<String>('environment-name'),
              controller: _name,
              autofocus: widget.existing == null,
              decoration: const InputDecoration(
                labelText: 'Name',
                hintText: 'production',
              ),
              validator: (value) =>
                  (value ?? '').trim().isEmpty ? 'Enter a name' : null,
            ),
            const SizedBox(height: 16),
            TextFormField(
              key: const ValueKey<String>('environment-url'),
              controller: _url,
              keyboardType: TextInputType.url,
              autocorrect: false,
              decoration: const InputDecoration(
                labelText: 'Base URL',
                hintText: 'https://yggdrasil.example.com',
                helperText: 'The console requests <base URL>/api/status',
                helperMaxLines: 2,
              ),
              validator: (value) => validateBaseUrl(value ?? ''),
            ),
            const SizedBox(height: 16),
            TextFormField(
              key: const ValueKey<String>('environment-token'),
              controller: _token,
              obscureText: _obscure,
              autocorrect: false,
              enableSuggestions: false,
              decoration: InputDecoration(
                labelText: 'Token',
                helperText: widget.hasToken
                    ? 'A token is saved. Leave empty to keep it.'
                    : 'Stored in secure storage on this device.',
                helperMaxLines: 2,
                suffixIcon: IconButton(
                  tooltip: _obscure ? 'Show token' : 'Hide token',
                  onPressed: () => setState(() => _obscure = !_obscure),
                  icon: Icon(
                    _obscure ? Icons.visibility : Icons.visibility_off,
                  ),
                ),
              ),
            ),
            if (widget.hasToken)
              CheckboxListTile(
                contentPadding: EdgeInsets.zero,
                value: _forgetToken,
                onChanged: (value) =>
                    setState(() => _forgetToken = value ?? false),
                title: const Text('Forget the saved token'),
              ),
          ],
        ),
      ),
    ),
    actions: <Widget>[
      TextButton(
        onPressed: () => Navigator.of(context).pop(),
        child: const Text('Cancel'),
      ),
      FilledButton(onPressed: _submit, child: const Text('Save')),
    ],
  );
}
