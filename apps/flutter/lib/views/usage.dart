import 'dart:math' as math;

import 'package:flutter/material.dart';

import '../api.dart';
import '../theme.dart';

const _purposeLabels = {
  'chat_round': '对话轮次',
  'final_answer': '最终回答',
  'summary': '摘要',
  'voice_cleanup': '语音整理',
  'decider': '决策',
  'memory_extract': '记忆提取',
  'other': '其他',
};

num _number(Object? value) =>
    value is num && value.isFinite && value >= 0 ? value : 0;

List<Map<String, dynamic>> _rows(Object? value) => value is List
    ? value
          .whereType<Map>()
          .map((row) => Map<String, dynamic>.from(row))
          .toList()
    : const [];

String _tokens(num value) {
  final digits = value.round().toString();
  return digits.replaceAllMapped(
    RegExp(r'(\d)(?=(\d{3})+(?!\d))'),
    (match) => '${match[1]},',
  );
}

String _milliseconds(Object? value) =>
    value is num && value.isFinite ? '${value.round()} ms' : '—';

String _dateLabel(Object? value) {
  final date = value is String ? DateTime.tryParse(value) : null;
  return date == null ? '—' : '${date.month}/${date.day}';
}

class UsageSection extends StatefulWidget {
  const UsageSection({super.key, required this.api, this.onSelectSession});

  final AssistantApi api;
  final ValueChanged<String>? onSelectSession;

  @override
  State<UsageSection> createState() => _UsageSectionState();
}

class _UsageSectionState extends State<UsageSection> {
  String _range = '7d';
  Map<String, dynamic>? _usage;
  String? _error;
  bool _loading = true;
  int _requestVersion = 0;
  String? _openingSession;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    final version = ++_requestVersion;
    setState(() {
      _loading = true;
      _error = null;
      _usage = null;
    });
    try {
      final usage = await widget.api.usage(range: _range);
      if (!mounted || version != _requestVersion) return;
      setState(() {
        _usage = usage;
        _loading = false;
      });
    } catch (error) {
      if (!mounted || version != _requestVersion) return;
      setState(() {
        _error = error is AssistantApiException ? error.message : '用量暂时不可用';
        _loading = false;
      });
    }
  }

  Future<void> _openSession(String id) async {
    if (_openingSession != null) return;
    setState(() => _openingSession = id);
    try {
      await widget.api.getSession(id);
      if (!mounted) return;
      widget.onSelectSession?.call(id);
    } catch (_) {
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(const SnackBar(content: Text('会话已删除或无法访问')));
    } finally {
      if (mounted) setState(() => _openingSession = null);
    }
  }

  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.all(16),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        SizedBox(
          width: double.infinity,
          child: SegmentedButton<String>(
            key: const Key('usage-range'),
            showSelectedIcon: false,
            segments: const [
              ButtonSegment(value: '7d', label: Text('7 天')),
              ButtonSegment(value: '30d', label: Text('30 天')),
              ButtonSegment(value: '90d', label: Text('90 天')),
            ],
            selected: {_range},
            onSelectionChanged: (selection) {
              if (selection.first == _range) return;
              _range = selection.first;
              _load();
            },
          ),
        ),
        const SizedBox(height: 16),
        if (_loading)
          const Center(
            child: Padding(
              padding: EdgeInsets.all(20),
              child: CircularProgressIndicator(),
            ),
          )
        else if (_error != null)
          Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(_error!, style: TextStyle(color: context.muse.muted)),
              TextButton(onPressed: _load, child: const Text('重试')),
            ],
          )
        else if (_usage != null)
          ..._content(context, _usage!),
      ],
    ),
  );

  List<Widget> _content(BuildContext context, Map<String, dynamic> usage) {
    final totals = Map<String, dynamic>.from(usage['totals'] as Map);
    final performance = Map<String, dynamic>.from(usage['performance'] as Map);
    final daily = _rows(usage['daily']);
    final sessions = _rows(usage['top_sessions']);
    final estimatedRatio = _number(totals['estimated_ratio']).clamp(0, 1);
    final colors = context.muse;
    return [
      LayoutBuilder(
        builder: (context, constraints) {
          final width = constraints.maxWidth >= 300
              ? (constraints.maxWidth - 16) / 3
              : constraints.maxWidth;
          return Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              _UsageMetric(
                width: width,
                label: '总 tokens',
                value: _tokens(_number(totals['total_tokens'])),
              ),
              _UsageMetric(
                width: width,
                label: '请求数',
                value: _tokens(_number(totals['calls'])),
              ),
              _UsageMetric(
                width: width,
                label: '平均首字延迟',
                value: _milliseconds(performance['first_token_ms_avg']),
              ),
            ],
          );
        },
      ),
      const SizedBox(height: 10),
      Text(
        '输入 ${_tokens(_number(totals['prompt_tokens']))} · '
        '输出 ${_tokens(_number(totals['completion_tokens']))} tokens',
        style: TextStyle(color: colors.muted, fontSize: 12),
      ),
      Text(
        '总耗时 p50 ${_milliseconds(performance['duration_ms_p50'])} · '
        'p95 ${_milliseconds(performance['duration_ms_p95'])}',
        style: TextStyle(color: colors.muted, fontSize: 12),
      ),
      if (estimatedRatio > 0)
        Padding(
          padding: const EdgeInsets.only(top: 8),
          child: Text(
            '部分为估算（${(estimatedRatio * 100).toStringAsFixed(1)}% 的调用）',
            key: const Key('usage-estimated'),
            style: TextStyle(color: colors.warn, fontSize: 12),
          ),
        ),
      if (_number(totals['calls']) == 0)
        Padding(
          padding: const EdgeInsets.only(top: 16),
          child: Text('此范围暂无模型调用', style: TextStyle(color: colors.muted)),
        ),
      const _UsageHeading('每日 tokens'),
      UsageBarChart(daily: daily),
      _UsageDistribution(
        title: '按用途',
        rows: _rows(usage['purposes']),
        label: (row) => _purposeLabels[row['purpose']] ?? '其他',
      ),
      _UsageDistribution(
        title: '按模型',
        rows: _rows(usage['models']),
        label: (row) =>
            row['model'] is String && (row['model'] as String).isNotEmpty
            ? row['model'] as String
            : '未知模型',
      ),
      const _UsageHeading('Top 会话'),
      if (sessions.isEmpty)
        Text('暂无会话用量', style: TextStyle(color: colors.muted, fontSize: 12)),
      for (final session in sessions)
        ListTile(
          key: ValueKey('usage-session-${session['session_id']}'),
          contentPadding: EdgeInsets.zero,
          title: Text(
            session['title'] is String &&
                    (session['title'] as String).isNotEmpty
                ? session['title'] as String
                : '未命名会话',
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
          ),
          subtitle: Text(
            '${_tokens(_number(session['total_tokens']))} tokens · '
            '${_tokens(_number(session['calls']))} 次调用',
            style: TextStyle(color: colors.muted, fontSize: 12),
          ),
          trailing: _openingSession == session['session_id']
              ? const SizedBox(
                  width: 18,
                  height: 18,
                  child: CircularProgressIndicator(strokeWidth: 2),
                )
              : widget.onSelectSession != null
              ? Icon(Icons.chevron_right_rounded, color: colors.muted)
              : null,
          onTap:
              widget.onSelectSession != null &&
                  _openingSession == null &&
                  session['session_id'] is String &&
                  (session['session_id'] as String).isNotEmpty
              ? () => _openSession(session['session_id'] as String)
              : null,
        ),
    ];
  }
}

