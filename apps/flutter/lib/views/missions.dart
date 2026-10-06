import 'package:flutter/material.dart';

import '../api.dart';
import '../theme.dart';

typedef ApprovalDecision = Future<void> Function(String id, bool approve);
typedef ApprovalDecisionWithRemember = Future<void> Function(
  String id,
  bool approve,
  bool remember,
);

class MissionsView extends StatelessWidget {
  const MissionsView({
    super.key,
    required this.tasks,
    required this.approvals,
    required this.runtimeJobs,
    required this.onDecideApproval,
    this.onDecideApprovalWithRemember,
    this.api,
    this.routines = const [],
    this.contentPadding,
    this.showHeader = true,
    this.onRefresh,
  });

  final List<Map<String, dynamic>> tasks;
  final List<Map<String, dynamic>> approvals;
  final List<Map<String, dynamic>> runtimeJobs;
  final ApprovalDecision onDecideApproval;
  final ApprovalDecisionWithRemember? onDecideApprovalWithRemember;
  final AssistantApi? api;
  final List<Map<String, dynamic>> routines;
  final EdgeInsetsGeometry? contentPadding;
  final bool showHeader;
  final Future<void> Function()? onRefresh;

  @override
  Widget build(BuildContext context) {
    final colors = context.muse;

    return LayoutBuilder(
      builder: (context, constraints) {
        final wide = constraints.maxWidth >= MuseMetrics.mobileBreakpoint;
        final horizontalPadding = wide ? 24.0 : 16.0;
        final topPadding = wide ? 72.0 : 24.0;

        final list = ListView(
          physics: const AlwaysScrollableScrollPhysics(),
          keyboardDismissBehavior: ScrollViewKeyboardDismissBehavior.onDrag,
          padding:
              contentPadding ??
              EdgeInsets.fromLTRB(
                horizontalPadding,
                topPadding,
                horizontalPadding,
                32,
              ),
          children: [
            Center(
              child: ConstrainedBox(
                constraints: const BoxConstraints(
                  maxWidth: MuseMetrics.readingColumn,
                ),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    if (showHeader) _PageHeader(colors: colors),
                    if (approvals.isNotEmpty) ...[
                      const SizedBox(height: 28),
                      _SectionHeading(label: '需要你的确认', colors: colors),
                      const SizedBox(height: 10),
                      ...approvals.map(
                        (approval) => Padding(
                          padding: const EdgeInsets.only(bottom: 12),
                          child: _ApprovalCard(
                            approval: approval,
                            colors: colors,
                            onDecideApproval: onDecideApproval,
                            onDecideApprovalWithRemember:
                                onDecideApprovalWithRemember,
                          ),
                        ),
                      ),
                    ],
                    if (tasks.isNotEmpty) ...[
                      SizedBox(height: approvals.isEmpty ? 28 : 16),
                      _SectionHeading(label: '目标清单', colors: colors),
                      const SizedBox(height: 10),
                      _TaskGrid(tasks: tasks, colors: colors),
                    ],
                    if (runtimeJobs.isNotEmpty) ...[
                      SizedBox(height: tasks.isEmpty ? 28 : 32),
                      _RuntimeList(runtimeJobs: runtimeJobs, colors: colors),
                    ],
                    if (routines.isNotEmpty || api != null) ...[
                      SizedBox(
                        height: tasks.isEmpty && runtimeJobs.isEmpty ? 28 : 32,
                      ),
                      _RoutineSection(
                        api: api,
                        routines: routines,
                        colors: colors,
                      ),
                    ],
                    if (tasks.isEmpty &&
                        approvals.isEmpty &&
                        runtimeJobs.isEmpty &&
                        routines.isEmpty &&
                        api == null) ...[
                      const SizedBox(height: 30),
                      _EmptyState(colors: colors),
                    ],
                  ],
                ),
              ),
            ),
          ],
        );
        return onRefresh == null
            ? list
            : RefreshIndicator(onRefresh: onRefresh!, child: list);
      },
    );
  }
}

class _PageHeader extends StatelessWidget {
  const _PageHeader({required this.colors});

  final MuseColors colors;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          '目标',
          style: TextStyle(
            color: colors.text,
            fontSize: 24,
            fontWeight: FontWeight.w600,
            height: 1.2,
          ),
        ),
        const SizedBox(height: 6),
        ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 520),
          child: Text(
            '让重要的事持续向前',
            style: TextStyle(color: colors.muted, fontSize: 14, height: 1.45),
          ),
        ),
      ],
    );
  }
}

