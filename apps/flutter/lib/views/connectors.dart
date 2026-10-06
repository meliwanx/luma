import 'package:flutter/material.dart';

import '../api.dart';
import '../theme.dart';

class ConnectorsView extends StatefulWidget {
  const ConnectorsView({super.key, required this.api});

  final AssistantApi api;

  @override
  State<ConnectorsView> createState() => _ConnectorsViewState();
}

class _ConnectorsViewState extends State<ConnectorsView> {
  late Future<List<Map<String, dynamic>>> _future;

  @override
  void initState() {
    super.initState();
    _reload();
  }

  void _reload() => _future = widget.api.listConnectors();

  Future<void> _refresh() async {
    setState(_reload);
    try {
      await _future;
    } catch (_) {}
  }

  String _host(dynamic endpoint) {
    final uri = Uri.tryParse('$endpoint');
    return uri?.host.isNotEmpty == true ? uri!.host : '$endpoint';
  }

  List<Map<String, dynamic>> _tools(Map<String, dynamic> connector) {
    final metadata = connector['metadata'];
    final raw = metadata is Map ? metadata['tools'] : null;
    return raw is List
        ? raw
              .whereType<Map>()
              .map((item) => Map<String, dynamic>.from(item))
              .toList()
        : const [];
  }

  Future<void> _openDetails(Map<String, dynamic> connector) async {
    if (connector['kind'] != 'mcp') return;
    await Navigator.of(context).push(
      MaterialPageRoute(
        builder: (_) =>
            ConnectorDetailView(api: widget.api, initial: connector),
      ),
    );
    if (mounted) _refresh();
  }

  Future<void> _add() async {
    final result = await showModalBottomSheet<Map<String, dynamic>>(
      context: context,
      isScrollControlled: true,
      backgroundColor: context.muse.bg,
      showDragHandle: true,
      builder: (_) => AddMcpView(api: widget.api),
    );
    if (!mounted || result == null) return;
    final connectors = result['connectors'];
    final first = connectors is List && connectors.isNotEmpty
        ? connectors.first
        : null;
    final name = first is Map ? '${first['name'] ?? '连接器'}' : '连接器';
    final tools = first is Map
        ? _tools(Map<String, dynamic>.from(first)).length
        : 0;
    ScaffoldMessenger.of(context)
        .showSnackBar(SnackBar(content: Text('已添加 $name（$tools 个工具）')));
    _refresh();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: context.muse.bg,
      appBar: AppBar(
        title: const Text('连接器'),
        backgroundColor: context.muse.bg,
        foregroundColor: context.muse.text,
        elevation: 0,
        actions: [
          IconButton(
            onPressed: _add,
            icon: const Icon(Icons.add),
            tooltip: '添加 MCP',
          ),
        ],
      ),
      body: FutureBuilder<List<Map<String, dynamic>>>(
        future: _future,
        builder: (context, snapshot) {
          if (snapshot.connectionState == ConnectionState.waiting) {
            return const Center(child: CircularProgressIndicator());
          }
          if (snapshot.hasError) {
            return _ErrorState(message: '${snapshot.error}', onRetry: _refresh);
          }
          final rows = snapshot.data ?? const [];
          return RefreshIndicator(
            onRefresh: _refresh,
            color: context.muse.accent,
            child: rows.isEmpty
                ? ListView(
                    children: [
                      const SizedBox(height: 120),
                      Center(
                        child: Text(
                          '还没有连接器',
                          style: TextStyle(color: context.muse.muted),
                        ),
                      ),
                    ],
                  )
                : ListView.separated(
                    padding: const EdgeInsets.fromLTRB(16, 8, 16, 24),
                    itemCount: rows.length,
                    separatorBuilder: (_, _) =>
                        Divider(color: context.muse.line),
                    itemBuilder: (_, index) => _connectorTile(rows[index]),
                  ),
          );
        },
      ),
      floatingActionButton: FloatingActionButton.extended(
        onPressed: _add,
        backgroundColor: context.muse.accent,
        foregroundColor: Colors.white,
        icon: const Icon(Icons.add),
        label: const Text('添加 MCP'),
      ),
    );
  }

  Widget _connectorTile(Map<String, dynamic> connector) {
    final enabled = connector['enabled'] != false;
    final status = connector['metadata'] is Map
        ? '${(connector['metadata'] as Map)['status'] ?? 'ok'}'
        : 'ok';
    final tools = _tools(connector);
    return ListTile(
      contentPadding: const EdgeInsets.symmetric(vertical: 4),
      onTap: connector['kind'] == 'mcp' ? () => _openDetails(connector) : null,
      leading: Icon(
        Icons.circle,
        size: 12,
        color: enabled && status == 'ok'
            ? context.muse.green
            : context.muse.warn,
      ),
      title: Text('${connector['name'] ?? '未命名连接器'}'),
      subtitle: Text(
        '${_host(connector['endpoint'])} · ${tools.length} 个工具',
        style: TextStyle(color: context.muse.muted, fontSize: 12),
      ),
      trailing: Switch(
        value: enabled,
        onChanged: (value) async {
          try {
            await widget.api.setConnectorEnabled('${connector['id']}', value);
            if (mounted) _refresh();
          } catch (error) {
            if (mounted) _showError(error);
          }
        },
      ),
    );
  }

  void _showError(Object error) => ScaffoldMessenger.of(context).showSnackBar(
    SnackBar(
      content: Text(error is AssistantApiException ? error.message : '$error'),
    ),
  );
}

