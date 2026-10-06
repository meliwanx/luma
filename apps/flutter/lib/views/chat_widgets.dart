import 'package:flutter/material.dart';

import '../theme.dart';

typedef ChatWidgetEvent = Future<void> Function(
  String widgetId,
  String action,
  Object? value,
);

class ChatWidgetView extends StatefulWidget {
  const ChatWidgetView({
    super.key,
    required this.widget,
    required this.onEvent,
    required this.disabled,
  });

  final Map<String, dynamic> widget;
  final ChatWidgetEvent onEvent;
  final bool disabled;

  @override
  State<ChatWidgetView> createState() => _ChatWidgetViewState();
}

class _ChatWidgetViewState extends State<ChatWidgetView> {
  bool _busy = false;
  late Set<String> _selected;
  late Map<String, String> _values;

  Map<String, dynamic> get _spec => _map(widget.widget['spec']);
  Map<String, dynamic> get _state => _map(widget.widget['state']);
  String get _id => '${widget.widget['id'] ?? ''}';
  bool get _submitted => _state['status'] == 'submitted';
  bool get _locked => widget.disabled || _busy || _submitted;
  bool get _allowAlways =>
      _spec['allow_always'] == true || widget.widget['allow_always'] == true;

  @override
  void initState() {
    super.initState();
    _syncState();
  }