class _SectionHeading extends StatelessWidget {
  const _SectionHeading({required this.label, required this.colors});

  final String label;
  final MuseColors colors;

  @override
  Widget build(BuildContext context) {
    return Text(
      label,
      style: TextStyle(
        color: colors.muted,
        fontSize: 13,
        fontWeight: FontWeight.w600,
        height: 1.3,
      ),
    );
  }
}

class _ApprovalCard extends StatelessWidget {
  const _ApprovalCard({
    required this.approval,
    required this.colors,
    required this.onDecideApproval,
    this.onDecideApprovalWithRemember,
  });

  final Map<String, dynamic> approval;
  final MuseColors colors;
  final ApprovalDecision onDecideApproval;
  final ApprovalDecisionWithRemember? onDecideApprovalWithRemember;

  @override
  Widget build(BuildContext context) {
    final payload = approval['payload'] is Map
        ? Map<String, dynamic>.from(approval['payload'] as Map)
        : const <String, dynamic>{};
    final action = approval['action'];
    final title = action == 'create_memory' ? '确认保存一条记忆' : '确认后台操作';
    final description =
        _firstText([
          payload['content'],
          payload['title'],
          approval['description'],
        ]) ??
        '运行时请求执行一项写入操作。';
    final id = '${approval['id']}';
    final allowAlways =
        approval['allow_always'] == true ||
        (approval['payload'] is Map &&
            (approval['payload'] as Map)['allow_always'] == true);

    return Container(
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: colors.warn.withValues(alpha: 0.08),
        borderRadius: BorderRadius.circular(MuseMetrics.cardRadius),
        border: Border.all(color: colors.warn.withValues(alpha: 0.35)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            '等待确认',
            style: TextStyle(
              color: colors.warn,
              fontSize: 12,
              fontWeight: FontWeight.w500,
              height: 1.3,
            ),
          ),
          const SizedBox(height: 6),
          Text(
            title,
            style: TextStyle(
              color: colors.text,
              fontSize: 15,
              fontWeight: FontWeight.w600,
              height: 1.35,
            ),
          ),
          const SizedBox(height: 5),
          Text(
            description,
            style: TextStyle(color: colors.muted, fontSize: 13, height: 1.45),
          ),
          const SizedBox(height: 14),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              _PillButton(
                label: '批准',
                primary: true,
                colors: colors,
                onPressed: () => onDecideApproval(id, true),
              ),
              _PillButton(
                label: '拒绝',
                colors: colors,
                onPressed: () => onDecideApproval(id, false),
              ),
              if (allowAlways)
                _PillButton(
                  label: '始终允许',
                  colors: colors,
                  onPressed: () => onDecideApprovalWithRemember != null
                      ? onDecideApprovalWithRemember!(id, true, true)
                      : onDecideApproval(id, true),
                ),
            ],
          ),
        ],
      ),
    );
  }
}

class _PillButton extends StatelessWidget {
  const _PillButton({
    required this.label,
    required this.colors,
    required this.onPressed,
    this.primary = false,
  });

  final String label;
  final MuseColors colors;
  final VoidCallback onPressed;
  final bool primary;

  @override
  Widget build(BuildContext context) {
    final backgroundColor = primary ? colors.accent : colors.chip;
    final foregroundColor = primary ? Colors.white : colors.text;

    return TextButton(
      onPressed: onPressed,
      style: TextButton.styleFrom(
        backgroundColor: backgroundColor,
        foregroundColor: foregroundColor,
        minimumSize: const Size(0, MuseMetrics.pillHeight),
        padding: const EdgeInsets.symmetric(horizontal: 14),
        tapTargetSize: MaterialTapTargetSize.shrinkWrap,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(MuseMetrics.pillHeight / 2),
        ),
        textStyle: const TextStyle(fontSize: 14, fontWeight: FontWeight.w500),
      ),
      child: Text(label),
    );
  }
}

class _TaskGrid extends StatelessWidget {
  const _TaskGrid({required this.tasks, required this.colors});

  final List<Map<String, dynamic>> tasks;
  final MuseColors colors;