class _ErrorState extends StatelessWidget {
  const _ErrorState({required this.message, required this.onRetry});
  final String message;
  final VoidCallback onRetry;
  @override
  Widget build(BuildContext context) => Center(
    child: Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        Text(message, textAlign: TextAlign.center),
        const SizedBox(height: 12),
        OutlinedButton(onPressed: onRetry, child: const Text('重试')),
      ],
    ),
  );
}

class ConnectorDetailView extends StatefulWidget {
  const ConnectorDetailView({
    super.key,
    required this.api,
    required this.initial,
  });
  final AssistantApi api;
  final Map<String, dynamic> initial;
  @override
  State<ConnectorDetailView> createState() => _ConnectorDetailViewState();
}

class _ConnectorDetailViewState extends State<ConnectorDetailView> {
  late Map<String, dynamic> connector;
  bool busy = false;

  @override
  void initState() {
    super.initState();
    connector = Map<String, dynamic>.from(widget.initial);
  }

  List<Map<String, dynamic>> get tools {
    final metadata = connector['metadata'];
    final raw = metadata is Map ? metadata['tools'] : null;
    return raw is List
        ? raw
              .whereType<Map>()
              .map((item) => Map<String, dynamic>.from(item))
              .toList()
        : const [];
  }

  String get id => '${connector['id']}';

  Future<void> _run(Future<Map<String, dynamic>> Function() action) async {
    setState(() => busy = true);
    try {
      final next = await action();
      if (mounted) setState(() => connector = next);
    } catch (error) {
      if (mounted) _showError(error);
    } finally {
      if (mounted) setState(() => busy = false);
    }
  }

  Future<void> _toggleTool(Map<String, dynamic> tool, bool enabled) async {
    final next = {
      for (final item in tools) '${item['name']}': item['enabled'] != false,
    };
    next['${tool['name']}'] = enabled;
    await _run(() => widget.api.updateMcpConnector(id, tools: next));
  }

  Future<void> _token() async {
    final controller = TextEditingController();
    final value = await showDialog<String>(
      context: context,
      builder: (dialog) => AlertDialog(
        title: const Text('更新令牌'),
        content: TextField(
          controller: controller,
          obscureText: true,
          autofocus: true,
          decoration: const InputDecoration(labelText: 'Authorization 值'),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialog),
            child: const Text('取消'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(dialog, controller.text),
            child: const Text('保存'),
          ),
        ],
      ),
    );
    controller.dispose();
    if (value == null || value.trim().isEmpty) return;
    await _run(
      () => widget.api.updateMcpConnector(
        id,
        headers: {'Authorization': value.trim()},
      ),
    );
  }

  Future<void> _delete() async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (dialog) => AlertDialog(
        title: const Text('删除连接器？'),
        content: const Text('删除后令牌也会从服务端移除。'),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialog, false),
            child: const Text('取消'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(dialog, true),
            child: const Text('删除'),
          ),
        ],
      ),
    );
    if (confirmed != true) return;
    try {
      await widget.api.deleteConnector(id);
      if (mounted) Navigator.pop(context);
    } catch (error) {
      if (mounted) _showError(error);
    }
  }

  @override
  Widget build(BuildContext context) {
    final metadata = connector['metadata'] is Map
        ? Map<String, dynamic>.from(connector['metadata'] as Map)
        : const <String, dynamic>{};
    final hints = metadata['header_hints'] is Map
        ? Map<String, dynamic>.from(metadata['header_hints'] as Map)
        : const <String, dynamic>{};
    return Scaffold(
      backgroundColor: context.muse.bg,
      appBar: AppBar(
        title: Text('${connector['name'] ?? '连接器'}'),
        backgroundColor: context.muse.bg,
        foregroundColor: context.muse.text,
        elevation: 0,
      ),
      body: ListView(
        padding: const EdgeInsets.fromLTRB(16, 8, 16, 32),
        children: [
          if (hints.isNotEmpty) ...[
            Text(
              '请求头（已打码）',
              style: TextStyle(color: context.muse.muted, fontSize: 12),
            ),
            const SizedBox(height: 6),
            for (final entry in hints.entries)
              Text(
                '${entry.key}: ${entry.value}',
                style: TextStyle(color: context.muse.text, fontSize: 13),
              ),
            const SizedBox(height: 20),
          ],
          Text(
            '工具',
            style: TextStyle(
              color: context.muse.text,
              fontWeight: FontWeight.w600,
            ),
          ),
          const SizedBox(height: 6),
          if (tools.isEmpty)
            Text('暂无工具', style: TextStyle(color: context.muse.muted)),
          for (final tool in tools)
            ListTile(
              contentPadding: EdgeInsets.zero,
              title: Text('${tool['title'] ?? tool['name'] ?? ''}'),
              subtitle: Text(
                '${tool['description'] ?? ''}',
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(color: context.muse.muted, fontSize: 12),
              ),
              leading: tool['read_only'] == true
                  ? null
                  : Chip(
                      label: const Text('需确认'),
                      labelStyle: const TextStyle(fontSize: 10),
                      padding: EdgeInsets.zero,
                    ),
              trailing: Switch(
                value: tool['enabled'] != false,
                onChanged: busy ? null : (value) => _toggleTool(tool, value),
              ),
            ),
          const SizedBox(height: 16),
          OutlinedButton.icon(
            onPressed: busy
                ? null
                : () => _run(() => widget.api.syncConnector(id)),
            icon: const Icon(Icons.sync),
            label: const Text('重新同步'),
          ),
          OutlinedButton.icon(
            onPressed: busy ? null : _token,
            icon: const Icon(Icons.key_outlined),
            label: const Text('更新令牌'),
          ),
          const SizedBox(height: 8),
          OutlinedButton.icon(
            onPressed: busy ? null : _delete,
            icon: const Icon(Icons.delete_outline),
            label: const Text('删除'),
            style: OutlinedButton.styleFrom(
              foregroundColor: context.muse.danger,
            ),
          ),
        ],
      ),
    );
  }

  void _showError(Object error) => ScaffoldMessenger.of(context).showSnackBar(
    SnackBar(
      content: Text(error is AssistantApiException ? error.message : '$error'),
    ),
  );
}