  @override
  void didUpdateWidget(covariant ChatWidgetView oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.widget != widget.widget) _syncState();
  }

  void _syncState() {
    final state = _state;
    _selected = (state['selected'] is List ? state['selected'] as List : [])
        .map((value) => '$value')
        .toSet();
    _values = _map(state['values'])
        .map((key, value) => MapEntry(key, '$value'));
  }

  Map<String, dynamic> _map(dynamic value) =>
      value is Map ? Map<String, dynamic>.from(value) : <String, dynamic>{};

  List<Map<String, dynamic>> _items(dynamic value) => value is List
      ? value.whereType<Map>().map((item) => _map(item)).toList()
      : [];

  Future<void> _event(String action, Object? value) async {
    if (_busy || widget.disabled) return;
    setState(() => _busy = true);
    try {
      await widget.onEvent(_id, action, value);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final type = '${widget.widget['type'] ?? _spec['type'] ?? ''}';
    final title = _spec['title'];
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: context.muse.chip,
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: context.muse.line),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        mainAxisSize: MainAxisSize.min,
        children: [
          if (title is String && title.isNotEmpty) ...[
            Text(
              title,
              style: TextStyle(
                color: context.muse.text,
                fontSize: 14,
                fontWeight: FontWeight.w600,
              ),
            ),
            const SizedBox(height: 10),
          ],
          switch (type) {
            'choice' => _choice(context),
            'form' => _form(context),
            'checklist' => _checklist(context),
            'cards' => _cards(context),
            'confirm' => _confirm(context),
            _ => Text(
              '${widget.widget['fallback'] ?? _spec['fallback'] ?? ''}',
              style: TextStyle(color: context.muse.muted),
            ),
          },
        ],
      ),
    );
  }

  Widget _confirm(BuildContext context) {
    final status = '${_state['status'] ?? 'pending'}';
    final pending = status == 'pending';
    final details = _items(_spec['details']);
    final statusText = switch (status) {
      'running' => '正在执行…',
      'done' => '已执行',
      'failed' => '执行失败',
      'cancelled' => '已取消',
      _ => '',
    };
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        if (_spec['body'] is String && (_spec['body'] as String).isNotEmpty)
          Padding(
            padding: const EdgeInsets.only(bottom: 10),
            child: Text(
              '${_spec['body']}',
              style: TextStyle(color: context.muse.muted, height: 1.45),
            ),
          ),
        for (final detail in details)
          Padding(
            padding: const EdgeInsets.only(bottom: 5),
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                SizedBox(
                  width: 108,
                  child: Text(
                    '${detail['label'] ?? ''}',
                    style: TextStyle(color: context.muse.muted, fontSize: 12),
                  ),
                ),
                Expanded(
                  child: Text(
                    '${detail['value'] ?? ''}',
                    style: TextStyle(color: context.muse.text, fontSize: 12),
                  ),
                ),
              ],
            ),
          ),
        if (statusText.isNotEmpty)
          Padding(
            padding: const EdgeInsets.only(top: 4),
            child: Text(
              statusText,
              style: TextStyle(
                color: status == 'failed'
                    ? context.muse.danger
                    : context.muse.muted,
                fontSize: 13,
              ),
            ),
          ),
        if (pending) ...[
          const SizedBox(height: 10),
          Wrap(
            spacing: 8,
            children: [
              FilledButton(
                onPressed: _locked ? null : () => _event('confirm', null),
                child: Text('${_spec['confirm_label'] ?? '确认执行'}'),
              ),
              if (_allowAlways)
                OutlinedButton(
                  onPressed: _locked
                      ? null
                      : () => _event('confirm', {'remember': true}),
                  child: const Text('始终允许'),
                ),
              OutlinedButton(
                onPressed: _locked ? null : () => _event('cancel', null),
                child: Text('${_spec['cancel_label'] ?? '取消'}'),
              ),
            ],
          ),
        ],
      ],
    );
  }

  Widget _pill(
    BuildContext context,
    String label, {
    required bool active,
    required VoidCallback? onPressed,
  }) => OutlinedButton(
    onPressed: onPressed,
    style: OutlinedButton.styleFrom(
      foregroundColor: active ? context.muse.accent : context.muse.text,
      disabledForegroundColor: active
          ? context.muse.accent
          : context.muse.muted,
      backgroundColor: active
          ? context.muse.accent.withValues(alpha: 0.1)
          : context.muse.bg,
      side: BorderSide(color: active ? context.muse.accent : context.muse.line),
      shape: const StadiumBorder(),
      minimumSize: const Size(0, MuseMetrics.pillHeight),
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 6),
    ),
    child: Text(label),
  );

  Widget _choice(BuildContext context) {
    final multiple = _spec['multiple'] == true;
    final options = _items(_spec['options']);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Wrap(
          spacing: 8,
          runSpacing: 8,
          children: [
            for (final option in options)
              _pill(
                context,
                '${option['label'] ?? ''}',
                active: _selected.contains('${option['id']}'),
                onPressed: _locked
                    ? null
                    : () {
                        final optionId = '${option['id']}';
                        if (multiple) {
                          setState(() {
                            if (!_selected.add(optionId)) {
                              _selected.remove(optionId);
                            }
                          });
                        } else {
                          _event('submit', [optionId]);
                        }
                      },
              ),
          ],
        ),
        if (multiple && !_submitted) ...[
          const SizedBox(height: 10),
          _pill(
            context,
            '${_spec['submit_label'] ?? '确定'}',
            active: true,
            onPressed: _locked || _selected.isEmpty
                ? null
                : () => _event('submit', _selected.toList()),
          ),
        ],
      ],
    );
  }

  Widget _form(BuildContext context) {
    final fields = _items(_spec['fields']);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        for (final field in fields) ...[
          _formField(context, field),
          const SizedBox(height: 10),
        ],
        if (!_submitted)
          _pill(
            context,
            '${_spec['submit_label'] ?? '提交'}',
            active: true,
            onPressed: _locked ? null : _submitForm,
          ),
      ],
    );
  }

  Widget _formField(BuildContext context, Map<String, dynamic> field) {
    final id = '${field['id']}';
    final kind = '${field['kind'] ?? 'text'}';
    final label =
        '${field['label'] ?? ''}${field['required'] == true ? ' *' : ''}';
    final options = field['options'] is List
        ? (field['options'] as List).map((item) => '$item').toList()
        : <String>[];
    if (kind == 'select') {
      return DropdownButtonFormField<String>(
        key: ValueKey('$_id:$id:$_submitted'),
        initialValue: options.contains(_values[id]) ? _values[id] : null,
        decoration: InputDecoration(labelText: label, isDense: true),
        items: [
          for (final option in options)
            DropdownMenuItem(value: option, child: Text(option)),
        ],
        onChanged: _locked
            ? null
            : (value) => setState(() => _values[id] = value ?? ''),
      );
    }
    return TextFormField(
      // Date values are set by the picker, so rebuild the field when they change.
      key: ValueKey(
        '$_id:$id:$_submitted:${kind == 'date' ? _values[id] : ''}',
      ),
      initialValue: _values[id] ?? '',
      enabled: !_locked,
      readOnly: kind == 'date',
      minLines: kind == 'textarea' ? 3 : 1,
      maxLines: kind == 'textarea' ? 5 : 1,
      keyboardType: kind == 'number'
          ? TextInputType.number
          : TextInputType.text,
      decoration: InputDecoration(
        labelText: label,
        hintText: '${field['placeholder'] ?? ''}',
        isDense: true,
      ),
      onChanged: (value) => _values[id] = value,
      onTap: kind == 'date' && !_locked
          ? () async {
              final picked = await showDatePicker(
                context: context,
                firstDate: DateTime(1900),
                lastDate: DateTime(2100),
                initialDate:
                    DateTime.tryParse(_values[id] ?? '') ?? DateTime.now(),
              );
              if (picked != null && mounted) {
                setState(() {
                  _values[id] =
                      '${picked.year.toString().padLeft(4, '0')}-${picked.month.toString().padLeft(2, '0')}-${picked.day.toString().padLeft(2, '0')}';
                });
              }
            }
          : null,
    );
  }

  void _submitForm() {
    for (final field in _items(_spec['fields'])) {
      final id = '${field['id']}';
      final value = (_values[id] ?? '').trim();
      if (field['required'] == true && value.isEmpty) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('请填写${field['label'] ?? '必填项'}')),
        );
        return;
      }
    }
    _event('submit', _values);
  }

  Widget _checklist(BuildContext context) {
    final done = _map(_state['done']);
    final tasksCreated = _state['tasks_created'] == true;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        for (final item in _items(_spec['items']))
          CheckboxListTile(
            value:
                done['${item['id']}'] == true ||
                (done['${item['id']}'] == null && item['done'] == true),
            onChanged: widget.disabled || _busy
                ? null
                : (value) => _event('toggle', {
                    'item_id': item['id'],
                    'done': value == true,
                  }),
            dense: true,
            controlAffinity: ListTileControlAffinity.leading,
            contentPadding: EdgeInsets.zero,
            activeColor: context.muse.accent,
            title: Text('${item['label'] ?? ''}'),
            subtitle:
                item['note'] is String && (item['note'] as String).isNotEmpty
                ? Text('${item['note']}')
                : null,
          ),
        if (_spec['allow_create_tasks'] != false)
          _pill(
            context,
            tasksCreated ? '已转为任务' : '转为任务',
            active: tasksCreated,
            onPressed: tasksCreated || widget.disabled || _busy
                ? null
                : _confirmCreateTasks,
          ),
      ],
    );
  }

  Future<void> _confirmCreateTasks() async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: const Text('转为任务'),
        content: const Text('将清单中未完成的项目创建为任务？'),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialogContext, false),
            child: const Text('取消'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(dialogContext, true),
            child: const Text('确认创建'),
          ),
        ],
      ),
    );
    if (confirmed == true && mounted && !widget.disabled) {
      await _event('create_tasks', null);
    }
  }

  Widget _cards(BuildContext context) {
    final selectable = _spec['selectable'] == true;
    return Column(
      children: [
        for (final item in _items(_spec['items']))
          Padding(
            padding: const EdgeInsets.only(bottom: 8),
            child: InkWell(
              borderRadius: BorderRadius.circular(12),
              onTap: !selectable || _locked
                  ? null
                  : () => _event('select', '${item['id']}'),
              child: Container(
                width: double.infinity,
                padding: const EdgeInsets.all(12),
                decoration: BoxDecoration(
                  color: _selected.contains('${item['id']}')
                      ? context.muse.accent.withValues(alpha: 0.1)
                      : context.muse.bg,
                  borderRadius: BorderRadius.circular(12),
                  border: Border.all(
                    color: _selected.contains('${item['id']}')
                        ? context.muse.accent
                        : context.muse.line,
                  ),
                ),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      '${item['title'] ?? ''}',
                      style: TextStyle(
                        color: context.muse.text,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                    for (final key in ['subtitle', 'body', 'tag'])
                      if (item[key] is String &&
                          (item[key] as String).isNotEmpty)
                        Padding(
                          padding: const EdgeInsets.only(top: 4),
                          child: Text(
                            '${item[key]}',
                            style: TextStyle(
                              color: context.muse.muted,
                              fontSize: key == 'tag' ? 11 : 13,
                            ),
                          ),
                        ),
                  ],
                ),
              ),
            ),
          ),
      ],
    );
  }
}