  @override
  Widget build(BuildContext context) {
    const gap = 12.0;
    const minimumTileWidth = 220.0;

    return LayoutBuilder(
      builder: (context, constraints) {
        final availableWidth = constraints.maxWidth;
        final estimatedColumns =
            ((availableWidth + gap) / (minimumTileWidth + gap)).floor();
        final columnCount = estimatedColumns < 1 ? 1 : estimatedColumns;
        final tileWidth =
            (availableWidth - gap * (columnCount - 1)) / columnCount;

        return Wrap(
          spacing: gap,
          runSpacing: gap,
          children: [
            for (final task in tasks)
              SizedBox(
                width: tileWidth,
                child: _TaskCard(task: task, colors: colors),
              ),
          ],
        );
      },
    );
  }
}

class _TaskCard extends StatelessWidget {
  const _TaskCard({required this.task, required this.colors});

  final Map<String, dynamic> task;
  final MuseColors colors;

  @override
  Widget build(BuildContext context) {
    final done = _isDone(task['status']);
    final progress = _progressValue(task);
    final description = _firstText([task['description']]);
    final meta = _firstText([
      task['due'],
      task['due_at'],
      task['deadline'],
      task['meta'],
      task['updated_at'],
    ]);

    final card = Container(
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: colors.bubble,
        borderRadius: BorderRadius.circular(MuseMetrics.cardRadius),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            _statusLabel(task['status']),
            style: TextStyle(
              color: done ? colors.green : colors.muted,
              fontSize: 12,
              fontWeight: FontWeight.w500,
              height: 1.3,
            ),
          ),
          const SizedBox(height: 9),
          Text(
            _firstText([task['title']]) ?? '未命名目标',
            style: TextStyle(
              color: colors.text,
              fontSize: 15,
              fontWeight: FontWeight.w600,
              height: 1.35,
            ),
          ),
          if (description != null) ...[
            const SizedBox(height: 6),
            Text(
              description,
              style: TextStyle(color: colors.muted, fontSize: 13, height: 1.45),
            ),
          ],
          if (progress != null) ...[
            const SizedBox(height: 14),
            ClipRRect(
              borderRadius: BorderRadius.circular(2),
              child: SizedBox(
                height: 4,
                child: LinearProgressIndicator(
                  value: progress,
                  backgroundColor: colors.line,
                  valueColor: AlwaysStoppedAnimation<Color>(colors.accent),
                  minHeight: 4,
                ),
              ),
            ),
          ],
          if (meta != null) ...[
            const SizedBox(height: 12),
            Text(
              meta,
              style: TextStyle(color: colors.faint, fontSize: 12, height: 1.3),
            ),
          ],
        ],
      ),
    );

    return Opacity(opacity: done ? 0.6 : 1.0, child: card);
  }
}

class _RuntimeList extends StatelessWidget {
  const _RuntimeList({required this.runtimeJobs, required this.colors});

  final List<Map<String, dynamic>> runtimeJobs;
  final MuseColors colors;

  @override
  Widget build(BuildContext context) {
    final jobs = runtimeJobs.take(8).toList();
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        _SectionHeading(label: '运行任务', colors: colors),
        const SizedBox(height: 2),
        ...jobs.asMap().entries.map(
          (entry) => _RuntimeRow(
            job: entry.value,
            colors: colors,
            showDivider: entry.key < jobs.length - 1,
          ),
        ),
      ],
    );
  }
}

class _RuntimeRow extends StatelessWidget {
  const _RuntimeRow({
    required this.job,
    required this.colors,
    required this.showDivider,
  });

  final Map<String, dynamic> job;
  final MuseColors colors;
  final bool showDivider;

  @override
  Widget build(BuildContext context) {
    final status = '${job['status'] ?? ''}'.toLowerCase();
    final meta = _firstText([
      job['meta'],
      job['created_at'],
      job['updated_at'],
    ]);
    final statusText = _statusLabel(job['status']);
    final title = _firstText([job['title'], job['type']]) ?? '运行任务';

    Color statusColor;
    switch (status) {
      case 'running':
        statusColor = colors.accent;
      case 'succeeded':
        statusColor = colors.green;
      case 'failed':
        statusColor = colors.danger;
      case 'queued':
      case 'waiting_approval':
        statusColor = colors.warn;
      default:
        statusColor = colors.faint;
    }

    return Column(
      children: [
        Padding(
          padding: const EdgeInsets.symmetric(vertical: 13),
          child: Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Padding(
                padding: const EdgeInsets.only(top: 5),
                child: Container(
                  width: 8,
                  height: 8,
                  decoration: BoxDecoration(
                    color: statusColor,
                    shape: BoxShape.circle,
                  ),
                ),
              ),
              const SizedBox(width: 10),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      title,
                      style: TextStyle(
                        color: colors.text,
                        fontSize: 14,
                        fontWeight: FontWeight.w500,
                        height: 1.35,
                      ),
                    ),
                    const SizedBox(height: 3),
                    Text(
                      meta == null ? statusText : '$statusText · $meta',
                      style: TextStyle(
                        color: colors.muted,
                        fontSize: 12,
                        height: 1.35,
                      ),
                    ),
                  ],
                ),
              ),
            ],
          ),
        ),
        if (showDivider) Container(height: 1, color: colors.line),
      ],
    );
  }
}