class _UsageHeading extends StatelessWidget {
  const _UsageHeading(this.title);

  final String title;

  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.only(top: 22, bottom: 10),
    child: Text(title, style: const TextStyle(fontWeight: FontWeight.w600)),
  );
}

class _UsageMetric extends StatelessWidget {
  const _UsageMetric({
    required this.width,
    required this.label,
    required this.value,
  });

  final double width;
  final String label;
  final String value;

  @override
  Widget build(BuildContext context) => Container(
    width: width,
    padding: const EdgeInsets.all(10),
    decoration: BoxDecoration(
      color: context.muse.bg,
      borderRadius: BorderRadius.circular(10),
    ),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(label, style: TextStyle(color: context.muse.muted, fontSize: 11)),
        const SizedBox(height: 6),
        Text(
          value,
          style: const TextStyle(fontSize: 17, fontWeight: FontWeight.w600),
        ),
      ],
    ),
  );
}

class _UsageDistribution extends StatelessWidget {
  const _UsageDistribution({
    required this.title,
    required this.rows,
    required this.label,
  });

  final String title;
  final List<Map<String, dynamic>> rows;
  final String Function(Map<String, dynamic>) label;

  @override
  Widget build(BuildContext context) {
    final total = rows.fold<num>(
      0,
      (sum, row) => sum + _number(row['total_tokens']),
    );
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        _UsageHeading(title),
        if (rows.isEmpty)
          Text(
            '暂无数据',
            style: TextStyle(color: context.muse.muted, fontSize: 12),
          ),
        for (final row in rows)
          Padding(
            padding: const EdgeInsets.only(bottom: 12),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(label(row), maxLines: 2, overflow: TextOverflow.ellipsis),
                const SizedBox(height: 4),
                Text(
                  '${_tokens(_number(row['total_tokens']))} tokens · '
                  '${_tokens(_number(row['calls']))} 次调用',
                  style: TextStyle(color: context.muse.muted, fontSize: 12),
                ),
                const SizedBox(height: 6),
                LinearProgressIndicator(
                  value: total > 0
                      ? (_number(row['total_tokens']) / total).toDouble()
                      : 0,
                  color: context.muse.accent,
                  backgroundColor: context.muse.chipHover,
                  borderRadius: BorderRadius.circular(3),
                  minHeight: 5,
                ),
              ],
            ),
          ),
      ],
    );
  }
}

