import 'package:flutter/material.dart';
import 'package:flutter/foundation.dart';

import '../api.dart';
import '../session_utils.dart';
import '../theme.dart';

class NotificationsView extends StatefulWidget {
  const NotificationsView({
    super.key,
    required this.api,
    required this.notifications,
    required this.onChanged,
    this.updates,
  });

  final AssistantApi api;
  final List<Map<String, dynamic>> notifications;
  final ValueChanged<List<Map<String, dynamic>>> onChanged;
  final ValueListenable<List<Map<String, dynamic>>>? updates;

  @override
  State<NotificationsView> createState() => _NotificationsViewState();
}

class _NotificationsViewState extends State<NotificationsView> {
  late List<Map<String, dynamic>> _items;
  final Set<String> _reading = {};
  String? _error;

  @override
  void initState() {
    super.initState();
    _items = List.of(widget.notifications);
    widget.updates?.addListener(_syncItems);
    _refresh();
  }

  void _syncItems() {
    if (mounted) setState(() => _items = List.of(widget.updates!.value));
  }

  @override
  void dispose() {
    widget.updates?.removeListener(_syncItems);
    super.dispose();
  }

  Future<void> _refresh() async {
    try {
      final items = await widget.api.listNotifications();
      if (!mounted) return;
      setState(() {
        _items = mergeNotifications(_items, items);
        _error = null;
      });
      widget.onChanged(_items);
    } catch (_) {
      if (mounted) setState(() => _error = '通知加载失败，下拉重试');
    }
  }

  Future<void> _read(Map<String, dynamic> item) async {
    final id = '${item['id'] ?? ''}';
    if (id.isEmpty || item['read_at'] != null || _reading.contains(id)) return;
    setState(() => _reading.add(id));
    try {
      final updated = await widget.api.markNotificationRead(id);
      if (!mounted) return;
      setState(() {
        _items = _items
            .map((entry) => entry['id'] == id ? updated : entry)
            .toList();
      });
      widget.onChanged(_items);
    } catch (_) {
      if (mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(const SnackBar(content: Text('标记已读失败')));
      }
    } finally {
      if (mounted) setState(() => _reading.remove(id));
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('通知')),
      body: RefreshIndicator(
        onRefresh: _refresh,
        child: ListView(
          physics: const AlwaysScrollableScrollPhysics(),
          padding: const EdgeInsets.all(16),
          children: [
            if (_error != null)
              Text(_error!, style: TextStyle(color: context.muse.muted)),
            if (_items.isEmpty && _error == null)
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 48),
                child: Text(
                  '还没有通知',
                  textAlign: TextAlign.center,
                  style: TextStyle(color: context.muse.muted),
                ),
              ),
            for (final item in _items)
              Padding(
                padding: const EdgeInsets.only(bottom: 12),
                child: Material(
                  color: context.muse.bubble,
                  borderRadius: BorderRadius.circular(22),
                  child: ListTile(
                    shape: RoundedRectangleBorder(
                      borderRadius: BorderRadius.circular(22),
                    ),
                    contentPadding: const EdgeInsets.symmetric(
                      horizontal: 16,
                      vertical: 8,
                    ),
                    title: Text('${item['title'] ?? '通知'}'),
                    subtitle: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text('${item['body'] ?? ''}'),
                        const SizedBox(height: 6),
                        Text(
                          _relativeTime(item),
                          style: TextStyle(
                            color: context.muse.muted,
                            fontSize: 12,
                          ),
                        ),
                      ],
                    ),
                    trailing: Icon(
                      item['read_at'] == null ? Icons.circle : Icons.done,
                      size: 12,
                      color: context.muse.accent,
                    ),
                    onTap: () => _read(item),
                  ),
                ),
              ),
          ],
        ),
      ),
    );
  }
}

String _relativeTime(Map<String, dynamic> notification) {
  final date = DateTime.tryParse('${notification['created_at'] ?? ''}');
  return date == null ? '' : formatRelativeTime(date.toLocal());
}

/// Merge polling and stream arrivals without losing newer notifications or reads.
List<Map<String, dynamic>> mergeNotifications(
  List<Map<String, dynamic>> current,
  List<Map<String, dynamic>> incoming,
) {
  final previous = {for (final item in current) item['id']: item};
  final merged = <Object?, Map<String, dynamic>>{};
  for (final item in [...incoming, ...current]) {
    final id = item['id'];
    if (id is! String || id.isEmpty || merged.containsKey(id)) continue;
    merged[id] = {
      ...?previous[id],
      ...item,
      if (previous[id]?['read_at'] != null && item['read_at'] == null)
        'read_at': previous[id]!['read_at'],
    };
  }
  final items = merged.values.toList();
  final order = {for (var i = 0; i < items.length; i++) items[i]['id']: i};
  items.sort((a, b) {
    final first = DateTime.tryParse('${a['created_at'] ?? ''}');
    final second = DateTime.tryParse('${b['created_at'] ?? ''}');
    final time = (second?.millisecondsSinceEpoch ?? 0).compareTo(
      first?.millisecondsSinceEpoch ?? 0,
    );
    return time != 0 ? time : order[a['id']]!.compareTo(order[b['id']]!);
  });
  return items.take(100).toList();
}