class _RoutineSection extends StatefulWidget {
  const _RoutineSection({
    required this.api,
    required this.routines,
    required this.colors,
  });

  final AssistantApi? api;
  final List<Map<String, dynamic>> routines;
  final MuseColors colors;

  @override
  State<_RoutineSection> createState() => _RoutineSectionState();
}

class _RoutineSectionState extends State<_RoutineSection> {
  late List<Map<String, dynamic>> _routines;
  bool _loading = false;

  @override
  void initState() {
    super.initState();
    _routines = _copyRows(widget.routines);
    if (widget.api != null && _routines.isEmpty) _load();
  }

  @override
  void didUpdateWidget(covariant _RoutineSection oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!identical(oldWidget.routines, widget.routines)) {
      _routines = _copyRows(widget.routines);
    }
  }

  Future<void> _load() async {
    final api = widget.api;
    if (api == null || _loading) return;
    setState(() => _loading = true);
    try {
      final rows = await api.listRoutines();
      if (mounted) setState(() => _routines = rows);
    } catch (error) {
      if (mounted) _showError(error, '例程列表加载失败');
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  Future<void> _edit([Map<String, dynamic>? routine]) async {
    final values = await _showRoutineDialog(routine);
    if (values == null || !mounted) return;
    final api = widget.api;
    if (api == null) return;
    try {
      final result = routine == null
          ? await api.createRoutine(
              title: values.title,
              prompt: values.prompt,
              schedule: values.schedule,
              timezone: values.timezone,
              enabled: values.enabled,
            )
          : await api.updateRoutine(
              '${routine['id']}',
              title: values.title,
              prompt: values.prompt,
              schedule: values.schedule,
              timezone: values.timezone,
              enabled: values.enabled,
            );
      if (!mounted) return;
      setState(() {
        if (routine == null) {
          _routines = [result, ..._routines];
        } else {
          final index = _routines.indexWhere(
            (item) => item['id'] == routine['id'],
          );
          if (index >= 0) _routines[index] = result;
        }
      });
    } catch (error) {
      if (mounted) _showError(error, routine == null ? '创建例程失败' : '更新例程失败');
    }
  }

  Future<void> _setEnabled(int index, bool enabled) async {
    final api = widget.api;
    if (api == null || index < 0 || index >= _routines.length) return;
    final old = _routines[index];
    setState(() => _routines[index] = {...old, 'enabled': enabled});
    try {
      final result = await api.updateRoutine('${old['id']}', enabled: enabled);
      if (mounted) setState(() => _routines[index] = result);
    } catch (error) {
      if (!mounted) return;
      setState(() => _routines[index] = old);
      _showError(error, '例程状态更新失败');
    }
  }

  Future<void> _run(Map<String, dynamic> routine) async {
    final api = widget.api;
    if (api == null) return;
    try {
      await api.runRoutine('${routine['id']}');
      if (mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(const SnackBar(content: Text('例程已加入运行队列')));
      }
    } catch (error) {
      if (mounted) _showError(error, '立即运行例程失败');
    }
  }

  Future<_RoutineFormValues?> _showRoutineDialog(
    Map<String, dynamic>? routine,
  ) async {
    final titleController = TextEditingController(
      text: '${routine?['title'] ?? ''}',
    );
    final promptController = TextEditingController(
      text: '${routine?['prompt'] ?? ''}',
    );
    final timezone = '${routine?['timezone'] ?? 'Asia/Shanghai'}';
    var frequency = 'daily';
    var weekday = '1';
    var time = '09:00';
    var minutes = '30';
    var enabled = routine?['enabled'] != false;
    final schedule = '${routine?['schedule'] ?? ''}';
    final daily = RegExp(r'^daily\s+(\d{1,2}:\d{2})$').firstMatch(schedule);
    final weekly = RegExp(r'^weekly\s+([1-7])\s+(\d{1,2}:\d{2})$')
        .firstMatch(schedule);
    final every = RegExp(r'^every\s+(\d+)m$').firstMatch(schedule);
    if (weekly != null) {
      frequency = 'weekly';
      weekday = weekly.group(1)!;
      time = weekly.group(2)!;
    } else if (every != null) {
      frequency = 'every';
      minutes = every.group(1)!;
    } else if (daily != null) {
      time = daily.group(1)!;
    }
    final timeController = TextEditingController(text: time);
    final minutesController = TextEditingController(text: minutes);
    String? validationError;

    return showDialog<_RoutineFormValues>(
      context: context,
      builder: (dialogContext) => StatefulBuilder(
        builder: (context, setDialogState) {
          String buildSchedule() {
            if (frequency == 'weekly') return 'weekly $weekday $time';
            if (frequency == 'every') return 'every ${minutes.trim()}m';
            return 'daily $time';
          }

          return AlertDialog(
            title: Text(routine == null ? '新建例程' : '编辑例程'),
            content: SingleChildScrollView(
              child: SizedBox(
                width: 420,
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    TextField(
                      controller: titleController,
                      autofocus: true,
                      decoration: const InputDecoration(labelText: '名称'),
                    ),
                    const SizedBox(height: 10),
                    TextField(
                      controller: promptController,
                      minLines: 2,
                      maxLines: 5,
                      decoration: const InputDecoration(labelText: '执行内容'),
                    ),
                    const SizedBox(height: 14),
                    DropdownButtonFormField<String>(
                      key: ValueKey('routine-frequency-$frequency'),
                      initialValue: frequency,
                      decoration: const InputDecoration(labelText: '频率'),
                      items: const [
                        DropdownMenuItem(value: 'daily', child: Text('每天')),
                        DropdownMenuItem(value: 'weekly', child: Text('每周')),
                        DropdownMenuItem(value: 'every', child: Text('每 N 分钟')),
                      ],
                      onChanged: (value) {
                        if (value != null) {
                          setDialogState(() => frequency = value);
                        }
                      },
                    ),
                    if (frequency == 'weekly') ...[
                      const SizedBox(height: 10),
                      DropdownButtonFormField<String>(
                        key: ValueKey('routine-weekday-$weekday'),
                        initialValue: weekday,
                        decoration: const InputDecoration(labelText: '星期'),
                        items: const [
                          DropdownMenuItem(value: '1', child: Text('周一')),
                          DropdownMenuItem(value: '2', child: Text('周二')),
                          DropdownMenuItem(value: '3', child: Text('周三')),
                          DropdownMenuItem(value: '4', child: Text('周四')),
                          DropdownMenuItem(value: '5', child: Text('周五')),
                          DropdownMenuItem(value: '6', child: Text('周六')),
                          DropdownMenuItem(value: '7', child: Text('周日')),
                        ],
                        onChanged: (value) {
                          if (value != null) {
                            setDialogState(() => weekday = value);
                          }
                        },
                      ),
                    ],
                    const SizedBox(height: 10),
                    if (frequency == 'every')
                      TextField(
                        controller: minutesController,
                        keyboardType: TextInputType.number,
                        decoration: const InputDecoration(
                          labelText: '间隔分钟数（至少 15）',
                        ),
                        onChanged: (value) => minutes = value,
                      )
                    else
                      TextField(
                        controller: timeController,
                        keyboardType: TextInputType.datetime,
                        decoration: const InputDecoration(
                          labelText: '时间（HH:MM）',
                        ),
                        onChanged: (value) => time = value,
                      ),
                    const SizedBox(height: 4),
                    SwitchListTile(
                      contentPadding: EdgeInsets.zero,
                      title: const Text('启用例程'),
                      value: enabled,
                      onChanged: (value) =>
                          setDialogState(() => enabled = value),
                    ),
                    if (validationError != null)
                      Text(
                        validationError!,
                        style: TextStyle(
                          color: Theme.of(context).colorScheme.error,
                        ),
                      ),
                  ],
                ),
              ),
            ),
            actions: [
              TextButton(
                onPressed: () => Navigator.of(dialogContext).pop(),
                child: const Text('取消'),
              ),
              FilledButton(
                onPressed: () {
                  final title = titleController.text.trim();
                  final prompt = promptController.text.trim();
                  final everyValue = int.tryParse(minutes.trim());
                  if (title.isEmpty || prompt.isEmpty) {
                    setDialogState(() => validationError = '请填写名称和执行内容');
                    return;
                  }
                  if (frequency == 'every' &&
                      (everyValue == null || everyValue < 15)) {
                    setDialogState(() => validationError = '间隔分钟数至少为 15');
                    return;
                  }
                  if (frequency != 'every' &&
                      !RegExp(r'^([01]\d|2[0-3]):[0-5]\d$').hasMatch(time)) {
                    setDialogState(() => validationError = '时间格式应为 HH:MM');
                    return;
                  }
                  Navigator.of(dialogContext).pop(
                    _RoutineFormValues(
                      title: title,
                      prompt: prompt,
                      schedule: buildSchedule(),
                      timezone: timezone,
                      enabled: enabled,
                    ),
                  );
                },
                child: const Text('保存'),
              ),
            ],
          );
        },
      ),
    ).whenComplete(() {
      titleController.dispose();
      promptController.dispose();
      timeController.dispose();
      minutesController.dispose();
    });
  }

  void _showError(Object error, String fallback) {
    final text = error is AssistantApiException ? error.message : fallback;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(text)));
  }

  @override
  Widget build(BuildContext context) {
    final colors = widget.colors;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Row(
          children: [
            Expanded(
              child: _SectionHeading(label: '例程', colors: colors),
            ),
            if (widget.api != null)
              TextButton.icon(
                onPressed: () => _edit(),
                icon: const Icon(Icons.add, size: 17),
                label: const Text('新建例程'),
                style: TextButton.styleFrom(
                  foregroundColor: colors.accent,
                  tapTargetSize: MaterialTapTargetSize.shrinkWrap,
                ),
              ),
          ],
        ),
        if (_loading && _routines.isEmpty)
          const Padding(
            padding: EdgeInsets.symmetric(vertical: 20),
            child: Center(child: CircularProgressIndicator(strokeWidth: 2)),
          )
        else if (_routines.isEmpty)
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 18),
            child: Text(
              '还没有例程，创建一个让重要的事自动发生。',
              style: TextStyle(color: colors.muted, fontSize: 13),
            ),
          )
        else
          ..._routines.asMap().entries.map(
            (entry) => _RoutineCard(
              routine: entry.value,
              colors: colors,
              onEnabledChanged: widget.api == null
                  ? null
                  : (value) => _setEnabled(entry.key, value),
              onEdit: widget.api == null ? null : () => _edit(entry.value),
              onRun: widget.api == null ? null : () => _run(entry.value),
              showDivider: entry.key < _routines.length - 1,
            ),
          ),
      ],
    );
  }
}

