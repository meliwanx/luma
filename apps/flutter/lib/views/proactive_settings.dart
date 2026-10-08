import 'package:flutter/material.dart';

import '../api.dart';
import '../brand.dart';
import '../theme.dart';

class ProactiveSettingsView extends StatefulWidget {
  const ProactiveSettingsView({super.key, required this.api});

  final AssistantApi api;

  @override
  State<ProactiveSettingsView> createState() => _ProactiveSettingsViewState();
}

class _ProactiveSettingsViewState extends State<ProactiveSettingsView> {
  final _topicsLike = TextEditingController();
  final _topicsAvoid = TextEditingController();
  final _style = TextEditingController();
  final _feedInstructions = TextEditingController();
  bool _enabled = true;
  int _maxPerDay = 2;
  TimeOfDay _windowStart = const TimeOfDay(hour: 9, minute: 0);
  TimeOfDay _windowEnd = const TimeOfDay(hour: 21, minute: 30);
  String _timezone = 'Asia/Shanghai';
  bool _feedEnabled = true;
  int _feedPerDay = 1;
  bool _loading = true;
  bool _saving = false;
  bool _supported = true;
  String? _error;

  static const _timezones = [
    'Asia/Shanghai',
    'Asia/Hong_Kong',
    'Asia/Tokyo',
    'Asia/Singapore',
    'Europe/London',
    'Europe/Paris',
    'America/New_York',
    'America/Los_Angeles',
    'UTC',
  ];

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void dispose() {
    _topicsLike.dispose();
    _topicsAvoid.dispose();
    _style.dispose();
    _feedInstructions.dispose();
    super.dispose();
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final prefs = await widget.api.proactivePrefs();
      if (!mounted) return;
      setState(() {
        _supported = prefs.isNotEmpty;
        _apply(prefs);
      });
    } catch (error) {
      if (!mounted) return;
      setState(() => _error = _message(error, '主动消息设置加载失败'));
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  void _apply(Map<String, dynamic> prefs) {
    _enabled = prefs['enabled'] is bool ? prefs['enabled'] as bool : true;
    _maxPerDay = _count(prefs['max_per_day'], 2, 5);
    _windowStart = _time(prefs['window_start'], _windowStart);
    _windowEnd = _time(prefs['window_end'], _windowEnd);
    if (prefs['timezone'] is String &&
        (prefs['timezone'] as String).isNotEmpty) {
      _timezone = prefs['timezone'] as String;
    }
    _topicsLike.text = '${prefs['topics_like'] ?? ''}';
    _topicsAvoid.text = '${prefs['topics_avoid'] ?? ''}';
    _style.text = '${prefs['style'] ?? ''}';
    _feedEnabled = prefs['feed_enabled'] is bool
        ? prefs['feed_enabled'] as bool
        : true;
    _feedPerDay = _count(prefs['feed_per_day'], 1, 3);
    _feedInstructions.text = '${prefs['feed_instructions'] ?? ''}';
  }

  int _count(dynamic value, int fallback, int maximum) =>
      value is num ? value.toInt().clamp(0, maximum).toInt() : fallback;

  TimeOfDay _time(dynamic value, TimeOfDay fallback) {
    if (value is! String || !RegExp(r'^\d{2}:\d{2}$').hasMatch(value)) {
      return fallback;
    }
    final parts = value.split(':');
    final hour = int.parse(parts[0]);
    final minute = int.parse(parts[1]);
    return hour < 24 && minute < 60
        ? TimeOfDay(hour: hour, minute: minute)
        : fallback;
  }

  String _format(TimeOfDay value) =>
      '${value.hour.toString().padLeft(2, '0')}:${value.minute.toString().padLeft(2, '0')}';

  Future<void> _chooseTime(bool start) async {
    final time = await showTimePicker(
      context: context,
      initialTime: start ? _windowStart : _windowEnd,
      builder: (context, child) => MediaQuery(
        data: MediaQuery.of(context).copyWith(alwaysUse24HourFormat: true),
        child: child!,
      ),
    );
    if (time == null || !mounted) return;
    setState(() {
      if (start) {
        _windowStart = time;
      } else {
        _windowEnd = time;
      }
    });
  }

  Future<void> _save() async {
    if (_saving || _loading || !_supported || _error != null) return;
    FocusScope.of(context).unfocus();
    final prefs = <String, dynamic>{
      'enabled': _enabled,
      'max_per_day': _maxPerDay,
      'window_start': _format(_windowStart),
      'window_end': _format(_windowEnd),
      'timezone': _timezone,
      'topics_like': _topicsLike.text,
      'topics_avoid': _topicsAvoid.text,
      'style': _style.text,
      'feed_enabled': _feedEnabled,
      'feed_per_day': _feedPerDay,
      'feed_instructions': _feedInstructions.text,
    };
    setState(() => _saving = true);
    try {
      final updated = await widget.api.updateProactivePrefs(prefs);
      if (!mounted) return;
      if (updated.isEmpty) {
        setState(() => _supported = false);
        _showMessage('当前服务器暂不支持主动消息设置');
        return;
      }
      setState(() => _apply(updated));
      _showMessage('主动消息设置已保存');
    } catch (error) {
      if (mounted) _showMessage(_message(error, '主动消息设置保存失败'));
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  String _message(Object error, String fallback) =>
      error is AssistantApiException ? error.message : fallback;

  void _showMessage(String text) =>
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(text)));

  @override
  Widget build(BuildContext context) {
    final colors = context.muse;
    final editable = !_saving && !_loading && _supported && _error == null;
    return Scaffold(
      backgroundColor: colors.bg,
      appBar: AppBar(title: const Text('主动消息')),
      body: _loading
          ? const Center(child: CircularProgressIndicator())
          : ListView(
              key: const Key('proactive-settings-list'),
              padding: const EdgeInsets.fromLTRB(16, 8, 16, 32),
              children: [
                if (_error != null) ...[
                  Text(_error!, style: TextStyle(color: colors.danger)),
                  TextButton(onPressed: _load, child: const Text('重试')),
                ],
                if (!_supported)
                  Padding(
                    padding: const EdgeInsets.only(bottom: 16),
                    child: Text(
                      '当前服务器暂不支持主动消息设置',
                      style: TextStyle(color: colors.muted),
                    ),
                  ),
                _group([
                  SwitchListTile.adaptive(
                    key: const Key('proactive-enabled'),
                    contentPadding: EdgeInsets.zero,
                    title: const Text('主动消息'),
                    subtitle: Text('让 ${context.brand.name} 在合适的时间主动联系你'),
                    value: _enabled,
                    onChanged: editable
                        ? (value) => setState(() => _enabled = value)
                        : null,
                  ),
                  _stepper(
                    '每天最多',
                    'proactive-max',
                    _maxPerDay,
                    5,
                    editable,
                    (value) => setState(() => _maxPerDay = value),
                  ),
                  const SizedBox(height: 12),
                  const Text('时间段'),
                  const SizedBox(height: 8),
                  Row(
                    children: [
                      Expanded(
                        child: OutlinedButton(
                          key: const Key('proactive-window-start'),
                          onPressed: editable ? () => _chooseTime(true) : null,
                          child: Text(_format(_windowStart)),
                        ),
                      ),
                      const Padding(
                        padding: EdgeInsets.symmetric(horizontal: 12),
                        child: Text('至'),
                      ),
                      Expanded(
                        child: OutlinedButton(
                          key: const Key('proactive-window-end'),
                          onPressed: editable ? () => _chooseTime(false) : null,
                          child: Text(_format(_windowEnd)),
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 16),
                  DropdownButtonFormField<String>(
                    key: ValueKey('proactive-timezone-$_timezone'),
                    initialValue: _timezone,
                    isExpanded: true,
                    decoration: const InputDecoration(labelText: '时区'),
                    items: {..._timezones, _timezone}
                        .map(
                          (value) => DropdownMenuItem(
                            value: value,
                            child: Text(value),
                          ),
                        )
                        .toList(),
                    onChanged: editable
                        ? (value) => setState(() => _timezone = value!)
                        : null,
                  ),
                  const SizedBox(height: 20),
                  _field(
                    '想听的话题',
                    'proactive-topics-like',
                    _topicsLike,
                    1000,
                    editable,
                  ),
                  const SizedBox(height: 16),
                  _field(
                    '不想听的话题',
                    'proactive-topics-avoid',
                    _topicsAvoid,
                    1000,
                    editable,
                  ),
                  const SizedBox(height: 16),
                  _field('风格', 'proactive-style', _style, 500, editable),
                ]),
                const SizedBox(height: 20),
                _group([
                  SwitchListTile.adaptive(
                    key: const Key('proactive-feed-enabled'),
                    contentPadding: EdgeInsets.zero,
                    title: const Text('动态'),
                    subtitle: const Text('围绕你关心的话题生成资讯帖子'),
                    value: _feedEnabled,
                    onChanged: editable
                        ? (value) => setState(() => _feedEnabled = value)
                        : null,
                  ),
                  _stepper(
                    '每天篇数',
                    'proactive-feed-max',
                    _feedPerDay,
                    3,
                    editable,
                    (value) => setState(() => _feedPerDay = value),
                  ),
                  const SizedBox(height: 16),
                  _field(
                    '动态说明',
                    'proactive-feed-instructions',
                    _feedInstructions,
                    2000,
                    editable,
                  ),
                ]),
                const SizedBox(height: 24),
                FilledButton(
                  key: const Key('proactive-save'),
                  onPressed: editable ? _save : null,
                  child: Text(_saving ? '保存中…' : '保存'),
                ),
              ],
            ),
    );
  }

  Widget _group(List<Widget> children) => Material(
    color: context.muse.bubble,
    borderRadius: BorderRadius.circular(MuseMetrics.cardRadius),
    clipBehavior: Clip.antiAlias,
    child: Padding(
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: children,
      ),
    ),
  );

  Widget _stepper(
    String label,
    String key,
    int value,
    int maximum,
    bool editable,
    ValueChanged<int> onChanged,
  ) => Row(
    children: [
      Expanded(child: Text(label)),
      IconButton(
        key: Key('$key-minus'),
        tooltip: '减少$label',
        onPressed: editable && value > 0 ? () => onChanged(value - 1) : null,
        icon: const Icon(Icons.remove_rounded),
      ),
      Text('$value', key: Key('$key-value')),
      IconButton(
        key: Key('$key-plus'),
        tooltip: '增加$label',
        onPressed: editable && value < maximum
            ? () => onChanged(value + 1)
            : null,
        icon: const Icon(Icons.add_rounded),
      ),
    ],
  );

  Widget _field(
    String label,
    String key,
    TextEditingController controller,
    int limit,
    bool editable,
  ) => TextField(
    key: Key(key),
    controller: controller,
    enabled: editable,
    maxLength: limit,
    minLines: 2,
    maxLines: 4,
    decoration: InputDecoration(labelText: label, counterText: ''),
  );
}