class AddMcpView extends StatefulWidget {
  const AddMcpView({super.key, required this.api});
  final AssistantApi api;
  @override
  State<AddMcpView> createState() => _AddMcpViewState();
}

class _AddMcpViewState extends State<AddMcpView>
    with SingleTickerProviderStateMixin {
  late final TabController tabs;
  final name = TextEditingController();
  final url = TextEditingController();
  final authorization = TextEditingController();
  final config = TextEditingController();
  bool busy = false;

  @override
  void initState() {
    super.initState();
    tabs = TabController(length: 2, vsync: this);
  }

  @override
  void dispose() {
    tabs.dispose();
    name.dispose();
    url.dispose();
    authorization.dispose();
    config.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    setState(() => busy = true);
    try {
      final result = tabs.index == 1
          ? await widget.api.addMcpConnector(config: config.text)
          : await widget.api.addMcpConnector(
              name: name.text,
              url: url.text,
              headers: authorization.text.trim().isEmpty
                  ? null
                  : {'Authorization': authorization.text.trim()},
            );
      if (mounted) Navigator.pop(context, result);
    } catch (error) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            content: Text(
              error is AssistantApiException ? error.message : '$error',
            ),
          ),
        );
      }
    } finally {
      if (mounted) setState(() => busy = false);
    }
  }

  @override
  Widget build(BuildContext context) => SafeArea(
    child: Padding(
      padding: EdgeInsets.fromLTRB(
        20,
        4,
        20,
        MediaQuery.viewInsetsOf(context).bottom + 20,
      ),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Text(
            '添加 MCP',
            style: TextStyle(fontSize: 20, fontWeight: FontWeight.w600),
          ),
          TabBar(
            controller: tabs,
            tabs: const [
              Tab(text: '填写'),
              Tab(text: '粘贴 JSON'),
            ],
          ),
          SizedBox(
            height: 250,
            child: TabBarView(
              controller: tabs,
              children: [
                ListView(
                  children: [
                    TextField(
                      controller: name,
                      decoration: const InputDecoration(labelText: '名称（可选）'),
                    ),
                    TextField(
                      controller: url,
                      decoration: const InputDecoration(labelText: 'URL'),
                      keyboardType: TextInputType.url,
                    ),
                    TextField(
                      controller: authorization,
                      decoration: const InputDecoration(
                        labelText: 'Authorization 值',
                      ),
                      obscureText: true,
                    ),
                  ],
                ),
                TextField(
                  controller: config,
                  maxLines: 10,
                  decoration: const InputDecoration(
                    labelText: 'MCP 配置 JSON',
                    alignLabelWithHint: true,
                  ),
                ),
              ],
            ),
          ),
          const SizedBox(height: 8),
          SizedBox(
            width: double.infinity,
            child: FilledButton.icon(
              onPressed: busy ? null : _submit,
              icon: busy
                  ? const SizedBox(
                      width: 16,
                      height: 16,
                      child: CircularProgressIndicator(
                        strokeWidth: 2,
                        color: Colors.white,
                      ),
                    )
                  : const Icon(Icons.add),
              label: Text(busy ? '正在连接…' : '添加'),
            ),
          ),
        ],
      ),
    ),
  );
}