class _RoutineCard extends StatelessWidget {
  const _RoutineCard({
    required this.routine,
    required this.colors,
    required this.onEnabledChanged,
    required this.onEdit,
    required this.onRun,
    required this.showDivider,
  });

  final Map<String, dynamic> routine;
  final MuseColors colors;
  final ValueChanged<bool>? onEnabledChanged;
  final VoidCallback? onEdit;
  final VoidCallback? onRun;
  final bool showDivider;

  @override
  Widget build(BuildContext context) {
    final enabled = routine['enabled'] == true;
    final schedule = _routineScheduleLabel('${routine['schedule'] ?? ''}');
    final status = _firstText([routine['last_status']]);
    return Column(
      children: [
        Padding(
          padding: const EdgeInsets.symmetric(vertical: 12),
          child: Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      _firstText([routine['title']]) ?? '未命名例程',
                      style: TextStyle(
                        color: colors.text,
                        fontSize: 14,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                    const SizedBox(height: 3),
                    Row(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          enabled ? '已启用' : '已停用',
                          style: TextStyle(color: colors.muted, fontSize: 12),
                        ),
                        Text(
                          ' · ',
                          style: TextStyle(color: colors.muted, fontSize: 12),
                        ),
                        Expanded(
                          child: Text(
                            schedule,
                            style: TextStyle(color: colors.muted, fontSize: 12),
                          ),
                        ),
                      ],
                    ),
                    if (status != null) ...[
                      const SizedBox(height: 3),
                      Text(
                        '上次运行：$status',
                        style: TextStyle(color: colors.faint, fontSize: 11),
                      ),
                    ],
                  ],
                ),
              ),
              if (onEnabledChanged != null)
                Switch(
                  value: enabled,
                  onChanged: onEnabledChanged,
                  materialTapTargetSize: MaterialTapTargetSize.shrinkWrap,
                ),
              if (onRun != null)
                IconButton(
                  tooltip: '立即运行',
                  onPressed: onRun,
                  icon: Icon(Icons.play_arrow, color: colors.accent, size: 20),
                  visualDensity: VisualDensity.compact,
                ),
              if (onEdit != null)
                IconButton(
                  tooltip: '编辑例程',
                  onPressed: onEdit,
                  icon: Icon(
                    Icons.edit_outlined,
                    color: colors.muted,
                    size: 18,
                  ),
                  visualDensity: VisualDensity.compact,
                ),
            ],
          ),
        ),
        if (showDivider) Container(height: 1, color: colors.line),
      ],
    );
  }
}