class UsageBarChart extends StatelessWidget {
  const UsageBarChart({super.key, required this.daily});

  final List<Map<String, dynamic>> daily;

  @override
  Widget build(BuildContext context) {
    final colors = context.muse;
    return Semantics(
      label:
          '每日用量，${daily.map((row) => '${_dateLabel(row['date'])}：${_tokens(_number(row['total_tokens']))} tokens，${_number(row['calls'])} 次调用').join('；')}',
      child: SizedBox(
        width: double.infinity,
        height: 190,
        child: CustomPaint(
          key: const Key('usage-daily-chart'),
          painter: UsageBarChartPainter(
            daily: daily,
            barColor: colors.accent,
            gridColor: colors.line,
            labelColor: colors.muted,
            textDirection: Directionality.of(context),
          ),
        ),
      ),
    );
  }
}

class UsageBarChartPainter extends CustomPainter {
  UsageBarChartPainter({
    required this.daily,
    required this.barColor,
    required this.gridColor,
    required this.labelColor,
    required this.textDirection,
  });

  final List<Map<String, dynamic>> daily;
  final Color barColor;
  final Color gridColor;
  final Color labelColor;
  final TextDirection textDirection;

  double get axisMaximum {
    final maximum = daily.fold<double>(
      0,
      (value, row) => math.max(value, _number(row['total_tokens']).toDouble()),
    );
    if (maximum == 0) return 4;
    final step = maximum / 4;
    final magnitude = math
        .pow(10, (math.log(step) / math.ln10).floor())
        .toDouble();
    final scaled = step / magnitude;
    final rounded = scaled <= 1
        ? 1
        : scaled <= 2
        ? 2
        : scaled <= 5
        ? 5
        : 10;
    return rounded * magnitude * 4;
  }

  String _axisLabel(double value) {
    if (value >= 1000000) return '${(value / 1000000).toStringAsFixed(1)}M';
    if (value >= 1000) return '${(value / 1000).toStringAsFixed(1)}K';
    return value < 1 && value > 0
        ? value.toStringAsFixed(1)
        : '${value.round()}';
  }

  void _label(
    Canvas canvas,
    String text,
    Offset position, {
    bool right = false,
    bool center = false,
  }) {
    final painter = TextPainter(
      text: TextSpan(
        text: text,
        style: TextStyle(color: labelColor, fontSize: 10),
      ),
      textDirection: textDirection,
    )..layout();
    painter.paint(
      canvas,
      position.translate(
        right
            ? -painter.width
            : center
            ? -painter.width / 2
            : 0,
        0,
      ),
    );
  }

  @override
  void paint(Canvas canvas, Size size) {
    const left = 46.0;
    const top = 8.0;
    const bottom = 24.0;
    final width = math.max(0.0, size.width - left - 12);
    final height = math.max(0.0, size.height - top - bottom);
    final maxValue = axisMaximum;
    final grid = Paint()..color = gridColor;
    for (var tick = 0; tick <= 4; tick++) {
      final y = top + height - height * tick / 4;
      canvas.drawLine(Offset(left, y), Offset(left + width, y), grid);
      _label(
        canvas,
        _axisLabel(maxValue * tick / 4),
        Offset(left - 6, y - 6),
        right: true,
      );
    }
    if (daily.isEmpty) return;
    final slot = width / daily.length;
    final barWidth = math.max(1.0, math.min(24.0, slot * .72));
    final paint = Paint()..color = barColor;
    for (var index = 0; index < daily.length; index++) {
      final x = left + slot * (index + .5);
      final barHeight =
          height * _number(daily[index]['total_tokens']) / maxValue;
      canvas.drawRRect(
        RRect.fromRectAndRadius(
          Rect.fromLTWH(
            x - barWidth / 2,
            top + height - barHeight,
            barWidth,
            barHeight,
          ),
          const Radius.circular(2),
        ),
        paint,
      );
      final stride = math.max(
        1,
        (daily.length / math.max(1, (width / 60).floor())).ceil(),
      );
      if (index % stride == 0) {
        _label(
          canvas,
          _dateLabel(daily[index]['date']),
          Offset(x, top + height + 7),
          center: true,
        );
      }
    }
  }

  @override
  bool shouldRepaint(UsageBarChartPainter oldDelegate) =>
      daily != oldDelegate.daily ||
      barColor != oldDelegate.barColor ||
      gridColor != oldDelegate.gridColor ||
      labelColor != oldDelegate.labelColor ||
      textDirection != oldDelegate.textDirection;
}