class _RoutineFormValues {
  const _RoutineFormValues({
    required this.title,
    required this.prompt,
    required this.schedule,
    required this.timezone,
    required this.enabled,
  });

  final String title;
  final String prompt;
  final String schedule;
  final String timezone;
  final bool enabled;
}

List<Map<String, dynamic>> _copyRows(List<Map<String, dynamic>> source) =>
    source.map((item) => Map<String, dynamic>.from(item)).toList();

String _routineScheduleLabel(String schedule) {
  final daily = RegExp(r'^daily\s+(\d{1,2}:\d{2})$').firstMatch(schedule);
  if (daily != null) return '每天 ${daily.group(1)}';
  final weekly = RegExp(r'^weekly\s+([1-7])\s+(\d{1,2}:\d{2})$')
      .firstMatch(schedule);
  if (weekly != null) {
    const weekdays = <String>['', '周一', '周二', '周三', '周四', '周五', '周六', '周日'];
    return '每${weekdays[int.parse(weekly.group(1)!)]} ${weekly.group(2)}';
  }
  final every = RegExp(r'^every\s+(\d+)m$').firstMatch(schedule);
  if (every != null) return '每 ${every.group(1)} 分钟';
  return schedule.isEmpty ? '未设置频率' : schedule;
}

class _EmptyState extends StatelessWidget {
  const _EmptyState({required this.colors});

  final MuseColors colors;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 48),
      child: Column(
        children: [
          Text(
            '暂时没有目标',
            style: TextStyle(
              color: colors.text,
              fontSize: 15,
              fontWeight: FontWeight.w600,
              height: 1.35,
            ),
            textAlign: TextAlign.center,
          ),
          const SizedBox(height: 6),
          Text(
            '新的目标和运行任务会显示在这里。',
            style: TextStyle(color: colors.muted, fontSize: 13, height: 1.45),
            textAlign: TextAlign.center,
          ),
        ],
      ),
    );
  }
}

String? _firstText(Iterable<dynamic> values) {
  for (final value in values) {
    if (value == null) continue;
    final text = '$value'.trim();
    if (text.isNotEmpty && text != 'null') return text;
  }
  return null;
}

bool _isDone(dynamic status) {
  final value = '$status'.toLowerCase();
  return value == 'done' || value == 'completed' || value == 'succeeded';
}

String _statusLabel(dynamic status) {
  final value = '$status'.toLowerCase();
  switch (value) {
    case 'done':
    case 'completed':
      return '已完成';
    case 'in_progress':
    case 'running':
      return '进行中';
    case 'pending':
      return '待处理';
    case 'queued':
      return '排队中';
    case 'waiting_approval':
      return '等待确认';
    case 'succeeded':
      return '已成功';
    case 'failed':
      return '失败';
    case 'cancelled':
    case 'canceled':
      return '已取消';
    default:
      return _firstText([status]) ?? '待处理';
  }
}

double? _progressValue(Map<String, dynamic> task) {
  dynamic raw =
      task['progress'] ?? task['completion'] ?? task['progress_ratio'];
  if (raw is Map) raw = raw['value'] ?? raw['percent'];
  if (raw is String) raw = double.tryParse(raw);
  if (raw is! num) return null;

  var value = raw.toDouble();
  if (value > 1) value /= 100;
  return value.clamp(0.0, 1.0).toDouble();
}
